"""
image_update builds system images.  updateImageJobservers() first adds this
job server as a host of images it already has; then each pending
image-update job is built by the distro's sub-module (image_update_mod/rhel.py),
the image-gen scripts run in the image, and the filesystem is packed into
<slug>.img.  Every shell command is recorded instead of run (the commands
fixture), so the tests check the commands and the files written.
"""
import copy
import json
import os
import stat

import pytest
import yaml

from conftest import Commands
from mprov_jobserver.plugins.image_update import image_update
from mprov_jobserver.plugins.image_update_mod.rhel import UpdateImage

JOBS = "jobs/?jobserver=1&module=image-update&status=2"

BASEREPO = {
  "id": 12, "name": "Rocky 9 BaseOS", "managed": True, "ostype": {"slug": "rhel"},
  "repo_package_url": "https://dl.rockylinux.org/pub/rocky/9/BaseOS/x86_64/os/",
}
IMAGE = {
  "slug": "compute", "version": 4, "jobservers": [], "customIPXE": "", "needs_rebuild": True,
  "osrepos": [{"id": 13, "name": "EPEL 9", "managed": False, "repo_package_url": "https://epel.example/9/"}],
  "osdistro": {
    "version": "9", "initial_mods": "e1000,virtio_net", "rootpw": "", "rootsshkeys": None,
    "osrepos": [], "extrarepos": [], "baserepo": BASEREPO,
  },
}


def image(**changes):
  data = copy.deepcopy(IMAGE)
  data.update(changes)
  return data


@pytest.fixture
def images(tmp_path):
  path = tmp_path / "images"
  path.mkdir()
  return path


@pytest.fixture(autouse=True)
def fake_wget(monkeypatch):
  """
  A recorded wget that succeeds writes its ``-O`` target, as a real wget
  does, so the build can verify the busybox it fetched (#170).  Tests can
  plant other content at the target by setting ``body``.
  """
  class FakeWget:
    body = b"\x7fELFfake busybox"
  fake = FakeWget()
  record = Commands.__call__
  def call(self, cmd):
    rc = record(self, cmd)
    if rc == 0 and "wget " in cmd and " -O " in cmd:
      with open(cmd.split(" -O ")[1].split()[0], "wb") as f:
        f.write(fake.body)
    return rc
  monkeypatch.setattr(Commands, "__call__", call)
  return fake


@pytest.fixture
def plugin(js, images, commands):
  js.jobmodules = ["image-update", "mprov-webserver"]
  js.config_data = {"global": {"mprovURL": js.mprovURL, "jobmodules": ["image-update", "mprov-webserver"]}}
  plugin = image_update(js)
  plugin.imageDir = str(images)
  return plugin


def no_dependent_jobs(js):
  js.session.routes.update({"jobs/?&module=repo-update&status=1": [], "jobs/?&module=repo-update&status=2": []})


def pending_build(js, data=None):
  """ One image-update job (id 9) for the compute image, and no hosted images yet. """
  no_dependent_jobs(js)
  js.session.routes.update({
    "images/": [],
    JOBS: [{"id": 9, "params": {"imageId": "compute"}}],
    "systemimages/compute/": data or image(),
  })


def job_states(js):
  return [(status, jobid) for _, status, jobid in js.job_updates_by_id if jobid is not None]


# updateImageJobservers()

def hosted(js, images, data, local_version):
  (images / "image-versions").write_text(json.dumps({"compute": local_version}))
  js.session.routes.update({"images/": [{"slug": "compute"}], "systemimages/compute/": data})


def test_adds_itself_as_host_of_an_image_it_has(js, plugin, images):
  hosted(js, images, image(needs_rebuild=False, jobservers=[7]), local_version=4)
  plugin.updateImageJobservers()
  assert js.session.writes == [
    ("PATCH", "systemimages/compute/?addjs", {"slug": "compute", "needs_rebuild": False, "jobservers": [7, 1]}),
  ]


