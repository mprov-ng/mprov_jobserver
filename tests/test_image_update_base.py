"""
An OS distro with a base_image_url builds new images from that published
image instead of bootstrapping them with dnf --installroot (#92).  The
download is stubbed with a tarball built here; the dnf commands are
recorded (test_image_update's fixtures).
"""
import hashlib

import pytest

from mprov_jobserver.plugins import base_image
# the fixtures (fake_wget is autouse there, so it's imported to apply here too)
from test_image_update import fake_wget, images, image, job_states, rhel  # noqa: F401
from test_base_image import BASE, tar_bytes

URL = "https://dl.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-Container-Base.latest.x86_64.tar.xz"
ARCHIVE = tar_bytes(BASE + [("etc/machine-id", b"0123456789abcdef\n"),
                            ("etc/fstab", b"UUID=abc / xfs defaults 0 0\n")])
SHA = hashlib.sha256(ARCHIVE).hexdigest()


@pytest.fixture
def downloads(monkeypatch):
  """ Record base image downloads; each writes ARCHIVE. """
  got = []
  def download(url, dest, get=None):
    got.append(url)
    with open(dest, "wb") as f:
      f.write(ARCHIVE)
    return SHA
  monkeypatch.setattr(base_image, "download", download)
  return got


def from_base(rhel, checksum="sha256:" + SHA, url=URL):
  rhel.imageDetails = image()
  rhel.imageDetails["osdistro"].update(base_image_url=url, base_image_checksum=checksum, distArch="x86_64")


def test_a_new_image_is_built_from_the_base_image(js, rhel, images, commands, downloads):
  from_base(rhel)
  rhel.handle_jobs()
  img = images / "compute"
  assert rhel.threadOk
  assert downloads == [URL]
  assert (img / "etc/os-release").read_text() == "ID=rocky\n"
  assert base_image.read_marker(str(img)) == {"url": URL, "sha256": SHA}
  # no host-side bootstrap: everything installs inside the base
  assert not commands.matching("--nogpgcheck install \"*-gpg-keys\"")
  assert not commands.matching(f"dnf -y --installroot={img} --releasever=9 --nogpgcheck groupinstall")
  assert commands.matching(f"chroot {img} dnf -y --releasever=9 --allowerasing groupinstall 'Minimal Install'")
  assert commands.matching(f"chroot {img}  dnf -y  --releasever=9  install kernel")
  # nothing is left of the scratch space beside the image
  assert sorted(p.name for p in images.iterdir()) == ["compute"]


def test_the_base_images_identity_is_reset(js, rhel, images, commands, downloads):
  from_base(rhel)
  rhel.handle_jobs()
  assert (images / "compute/etc/machine-id").read_text() == ""
  assert "UUID" not in (images / "compute/etc/fstab").read_text()


def test_cloud_init_and_the_base_kernel_are_removed(js, rhel, images, commands, downloads):
  from_base(rhel)
  rhel.handle_jobs()
  img = images / "compute"
  removes = commands.matching("dnf -y remove --noautoremove")
  assert removes == [
    f"chroot {img} dnf -y remove --noautoremove cloud-init",
    f"chroot {img} dnf -y remove --noautoremove 'kernel' 'kernel-core' 'kernel-modules*'",
  ]
  # removed before the image's own kernel goes in
  assert commands.run.index(removes[-1]) < commands.run.index(commands.matching("install kernel")[0])


def test_nothing_is_removed_when_the_base_has_neither(js, rhel, images, commands, downloads):
  commands.fail = ["rpm -q --quiet"]
  from_base(rhel)
  rhel.handle_jobs()
  assert not commands.matching("dnf -y remove")
  assert rhel.threadOk


def test_a_failed_removal_fails_the_job(js, rhel, commands, downloads):
  commands.fail = ["remove --noautoremove cloud-init"]
  from_base(rhel)
  rhel.handle_jobs()
  assert not rhel.threadOk
  assert job_states(js) == [(3, 9)]
  assert not commands.matching("install kernel")


def test_stock_repos_are_disabled_with_the_hosts_dnf(js, rhel, images, commands, monkeypatch):
  base = BASE + [("etc/yum.repos.d", None), ("etc/yum.repos.d/rocky.repo", b"[baseos]\n[appstream]\n")]
  archive = tar_bytes(base)
  def download(url, dest, get=None):
    with open(dest, "wb") as f:
      f.write(archive)
    return hashlib.sha256(archive).hexdigest()
  monkeypatch.setattr(base_image, "download", download)
  from_base(rhel, checksum="sha256:" + hashlib.sha256(archive).hexdigest())
  rhel.handle_jobs()
  img = images / "compute"
  assert commands.matching(f"dnf -y --installroot={img} --releasever=9 config-manager --disable appstream baseos")


