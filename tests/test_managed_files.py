"""
Managed files (#96, mprov_control_center#519): the files the mPCC attaches
to an image are placed into it while it's built, safely: nothing outside the
image tree, nothing secret in the log, nothing half-done packed.
"""
import json
import os
import stat
import subprocess

import pytest

from mprov_jobserver import managed_files as mf
from conftest import FakeResponse, FakeSession, MPROV_URL

CHRONY = {"slug": "chrony-conf", "name": "chrony.conf", "version": 3, "dest_path": "/etc/chrony.conf",
          "owner": "root", "group": "chrony", "mode": "0640", "template": True,
          "url": "/systemimages/compute/files/chrony-conf/"}
MOTD = {"slug": "motd", "name": "motd", "version": 1, "dest_path": "/etc/motd", "owner": "root", "group": "root",
        "mode": "0644", "template": False, "url": "/media/motd-v1"}
SECRET_LINE = b"server ntp.example iburst # param: s3cr3t-value\n"


@pytest.fixture
def image(tmp_path):
  root = tmp_path / "image"
  (root / "etc").mkdir(parents=True)
  return root


@pytest.fixture
def chroot(monkeypatch):
  """ chroot <image> chown/rpm/dnf, recorded; chown succeeds for known names. """
  calls = []
  known = {"root", "chrony", "0"}
  state = {"owned": {}, "reinstall_ok": True}

  def run(root, args):
    calls.append(args)
    if args[0] == "chown":
      user, group = args[1].split(":")
      ok = user in known and group in known
      return subprocess.CompletedProcess(args, 0 if ok else 1, "", "" if ok else f"chown: invalid group: '{args[1]}'")
    if args[0] == "rpm":
      owner = state["owned"].get(args[-1])
      return subprocess.CompletedProcess(args, 0 if owner else 1, owner or f"file {args[-1]} is not owned by any package", "")
    if args[0] == "dnf":
      return subprocess.CompletedProcess(args, 0 if state["reinstall_ok"] else 1, "", "")
    raise AssertionError(f"unexpected chroot {args}")
  monkeypatch.setattr(mf, "chroot", run)
  run.calls, run.state = calls, state
  return run


@pytest.fixture
def session():
  session = FakeSession()
  session.verify = "/var/lib/mprov/tls/ca.crt"
  session.routes["systemimages/compute/files/chrony-conf/"] = SECRET_LINE
  return session


@pytest.fixture
def media(monkeypatch):
  seen = []

  def get(url, **kwargs):
    seen.append((url, kwargs))
    return FakeResponse(200, b"Welcome\n")
  monkeypatch.setattr(mf.requests, "get", get)
  return seen


def config(*files):
  return {"files": [dict(f) for f in files]}


def manifest(image):
  return json.loads((image / "var/lib/mprov/placed-files.json").read_text())


# placing

def test_files_are_placed_with_their_owner_group_and_mode(image, chroot, session, media, caplog):
  mf.place_files(session, MPROV_URL, str(image), config(CHRONY, MOTD))
  chrony = image / "etc/chrony.conf"
  assert chrony.read_bytes() == SECRET_LINE
  assert stat.S_IMODE(os.stat(chrony).st_mode) == 0o640
  assert (image / "etc/motd").read_bytes() == b"Welcome\n"
  # chown inside the image, so the names are the image's own
  chowns = [c for c in chroot.calls if c[0] == "chown"]
  assert [c[1] for c in chowns] == ["root:chrony", "root:root"]
  assert all(c[2].startswith("/etc/.") for c in chowns)
  assert "placed /etc/chrony.conf (chrony-conf v3)" in caplog.text
  # a rendered file holds config_params: never its contents
  assert b"s3cr3t" not in caplog.text.encode()
  assert manifest(image) == {"/etc/chrony.conf": {"slug": "chrony-conf", "version": 3, "package": None},
                             "/etc/motd": {"slug": "motd", "version": 1, "package": None}}
  # no temporary files left
  assert sorted(os.listdir(image / "etc")) == ["chrony.conf", "motd"]