@pytest.mark.parametrize("changes", [
  {"jobservers": [1]},            # already a host
  {"customIPXE": "#!ipxe"},        # served by its own iPXE script, not by job servers
  {"needs_rebuild": True},         # will be rebuilt
])
def test_doesnt_add_itself(js, plugin, images, changes):
  hosted(js, images, image(**dict({"needs_rebuild": False}, **changes)), local_version=4)
  plugin.updateImageJobservers()
  assert js.session.writes == []


def test_pulls_a_newer_image_from_another_host(js, plugin, images):
  hosted(js, images, image(needs_rebuild=False, jobservers=[7]), local_version=3)
  js.session.routes.update({
    "images/compute": b"IMG", "images/compute.initramfs": b"INITRD", "images/compute.vmlinuz": b"KERNEL",
  })
  plugin.updateImageJobservers()
  assert (images / "compute" / "compute.img").read_bytes() == b"IMG"
  assert (images / "compute" / "compute.vmlinuz").read_bytes() == b"KERNEL"


@pytest.mark.xfail(strict=True, reason="a pulled image's version is never recorded, so it's pulled again every pass (#142)")
def test_pulled_image_version_is_recorded(js, plugin, images):
  hosted(js, images, image(needs_rebuild=False, jobservers=[7]), local_version=3)
  js.session.routes.update({
    "images/compute": b"IMG", "images/compute.initramfs": b"INITRD", "images/compute.vmlinuz": b"KERNEL",
  })
  plugin.updateImageJobservers()
  assert json.loads((images / "image-versions").read_text()) == {"compute": 4}


@pytest.mark.xfail(strict=True, reason="updateImageJobservers() ignores a configured imageList (#142)")
def test_configured_image_list_is_used(js, plugin, images):
  plugin.imageList = ["compute"]
  (images / "image-versions").write_text(json.dumps({"compute": 4}))
  js.session.routes["systemimages/compute/"] = image(needs_rebuild=False)
  plugin.updateImageJobservers()
  assert js.session.writes != []


def test_image_list_error_skips_hosting(js, plugin):
  js.session.routes["images/"] = (500, None)
  plugin.updateImageJobservers()
  assert js.session.writes == []


# handle_jobs()

@pytest.mark.parametrize("status", [1, 2])
def test_waits_for_repo_updates(js, plugin, status):
  no_dependent_jobs(js)
  js.session.routes["images/"] = []
  js.session.routes[f"jobs/?&module=repo-update&status={status}"] = [{"id": 1}]
  plugin.handle_jobs()
  assert not plugin.threadOk
  assert js.job_updates == []


def test_no_pending_jobs_builds_nothing(js, plugin, commands):
  no_dependent_jobs(js)
  js.session.routes["images/"] = []
  js.has_jobs = False
  plugin.handle_jobs()
  assert commands.run == []


def test_builds_an_image(js, plugin, images, commands):
  pending_build(js)
  plugin.handle_jobs()
  img = images / "compute"

  # the rhel module wrote a repo file per repo, managed ones pointing at the mPCC
  epel = (img / "etc/yum.repos.d/epel-9-mprov.repo").read_text()
  assert 'baseurl="https://epel.example/9/"' in epel
  base = (img / "etc/yum.repos.d/rocky-9-baseos-mprov.repo").read_text()
  assert "osrepos/12/" in base
  assert commands.matching("groupinstall 'Minimal Install'")
  assert commands.matching(f"--installroot={img}")
  # Rocky 9 gets the plain python3 packages
  assert commands.matching(" python3-pyyaml")
  assert not commands.matching("alternatives --set python3")
  assert commands.matching('--add-drivers "e1000 virtio_net"')

  # the image-gen scripts run through a runonce script-runner job server in the image
  config = yaml.safe_load((img / "tmp/mprov/plugins/script-runner.yaml").read_text())
  assert config is not None
  assert (img / "tmp/mprov/script-runner.sh").exists()
  assert commands.matching(f"{img}/tmp/mprov/script-runner.sh")
  assert not (img / "tmp/mprov/jobserver.yaml").exists()

  # packed, registered with this job server as the host, and recorded
  assert commands.matching(f"tar c -C{img} ")
  assert js.session.writes[-1] == (
    "PATCH", "systemimages/compute/?addjs", {"slug": "compute", "needs_rebuild": False, "jobservers": [1]})
  assert json.loads((images / "image-versions").read_text()) == {"compute": 4}
  assert job_states(js) == [(4, 9)]


