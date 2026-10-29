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
import subprocess

import pytest
import yaml

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


@pytest.fixture
def plugin(js, images, commands, monkeypatch):
  js.jobmodules = ["image-update", "mprov-webserver"]
  js.config_data = {"global": {"mprovURL": js.mprovURL, "jobmodules": ["image-update", "mprov-webserver"]}}
  # busybox is fetched with wget (a recorded command), so there's no file to chmod.
  chmod = os.chmod
  monkeypatch.setattr(os, "chmod", lambda path, mode: None if path == "/tmp/busybox" else chmod(path, mode))
  plugin = image_update(js)
  plugin.imageDir = str(images)
  return plugin


@pytest.fixture
def recorded_processes(monkeypatch):
  """ Record calls to subprocess.run instead of running them.  Use after no_processes. """
  runs = []
  def run(argv, **kwargs):
    runs.append((argv, kwargs))
    return subprocess.CompletedProcess(argv, 0)
  monkeypatch.setattr(subprocess, "run", run)
  return runs


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


def test_dracut_gets_only_real_modules_of_the_image_kernel(js, plugin, images, commands):
  # dracut stops at a built-in or unknown module and drops every driver
  # after it (#206), so those are left out of --add-drivers.
  data = image()
  data["osdistro"]["initial_mods"] = "virtio_scsi,virtio_pci,e1000,nosuchmod,,e1000,sd_mod"
  pending_build(js, data)
  (images / "compute/lib/modules/4.18.0-553.el8_10.x86_64").mkdir(parents=True)
  commands.fail = ["-n virtio_pci ", "-n nosuchmod "]
  plugin.handle_jobs()

  assert commands.matching("modinfo -k 4.18.0-553.el8_10.x86_64 -n virtio_pci ")
  dracut = commands.matching(" dracut --regenerate-all")
  assert dracut and all('--add-drivers "virtio_scsi e1000 sd_mod"' in cmd for cmd in dracut)
  assert job_states(js) == [(4, 9)]


def test_dracut_module_needs_to_be_a_module_of_every_image_kernel(js, plugin, images, commands):
  data = image()
  data["osdistro"]["initial_mods"] = "e1000,virtio_pci"
  pending_build(js, data)
  for kernel in ("4.18.0-1.el8.x86_64", "4.18.0-2.el8.x86_64"):
    (images / "compute/lib/modules" / kernel).mkdir(parents=True)
  # built in on only one of the two kernels
  commands.fail = ["-k 4.18.0-2.el8.x86_64 -n virtio_pci "]
  plugin.handle_jobs()

  dracut = commands.matching(" dracut --regenerate-all")
  assert dracut and all('--add-drivers "e1000"' in cmd for cmd in dracut)


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


def test_root_ssh_keys_are_written_into_the_image(js, plugin, images, commands, monkeypatch):
  """
  The distro's rootsshkeys must land inside the image tree with ssh-usable
  permissions; the build host's /root/.ssh must not be touched (#175).
  """
  host_dirs = []
  real_makedirs = os.makedirs
  def spy_makedirs(path, *args, **kwargs):
    if not str(path).startswith(str(images)):
      host_dirs.append(str(path))
    return real_makedirs(path, *args, **kwargs)
  monkeypatch.setattr(os, "makedirs", spy_makedirs)

  data = image()
  data["osdistro"]["rootsshkeys"] = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 key@example\n"
  pending_build(js, data)
  plugin.handle_jobs()

  ssh_dir = images / "compute/root/.ssh"
  keyfile = ssh_dir / "authorized_keys"
  assert keyfile.read_text() == "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 key@example\n"
  assert stat.S_IMODE(os.stat(ssh_dir).st_mode) == 0o700
  assert stat.S_IMODE(os.stat(keyfile).st_mode) == 0o600
  assert "/root/.ssh" not in host_dirs
  assert job_states(js) == [(4, 9)]


def test_unwritable_authorized_keys_fails_the_job(js, plugin, images, commands):
  """
  Key material that cannot be written must fail the image update, not pack
  an image silently without it (#175).
  """
  data = image()
  data["osdistro"]["rootsshkeys"] = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 key@example\n"
  pending_build(js, data)
  # something already owns the .ssh path in the image, and it isn't a directory.
  (images / "compute/root").mkdir(parents=True)
  (images / "compute/root/.ssh").write_text("not a directory")
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]
  assert not commands.matching("tar c")
  # not registered as a host of the image
  assert [w for w in js.session.writes if "systemimages" in w[1]] == []


def test_root_password_is_set_in_the_image(js, plugin, images, commands, recorded_processes):
  data = image()
  data["osdistro"]["rootpw"] = "s3cret"
  pending_build(js, data)
  plugin.handle_jobs()
  passwd_runs = [run for run in recorded_processes if "passwd" in run[0]]
  assert len(passwd_runs) == 1
  argv, kwargs = passwd_runs[0]
  assert argv == ["chroot", f"{images}/compute", "passwd", "root"]
  feed = kwargs["input"]
  if isinstance(feed, bytes):
    feed = feed.decode()
  # the password reaches passwd's stdin: the new password, then the confirmation
  assert feed.count("s3cret") == 2


def test_root_password_never_reaches_process_argv(js, plugin, images, commands, recorded_processes):
  """The password must never be visible in a shell command line or any argv (#168)."""
  data = image()
  data["osdistro"]["rootpw"] = "s3cret"
  pending_build(js, data)
  plugin.handle_jobs()
  assert not [cmd for cmd in commands.run if "s3cret" in cmd]
  for argv, _ in recorded_processes:
    assert all("s3cret" not in arg for arg in argv)


def test_root_password_never_printed_to_daemon_output(js, plugin, images, commands, recorded_processes, capsys):
  """The per-image debug print must not dump rootpw into the daemon log (#168)."""
  data = image()
  data["osdistro"]["rootpw"] = "s3cret"
  pending_build(js, data)
  plugin.handle_jobs()
  assert "s3cret" not in capsys.readouterr().out


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
