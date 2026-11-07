"""
A new EL8 image built by a Rocky 9 job server gets its rpm database in
sqlite from the host's rpm; the image's EL8 rpm can't write sqlite, so the
build converts it with the image's own rpm --rebuilddb before the first
install from inside the image (#329).  Uses test_image_update's fake job
server, image store and recorded commands.
"""
import logging

import pytest

# the fixtures (fake_wget is autouse there, so it's imported to apply here too)
from test_image_update import fake_wget, images, plugin, image, job_states, pending_build  # noqa: F401


def build(js, plugin, images, version, sqlite):
  data = image()
  data["osdistro"]["version"] = version
  if sqlite:
    rpmdb = images / "compute/var/lib/rpm"
    rpmdb.mkdir(parents=True)
    (rpmdb / "rpmdb.sqlite").write_bytes(b"SQLite format 3\x00")
  pending_build(js, data)
  plugin.handle_jobs()
  return images / "compute"


@pytest.mark.parametrize("version", ["8", "8.10"])
def test_a_new_el8_image_with_a_sqlite_rpmdb_is_converted_before_its_first_install(
    js, plugin, images, commands, caplog, version):
  caplog.set_level(logging.INFO)
  img = build(js, plugin, images, version, sqlite=True)
  [rebuild] = commands.matching(f"chroot {img} rpm --rebuilddb")
  hostInstall = commands.matching(f"--installroot={img} --releasever={version} --nogpgcheck groupinstall")[0]
  firstChrootInstall = commands.matching(f"chroot {img}  dnf -y  --releasever={version}  install kernel")[0]
  assert commands.run.index(hostInstall) < commands.run.index(rebuild) < commands.run.index(firstChrootInstall)
  assert "Converting the EL8 image's rpm database" in caplog.text
  assert job_states(js) == [(4, 9)]


def test_an_el9_image_isnt_converted(js, plugin, images, commands):
  build(js, plugin, images, "9", sqlite=True)
  assert not commands.matching("rpm --rebuilddb")


def test_an_el8_image_without_a_sqlite_rpmdb_isnt_converted(js, plugin, images, commands):
  # an EL8 job server's rpm writes bdb, which the image's rpm reads and writes
  build(js, plugin, images, "8", sqlite=False)
  assert not commands.matching("rpm --rebuilddb")


def test_a_failed_conversion_only_warns(js, plugin, images, commands, caplog):
  commands.fail.append("rpm --rebuilddb")
  img = build(js, plugin, images, "8", sqlite=True)
  assert "unable to convert the EL8 image's rpm database" in caplog.text
  # the build goes on to the package install, which has its own fallback (#97)
  assert commands.matching(f"chroot {img}  dnf -y  --releasever=8  install kernel")
  assert job_states(js) == [(4, 9)]