def test_a_template_comes_from_the_mpcc_and_an_upload_from_media(image, chroot, session, media):
  mf.place_files(session, MPROV_URL, str(image), config(CHRONY, MOTD))
  assert ("GET", "systemimages/compute/files/chrony-conf/") in session.requests
  url, kwargs = media[0]
  assert url == "http://mpcc/media/motd-v1"
  assert kwargs["verify"] == "/var/lib/mprov/tls/ca.crt"


def test_missing_parents_are_made_root_owned_0755(image, chroot, session):
  session.routes["systemimages/compute/files/deep/"] = b"x"
  deep = dict(CHRONY, slug="deep", dest_path="/opt/site/conf/deep.conf", url="/systemimages/compute/files/deep/")
  mf.place_files(session, MPROV_URL, str(image), config(deep))
  for part in ("opt", "opt/site", "opt/site/conf"):
    st = os.stat(image / part)
    assert stat.S_IMODE(st.st_mode) == 0o755
    assert (st.st_uid, st.st_gid) == (0, 0)


def test_an_older_mpcc_without_files_changes_nothing(image, chroot, session):
  mf.place_files(session, MPROV_URL, str(image), {"config_params": {}})
  mf.place_files(session, MPROV_URL, str(image), None)
  assert not (image / "var").exists()
  assert chroot.calls == []


def test_a_template_that_doesnt_render_fails_with_the_mpccs_reason(image, chroot, session):
  session.routes["systemimages/compute/files/chrony-conf/"] = (422, {"detail": "chrony-conf: 'ntp' is undefined"})
  with pytest.raises(mf.PlacementError) as err:
    mf.place_files(session, MPROV_URL, str(image), config(CHRONY))
  assert "'ntp' is undefined" in str(err.value)
  assert not (image / "etc/chrony.conf").exists()


def test_a_file_over_10_mib_is_refused(image, chroot, session):
  session.routes["systemimages/compute/files/chrony-conf/"] = b"x" * (mf.MAX_BYTES + 1)
  with pytest.raises(mf.PlacementError, match="larger than 10 MiB"):
    mf.place_files(session, MPROV_URL, str(image), config(CHRONY))
  assert not (image / "etc/chrony.conf").exists()


def test_an_unknown_group_fails_and_leaves_nothing(image, chroot, session):
  with pytest.raises(mf.PlacementError, match="no user or group root:ntpd"):
    mf.place_files(session, MPROV_URL, str(image), config(dict(CHRONY, group="ntpd")))
  assert os.listdir(image / "etc") == []


def test_setuid_is_refused_unless_allowed(image, chroot, session):
  setuid = dict(CHRONY, mode="4755")
  with pytest.raises(mf.PlacementError, match="allowSetuidFiles"):
    mf.place_files(session, MPROV_URL, str(image), config(setuid))
  mf.place_files(session, MPROV_URL, str(image), config(setuid), allow_setuid=True)
  assert stat.S_IMODE(os.stat(image / "etc/chrony.conf").st_mode) == 0o4755


# checked before anything is written

@pytest.mark.parametrize("dest", ["etc/x", "/", "/etc/../root/x", "/etc//x", "/etc/x/", "/proc/x", "/sys/x",
                                  "/dev/x", "/run/x", "/tmp/mprov/tls/jobserver.pem", "/etc/x\0y"])
def test_bad_destinations_are_refused_before_anything_is_written(image, chroot, session, media, dest):
  with pytest.raises(mf.PlacementError):
    mf.place_files(session, MPROV_URL, str(image), config(MOTD, dict(CHRONY, dest_path=dest)))
  assert not (image / "etc/motd").exists()
  assert session.requests == [] and media == []


@pytest.mark.parametrize("change", [{"mode": "644x"}, {"mode": "99"}, {"owner": "root;rm"}, {"group": ""},
                                    {"url": ""}, {"slug": None}])
def test_bad_entries_are_refused(image, chroot, session, change):
  with pytest.raises(mf.PlacementError):
    mf.place_files(session, MPROV_URL, str(image), config(dict(CHRONY, **change)))


