"""
base_image fetches, checks and unpacks a published OS image as the base of
a system image (#92): OCI image archives (as Rocky's Container-Base images
are) and plain root filesystem tarballs.  The archives here are built in the
test; nothing is downloaded.
"""
import hashlib
import io
import json
import os
import stat
import tarfile

import pytest
import requests

from mprov_jobserver.plugins import base_image
from mprov_jobserver.plugins.base_image import BaseImageError

HEX = "ab" * 32


# archives

def tar_bytes(entries, mode="w:gz"):
  """
  A tar archive from (name, content) pairs: bytes is a file, None a
  directory, ("symlink", target) or ("hardlink", target) a link.
  """
  buf = io.BytesIO()
  with tarfile.open(fileobj=buf, mode=mode) as tar:
    for name, content in entries:
      info = tarfile.TarInfo(name)
      info.mtime = 0
      if content is None:
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        tar.addfile(info)
      elif isinstance(content, tuple):
        info.type = tarfile.SYMTYPE if content[0] == "symlink" else tarfile.LNKTYPE
        info.linkname = content[1]
        tar.addfile(info)
      else:
        info.size = len(content)
        info.mode = 0o4755 if name.endswith("suid") else 0o644
        tar.addfile(info, io.BytesIO(content))
  return buf.getvalue()


def sha(data):
  return hashlib.sha256(data).hexdigest()


def oci_archive(layers, arch="amd64", index_list=False, tamper=False):
  """ An OCI image archive with these layer tarballs, as bytes. """
  blobs = {}
  def blob(data):
    blobs[sha(data)] = data
    return {"digest": "sha256:" + sha(data), "size": len(data)}
  layer_descs = [dict(blob(l), mediaType="application/vnd.oci.image.layer.v1.tar+gzip") for l in layers]
  config = blob(b"{}")
  manifest = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                         "config": config, "layers": layer_descs}).encode()
  mdesc = dict(blob(manifest), mediaType="application/vnd.oci.image.manifest.v1+json")
  if index_list:
    # a nested index listing two platforms
    other = json.dumps({"schemaVersion": 2, "config": config, "layers": []}).encode()
    nested = json.dumps({"schemaVersion": 2, "mediaType": base_image.OCI_INDEX, "manifests": [
      dict(blob(other), platform={"os": "linux", "architecture": "arm64"}),
      dict(mdesc, platform={"os": "linux", "architecture": arch}),
    ]}).encode()
    top = [dict(blob(nested), mediaType=base_image.OCI_INDEX)]
  else:
    top = [mdesc]
  index = json.dumps({"schemaVersion": 2, "manifests": top}).encode()
  entries = [("oci-layout", b'{"imageLayoutVersion": "1.0.0"}'), ("index.json", index),
             ("blobs", None), ("blobs/sha256", None)]
  for digest, data in blobs.items():
    if tamper and data in layers:
      data = data + b"x"
    entries.append(("blobs/sha256/" + digest, data))
  return tar_bytes(entries, mode="w:xz")


@pytest.fixture
def root(tmp_path):
  path = tmp_path / "image"
  path.mkdir()
  return path


@pytest.fixture
def workdir(tmp_path):
  path = tmp_path / "work"
  path.mkdir()
  return path


def unpack(tmp_path, data, root, workdir, arch="x86_64"):
  archive = tmp_path / "base-image"
  archive.write_bytes(data)
  base_image.unpack(str(archive), str(root), str(workdir), arch)


BASE = [("etc", None), ("etc/os-release", b"ID=rocky\n"), ("usr", None), ("usr/bin", None),
        ("usr/bin/suid", b"#!"), ("bin", ("symlink", "usr/bin")), ("etc/old", b"old"),
        ("etc/dir", None), ("etc/dir/a", b"a")]


# checksums

def test_parses_rockys_bsd_checksum_file():
  text = ("# Rocky-9-Container-Base.latest.x86_64.tar.xz: 86205196 bytes\n"
          f"SHA256 (Rocky-9-Container-Base.latest.x86_64.tar.xz) = {HEX.upper()}\n")
  assert base_image.parse_checksum(text, "Rocky-9-Container-Base.latest.x86_64.tar.xz") == HEX


def test_parses_sha256sum_lines_and_a_bare_digest():
  text = f"{'cd' * 32}  other.tar.xz\n{HEX} *base.tar.xz\n"
  assert base_image.parse_checksum(text, "base.tar.xz") == HEX
  assert base_image.parse_checksum(f"{HEX}\n", "anything") == HEX


def test_a_checksum_file_without_the_image_is_refused():
  with pytest.raises(BaseImageError, match="no SHA256 for base.tar.xz"):
    base_image.parse_checksum(f"{HEX}  other.tar.xz\n", "base.tar.xz")


def test_an_inline_checksum():
  assert base_image.expected_sha256(f" sha256:{HEX.upper()} ", "https://x/b.tar.xz") == HEX


@pytest.mark.parametrize("checksum, message", [
  ("", "no base image checksum"),
  (None, "no base image checksum"),
  ("sha256:abc", "64 hex digits"),
  ("md5:" + "a" * 32, "neither sha256"),
  ("/srv/CHECKSUM", "neither sha256"),
])
def test_a_missing_or_malformed_checksum_is_refused(checksum, message):
  with pytest.raises(BaseImageError, match=message):
    base_image.expected_sha256(checksum, "https://x/b.tar.xz")


class Reply:
  def __init__(self, status=200, text="", body=b""):
    self.status_code, self.text, self.body = status, text, body
  def __enter__(self):
    return self
  def __exit__(self, *a):
    return False
  def iter_content(self, chunk_size):
    yield self.body