def test_script_runner_config_is_runonce_script_runner(js, plugin, images, commands, monkeypatch):
  # Capture jobserver.yaml before it's removed, when the script runs.
  seen = {}
  def system(cmd):
    if cmd.endswith("/tmp/mprov/script-runner.sh"):
      seen["config"] = (images / "compute/tmp/mprov/jobserver.yaml").read_text()
    return commands(cmd)
  monkeypatch.setattr(os, "system", system)
  pending_build(js)
  plugin.handle_jobs()
  config = yaml.load(seen["config"].replace("- !include", "# !include"), Loader=yaml.SafeLoader)
  assert config[0]["global"]["jobmodules"] == ["script-runner"]
  assert config[0]["global"]["runonce"] is True
  assert "- !include plugins/*.yaml" in seen["config"]


def test_root_password_is_set_in_the_image(js, plugin, images, commands):
  data = image()
  data["osdistro"]["rootpw"] = "s3cret"
  pending_build(js, data)
  plugin.handle_jobs()
  [cmd] = commands.matching("passwd root")
  assert f"chroot {images}/compute passwd root" in cmd


# the busybox download and its verification (#170)

def busybox_target(commands):
  """ The path the build fetched busybox to. """
  [download] = commands.matching(" -O ")
  return download.split(" -O ")[1].split()[0]


def test_busybox_is_fetched_to_a_root_only_dir_under_the_image_store(js, plugin, images, commands):
  pending_build(js)
  plugin.handle_jobs()
  target = busybox_target(commands)
  # never the fixed world-writable /tmp/busybox path, where another user
  # could plant a binary for us to run as root. (the image store itself may
  # live under /tmp in CI, so containment in it is the real check.)
  assert target != "/tmp/busybox"
  assert target.startswith(str(images) + os.sep)
  assert stat.S_IMODE(os.stat(os.path.dirname(target)).st_mode) == 0o700
  # and the pack step runs the binary we just fetched and verified, from there
  [pack] = commands.matching("tar c")
  assert pack.startswith(target + " ")


def test_failed_busybox_download_fails_the_build(js, plugin, images, commands):
  commands.fail = ["https://busybox.net"]
  pending_build(js)
  plugin.handle_jobs()
  assert any(status == 3 for _, status, _ in js.job_updates)
  assert not commands.matching("tar c")
  assert [w for w in js.session.writes if "systemimages" in w[1]] == []


def test_busybox_that_is_not_an_elf_binary_is_not_executed(js, plugin, images, commands, fake_wget):
  # a planted or truncated download must fail the build, not be chmod'ed 755
  # and exec'ed as root
  fake_wget.body = b"#!/bin/sh\necho pwned\n"
  pending_build(js)
  plugin.handle_jobs()
  assert any(status == 3 for _, status, _ in js.job_updates)
  assert not commands.matching("tar c")


def test_distro_build_failure_fails_the_job(js, plugin, commands):
  commands.fail = ["groupinstall"]
  pending_build(js)
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]
  assert not commands.matching("tar c")
  # not registered as a host of the image
  assert [w for w in js.session.writes if "systemimages" in w[1]] == []


def test_image_without_a_distro_is_skipped(js, plugin, commands):
  pending_build(js, image(osdistro=None))
  plugin.handle_jobs()
  assert commands.run == []
  assert js.session.writes == []


