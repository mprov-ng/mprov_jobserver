"""
Values from mPCC records reach the image build's shell commands; they're
checked or quoted first, so a value that's fine as data but odd as shell
syntax can't change a command (#184).  Uses test_image_update's fake job
server, image store and recorded commands.
"""
import shlex

import pytest

# the fixtures (fake_wget is autouse there, so it's imported to apply here too)
from test_image_update import fake_wget, images, plugin, image, job_states, pending_build  # noqa: F401


def argv(cmd):
  # what a shell would run: the command split the way sh splits it
  return shlex.split(cmd)


@pytest.mark.parametrize("version", ["9; touch /tmp/pwned", "9 --setopt=x", "$(id)", "", "nine"])
def test_a_distro_version_that_isnt_a_number_fails_the_build(js, plugin, commands, version):
  data = image()
  data["osdistro"]["version"] = version
  pending_build(js, data)
  plugin.handle_jobs()
  assert job_states(js)[0] == (3, 9)
  assert not commands.matching("--releasever")


def test_a_dotted_version_still_builds(js, plugin, commands):
  data = image()
  data["osdistro"]["version"] = "8.10"
  pending_build(js, data)
  plugin.handle_jobs()
  assert commands.matching("--releasever=8.10 ")


def test_only_kernel_module_names_reach_dracut(js, plugin, images, commands):
  data = image()
  data["osdistro"]["initial_mods"] = 'e1000,x;true,virtio_net,"a b",$(id)'
  pending_build(js, data)
  (images / "compute/lib/modules/5.14.0").mkdir(parents=True)
  plugin.handle_jobs()
  [dracut] = commands.matching("dracut --regenerate-all")[:1]
  args = argv(dracut)
  assert args[args.index("--add-drivers", args.index("sd_mod")) + 1] == "e1000 virtio_net"
  # and only the real names went to modinfo
  checked = [argv(cmd)[argv(cmd).index("-n") + 1] for cmd in commands.matching("modinfo")]
  assert sorted(set(checked)) == ["e1000", "virtio_net"]


def test_an_image_path_with_a_space_stays_one_argument(js, plugin, images, commands, tmp_path):
  store = tmp_path / "image store"
  store.mkdir()
  plugin.imageDir = str(store)
  pending_build(js)
  plugin.handle_jobs()
  img = f"{store}/compute"
  installs = [argv(cmd) for cmd in commands.matching("--installroot=")]
  assert installs and all(f"--installroot={img}" in args for args in installs)
  # the rhel module's chroot commands; image_update's own (its pip install
  # of the job server, script-runner.sh) are quoted in a follow-up
  chroots = [argv(cmd) for cmd in commands.matching("chroot ") if "mprov_jobserver" not in cmd]
  assert chroots and all(args[0] == "chroot" and args[1] == img for args in chroots)