def test_two_files_at_one_destination_are_refused(image, chroot, session):
  with pytest.raises(mf.PlacementError, match="same destination"):
    mf.place_files(session, MPROV_URL, str(image), config(CHRONY, dict(MOTD, dest_path="/etc/chrony.conf")))


def test_a_link_in_the_image_never_sends_a_write_outside_it(image, chroot, session, tmp_path):
  outside = tmp_path / "host-etc"
  outside.mkdir()
  (image / "etc").rmdir()
  os.symlink(str(outside), str(image / "etc"))
  with pytest.raises(mf.PlacementError, match="a link"):
    mf.place_files(session, MPROV_URL, str(image), config(CHRONY))
  assert os.listdir(outside) == []


def test_a_directory_at_the_destination_isnt_replaced(image, chroot, session):
  (image / "etc/chrony.conf").mkdir()
  with pytest.raises(mf.PlacementError, match="is a directory"):
    mf.place_files(session, MPROV_URL, str(image), config(CHRONY))
  assert (image / "etc/chrony.conf").is_dir()


def test_a_link_at_the_destination_is_replaced_not_followed(image, chroot, session, tmp_path):
  target = tmp_path / "host-file"
  target.write_text("host")
  os.symlink(str(target), str(image / "etc/chrony.conf"))
  mf.place_files(session, MPROV_URL, str(image), config(CHRONY))
  assert target.read_text() == "host"
  assert not os.path.islink(image / "etc/chrony.conf")


# files no longer attached

def test_a_file_no_longer_attached_is_removed(image, chroot, session, media):
  mf.place_files(session, MPROV_URL, str(image), config(CHRONY, MOTD))
  mf.place_files(session, MPROV_URL, str(image), config(CHRONY))
  assert not (image / "etc/motd").exists()
  assert list(manifest(image)) == ["/etc/chrony.conf"]


def test_a_package_owned_file_is_restored_from_its_package(image, chroot, session, caplog):
  (image / "etc/chrony.conf").write_text("package default")
  chroot.state["owned"]["/etc/chrony.conf"] = "chrony"
  mf.place_files(session, MPROV_URL, str(image), config(CHRONY))
  assert manifest(image)["/etc/chrony.conf"]["package"] == "chrony"
  mf.place_files(session, MPROV_URL, str(image), config())
  assert ["dnf", "-y", "reinstall", "chrony"] in chroot.calls
  assert "restored /etc/chrony.conf from package chrony" in caplog.text
  assert manifest(image) == {}


def test_a_package_that_cant_be_reinstalled_leaves_the_file(image, chroot, session, caplog):
  (image / "etc/chrony.conf").write_text("package default")
  chroot.state["owned"]["/etc/chrony.conf"] = "chrony"
  mf.place_files(session, MPROV_URL, str(image), config(CHRONY))
  chroot.state["reinstall_ok"] = False
  mf.place_files(session, MPROV_URL, str(image), config())
  assert (image / "etc/chrony.conf").read_bytes() == SECRET_LINE
  assert "unable to restore /etc/chrony.conf" in caplog.text


def test_a_package_owner_is_remembered_across_builds(image, chroot, session):
  (image / "etc/chrony.conf").write_text("package default")
  chroot.state["owned"]["/etc/chrony.conf"] = "chrony"
  mf.place_files(session, MPROV_URL, str(image), config(CHRONY))
  # once replaced, rpm still says chrony owns it; but the record is kept, not re-queried
  queries = len([c for c in chroot.calls if c[0] == "rpm"])
  mf.place_files(session, MPROV_URL, str(image), config(CHRONY))
  assert len([c for c in chroot.calls if c[0] == "rpm"]) == queries
  assert manifest(image)["/etc/chrony.conf"]["package"] == "chrony"


def test_the_url_is_on_mprov_urls_origin():
  assert mf.origin_url("https://mpcc.example/mprov/", "/mprov/media/x") == "https://mpcc.example/mprov/media/x"
  assert mf.origin_url("http://10.0.0.1:8080/", "/systemimages/c/files/f/") == "http://10.0.0.1:8080/systemimages/c/files/f/"