def test_a_checksum_url_is_fetched_and_read_by_the_images_file_name():
  asked = []
  def get(url, **kwargs):
    asked.append((url, kwargs))
    return Reply(text=f"SHA256 (b.tar.xz) = {HEX}\n")
  assert base_image.expected_sha256("https://x/b.tar.xz.CHECKSUM", "https://x/dir/b.tar.xz?x=1", get) == HEX
  assert asked[0][0] == "https://x/b.tar.xz.CHECKSUM"
  assert asked[0][1]["timeout"]


def test_a_checksum_url_that_fails_is_refused():
  with pytest.raises(BaseImageError, match="HTTP 404"):
    base_image.expected_sha256("https://x/C", "https://x/b", lambda url, **k: Reply(status=404))
  def broken(url, **kwargs):
    raise requests.ConnectionError("refused")
  with pytest.raises(BaseImageError, match="refused"):
    base_image.expected_sha256("https://x/C", "https://x/b", broken)


# download

def test_download_writes_the_file_and_returns_its_sha256(tmp_path):
  dest = tmp_path / "f"
  asked = []
  def get(url, **kwargs):
    asked.append(kwargs)
    return Reply(body=b"image")
  assert base_image.download("https://x/b", str(dest), get) == sha(b"image")
  assert dest.read_bytes() == b"image"
  assert asked[0]["stream"] and asked[0]["timeout"]


def test_download_refuses_other_schemes_and_failed_replies(tmp_path):
  with pytest.raises(BaseImageError, match="isn't an http"):
    base_image.download("file:///etc/shadow", str(tmp_path / "f"))
  with pytest.raises(BaseImageError, match="HTTP 500"):
    base_image.download("https://x/b", str(tmp_path / "f"), lambda url, **k: Reply(status=500))


# unpacking

def test_unpacks_a_plain_rootfs_tarball(tmp_path, root, workdir):
  unpack(tmp_path, tar_bytes(BASE), root, workdir)
  assert (root / "etc/os-release").read_text() == "ID=rocky\n"
  assert os.readlink(root / "bin") == "usr/bin"
  # setuid bits survive: a root filesystem needs them
  assert stat.S_IMODE(os.lstat(root / "usr/bin/suid").st_mode) == 0o4755


def test_unpacks_an_oci_archive_with_whiteouts(tmp_path, root, workdir):
  top = tar_bytes([("etc", None), ("etc/.wh.old", b""), ("etc/dir", None), ("etc/dir/.wh..wh..opq", b""),
                   ("etc/dir/b", b"b"), ("etc/new", b"new"), ("usr/bin/suid", b"replaced")])
  unpack(tmp_path, oci_archive([tar_bytes(BASE), top]), root, workdir)
  assert not (root / "etc/old").exists()
  assert sorted(os.listdir(root / "etc/dir")) == ["b"]
  assert (root / "etc/new").read_text() == "new"
  assert (root / "usr/bin/suid").read_bytes() == b"replaced"
  # the whiteout markers themselves aren't left in the image
  assert not list(root.rglob(".wh.*"))


def test_picks_the_manifest_for_the_distros_architecture(tmp_path, root, workdir):
  unpack(tmp_path, oci_archive([tar_bytes(BASE)], index_list=True), root, workdir)
  assert (root / "etc/os-release").exists()


def test_refuses_an_archive_without_the_architecture(tmp_path, root, workdir):
  with pytest.raises(BaseImageError, match="no manifest for linux/ppc64le"):
    unpack(tmp_path, oci_archive([tar_bytes(BASE)], index_list=True), root, workdir, arch="ppc64le")


def test_refuses_a_blob_that_doesnt_match_its_digest(tmp_path, root, workdir):
  with pytest.raises(BaseImageError, match="doesn't match its digest"):
    unpack(tmp_path, oci_archive([tar_bytes(BASE)], tamper=True), root, workdir)


@pytest.mark.parametrize("entries", [
  [("../escape", b"x")],
  [("etc/../../escape", b"x")],
  # a symlink to the host, then a file through it
  [("etc", ("symlink", "/tmp")), ("etc/passwd", b"x")],
  # a hard link to a host file
  [("evil", ("hardlink", "../../outside"))],
])
def test_refuses_paths_that_leave_the_image(tmp_path, root, workdir, entries):
  with pytest.raises(BaseImageError):
    unpack(tmp_path, tar_bytes(entries), root, workdir)
  assert not (tmp_path / "escape").exists()


def test_absolute_member_names_stay_in_the_image(tmp_path, root, workdir):
  unpack(tmp_path, tar_bytes([("/etc", None), ("/etc/os-release", b"ID=x\n")]), root, workdir)
  assert (root / "etc/os-release").exists()


def test_refuses_something_that_isnt_a_tar(tmp_path, root, workdir):
  with pytest.raises(BaseImageError, match="isn't a tar archive"):
    unpack(tmp_path, b"QFI\xfb qcow2", root, workdir)


# after unpacking

def test_reset_identity_empties_machine_id_and_fstab(root):
  (root / "etc").mkdir()
  (root / "etc/machine-id").write_text("0123456789abcdef\n")
  (root / "etc/fstab").symlink_to("/etc/fstab")
  base_image.reset_identity(str(root))
  assert (root / "etc/machine-id").read_text() == ""
  assert not (root / "etc/fstab").is_symlink()
  assert "UUID" not in (root / "etc/fstab").read_text()


def test_the_marker_records_the_base(root):
  (root / "etc").mkdir()
  assert base_image.read_marker(str(root)) is None
  base_image.write_marker(str(root), "https://x/b", HEX)
  assert base_image.read_marker(str(root)) == {"url": "https://x/b", "sha256": HEX}