def test_a_checksum_mismatch_fails_the_job_and_unpacks_nothing(js, rhel, images, commands, downloads, capsys):
  from_base(rhel, checksum="sha256:" + "0" * 64)
  rhel.handle_jobs()
  assert not rhel.threadOk
  assert job_states(js) == [(3, 9)]
  assert not (images / "compute/etc/os-release").exists()
  assert not commands.matching("dnf")
  assert "not " + "0" * 64 in capsys.readouterr().out
  assert sorted(p.name for p in images.iterdir()) == ["compute"]


def test_a_base_image_url_without_a_checksum_fails_the_job(js, rhel, commands, downloads):
  from_base(rhel, checksum="")
  rhel.handle_jobs()
  assert not rhel.threadOk
  assert downloads == []
  assert job_states(js) == [(3, 9)]


def test_a_base_image_that_isnt_an_os_fails_the_job(js, rhel, images, commands, monkeypatch):
  archive = tar_bytes([("readme", b"hi")])
  def download(url, dest, get=None):
    with open(dest, "wb") as f:
      f.write(archive)
    return hashlib.sha256(archive).hexdigest()
  monkeypatch.setattr(base_image, "download", download)
  from_base(rhel, checksum="sha256:" + hashlib.sha256(archive).hexdigest())
  rhel.handle_jobs()
  assert not rhel.threadOk
  assert job_states(js) == [(3, 9)]


@pytest.mark.parametrize("osdistro", [
  {},                                                  # an mPCC without the fields
  {"base_image_url": "", "base_image_checksum": ""},   # fields left empty
  {"base_image_url": None, "base_image_checksum": None},
])
def test_without_a_base_image_url_the_image_is_built_with_dnf(js, rhel, images, commands, downloads, osdistro):
  rhel.imageDetails = image()
  rhel.imageDetails["osdistro"].update(osdistro)
  rhel.handle_jobs()
  assert downloads == []
  assert commands.matching(f"dnf -y --installroot={images}/compute --releasever=9 --nogpgcheck groupinstall")
  assert base_image.read_marker(str(images / "compute")) is None
  assert rhel.threadOk


def test_a_rebuild_updates_the_tree_in_place(js, rhel, images, commands, downloads):
  from_base(rhel)
  rhel.handle_jobs()
  (images / "compute/etc/kept").write_text("mine")
  rhel.handle_jobs()
  # downloaded and unpacked once; the second build updates what's there,
  # from inside the tree as the first did, never with the host's dnf
  assert downloads == [URL]
  assert not commands.matching(f"--installroot={images}/compute --releasever=9 --nogpgcheck")
  assert len(commands.matching(f"chroot {images}/compute dnf -y --releasever=9 --allowerasing groupinstall")) == 2
  assert (images / "compute/etc/kept").read_text() == "mine"
  assert (images / "compute/etc/machine-id").read_text() == ""


@pytest.mark.parametrize("marker, warning", [
  (None, "was built with dnf"),
  ({"url": "https://old/base.tar.xz", "sha256": SHA}, "was built from the base image https://old/base.tar.xz"),
])
def test_an_existing_tree_from_another_source_is_kept_with_a_warning(js, rhel, images, commands, downloads, capsys, marker, warning):
  (images / "compute/etc").mkdir(parents=True)
  (images / "compute/etc/os-release").write_text("ID=rocky\n")
  if marker:
    base_image.write_marker(str(images / "compute"), marker["url"], marker["sha256"])
  from_base(rhel)
  rhel.handle_jobs()
  assert downloads == []
  assert warning in capsys.readouterr().out
  assert rhel.threadOk
  # a dnf-built tree stays on the dnf path; one from a base, inside the tree
  hostDnf = commands.matching(f"--installroot={images}/compute --releasever=9 --nogpgcheck groupinstall")
  assert bool(hostDnf) == (marker is None)


def test_a_base_built_tree_stays_on_its_path_when_the_url_is_cleared(js, rhel, images, commands, downloads):
  from_base(rhel)
  rhel.handle_jobs()
  from_base(rhel, url="", checksum="")
  rhel.handle_jobs()
  assert downloads == [URL]
  assert not commands.matching("--nogpgcheck groupinstall")
  assert rhel.threadOk