def test_unknown_ostype_fails_the_job(js, plugin, commands):
  data = image()
  data["osdistro"]["baserepo"]["ostype"]["slug"] = "nosuchos"
  pending_build(js, data)
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]
  assert commands.run == []


@pytest.mark.xfail(strict=True, reason="a failed mprov_jobserver install in the image returns without failing the job (#144)")
def test_jobserver_install_failure_fails_the_job(js, plugin, commands):
  commands.fail = ["pip3 --no-cache-dir install --upgrade mprov_jobserver"]
  pending_build(js)
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]


@pytest.mark.xfail(strict=True, reason="a failed build fails every running job on this job server, not just its own (#143)")
def test_mpcc_error_fails_only_this_modules_jobs(js, plugin):
  pending_build(js)
  js.session.routes["systemimages/compute/"] = (500, None)
  js.session.routes["images/"] = []
  plugin.handle_jobs()
  [(module, status, query)] = [u for u in js.job_updates if u[1] == 3]
  assert "module=image-update" in query


@pytest.mark.xfail(strict=True, reason="handle_jobs() with a configured imageList crashes on list.items() (#144)")
def test_configured_image_list_builds_requested_images(js, plugin, commands):
  plugin.imageList = ["compute"]
  pending_build(js)
  js.session.routes["systemimages/compute/"] = image()
  plugin.handle_jobs()
  assert commands.matching("tar c")


def test_needs_the_webserver(js):
  js.jobmodules = ["image-update"]
  with pytest.raises(SystemExit):
    image_update(js).load_config()


# image_update_mod/rhel.py on its own

@pytest.fixture
def rhel(js, images, commands):
  module = UpdateImage(js)
  module.imageDir = str(images)
  module.jobid = 9
  return module


def test_rhel_8_uses_python38(js, rhel, images, commands):
  rhel.imageDetails = image()
  rhel.imageDetails["osdistro"]["version"] = "8"
  rhel.handle_jobs()
  assert commands.matching(" python38-pyyaml")
  assert commands.matching(f"chroot {images}/compute alternatives --set python3 /usr/bin/python3.8")
  assert rhel.threadOk


def test_rhel_retries_the_package_install_after_rebuilding_the_rpmdb(js, rhel, commands, monkeypatch):
  rhel.imageDetails = image()
  def system(cmd):
    # the first package install fails, the retry works
    first_install = "install kernel" in cmd and not commands.matching("install kernel")
    commands(cmd)
    return 1 if first_install else 0
  monkeypatch.setattr(os, "system", system)
  rhel.handle_jobs()
  assert commands.matching("rpm --rebuilddb")
  assert len(commands.matching("install kernel")) == 2
  assert rhel.threadOk


def test_rhel_package_install_failure_fails_the_job(js, rhel, commands):
  rhel.imageDetails = image()
  commands.fail = ["install kernel", "rpm --rebuilddb"]
  rhel.handle_jobs()
  assert not rhel.threadOk
  assert job_states(js) == [(3, 9)]


def test_rhel_minimal_install_falls_back_to_the_chroot(js, rhel, images, commands):
  rhel.imageDetails = image()
  commands.fail = [f"--installroot={images}/compute --releasever=9 --nogpgcheck groupinstall"]
  rhel.handle_jobs()
  assert commands.matching(f"chroot {images}/compute dnf -y --releasever=9 --nogpgcheck groupinstall")
  assert rhel.threadOk


def test_rhel_links_the_newest_kernel_and_initramfs(js, rhel, images, commands):
  rhel.imageDetails = image()
  rhel.handle_jobs()
  [kernel] = commands.matching("/boot/vmlinuz-*")
  assert "grep -v rescue | sort -r | head -n1" in kernel
  assert kernel.endswith(f"{images}/compute/compute.vmlinuz")
  assert commands.matching(f"{images}/compute/compute.initramfs")
