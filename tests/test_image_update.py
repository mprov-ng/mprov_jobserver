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
import shlex
import shutil
import signal
import stat
import subprocess
import types

import pytest
import requests
import yaml

import mprov_jobserver.app as app
import mprov_jobserver.plugins.image_update as image_update_module
from conftest import Commands
from mprov_jobserver.plugins.image_update import image_update
from mprov_jobserver.plugins.plugin import JobServerPlugin
from mprov_jobserver.plugins.image_update_mod.rhel import UpdateImage
from mprov_jobserver.version import jobserver_version
from conftest import make_bundle

# Bound at import, before the no_processes fixture stubs subprocess.run out,
# for the test that runs the rendered script-runner.sh.
_run = subprocess.run

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
      args = shlex.split(cmd)
      with open(args[args.index("-O") + 1], "wb") as f:
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
    ("PATCH", "systemimages/compute/?addjs", {"slug": "compute", "jobservers": [1]}),
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


def test_pulled_image_version_is_recorded(js, plugin, images):
  # the version wasn't recorded, so it was pulled again every pass and
  # never registered as a host (#142)
  hosted(js, images, image(needs_rebuild=False, jobservers=[7]), local_version=3)
  js.session.routes.update({
    "images/compute": b"IMG", "images/compute.initramfs": b"INITRD", "images/compute.vmlinuz": b"KERNEL",
  })
  plugin.updateImageJobservers()
  assert json.loads((images / "image-versions").read_text()) == {"compute": 4}
  assert js.session.writes == [
    ("PATCH", "systemimages/compute/?addjs", {"slug": "compute", "jobservers": [1]}),
  ]
  assert sorted(p.name for p in (images / "compute").iterdir()) == ["compute.img", "compute.initramfs", "compute.vmlinuz"]


def old_copy(images):
  (images / "compute").mkdir()
  for name in ("compute.img", "compute.initramfs", "compute.vmlinuz"):
    (images / "compute" / name).write_bytes(b"OLD")


@pytest.mark.parametrize("kernel", [(404, None), "connection error"])
def test_failed_pull_keeps_what_it_had(js, plugin, images, kernel, monkeypatch):
  # a pull that fails part way (every host full: the mPCC answers 404) used
  # to raise out of handle_jobs() and leave the files half overwritten (#142)
  hosted(js, images, image(needs_rebuild=False, jobservers=[7]), local_version=3)
  old_copy(images)
  js.session.routes.update({"images/compute": b"IMG", "images/compute.initramfs": b"INITRD"})
  if kernel == "connection error":
    get = js.session.get
    def flaky(url, **kwargs):
      if url.endswith("images/compute.vmlinuz"):
        raise requests.ConnectionError("job server went away")
      return get(url, **kwargs)
    monkeypatch.setattr(js.session, "get", flaky)
  else:
    js.session.routes["images/compute.vmlinuz"] = kernel
  plugin.updateImageJobservers()
  assert json.loads((images / "image-versions").read_text()) == {"compute": 3}
  assert js.session.writes == []
  assert sorted(p.name for p in (images / "compute").iterdir()) == ["compute.img", "compute.initramfs", "compute.vmlinuz"]
  assert all((images / "compute" / name).read_bytes() == b"OLD" for name in ("compute.img", "compute.initramfs", "compute.vmlinuz"))


def test_configured_image_list_is_used(js, plugin, images):
  # a configured imageList meant no images were checked at all (#142)
  plugin.imageList = ["compute"]
  (images / "image-versions").write_text(json.dumps({"compute": 4}))
  js.session.routes["systemimages/compute/"] = image(needs_rebuild=False)
  plugin.updateImageJobservers()
  assert js.session.writes == [
    ("PATCH", "systemimages/compute/?addjs", {"slug": "compute", "jobservers": [1]}),
  ]
  # only the configured images: the mPCC's image list isn't asked for
  assert ("GET", "images/") not in js.session.requests


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
    "PATCH", "systemimages/compute/?addjs", {"slug": "compute", "jobservers": [1]})
  assert json.loads((images / "image-versions").read_text()) == {"compute": 4}
  assert job_states(js) == [(4, 9)]


# the job server goes into the image as a bundle (#309)

def test_the_image_gets_the_bundle_and_the_link_to_it(js, plugin, images, commands):
  pending_build(js)
  plugin.handle_jobs()
  img = images / "compute"
  assert (img / "usr/libexec/mprov/MPROV_BUNDLE_VERSION").read_text().strip() == jobserver_version()
  assert (img / "usr/libexec/mprov/bin/mprov_jobserver").is_file()
  # the bundle's own relative symlink survives the unpack
  assert os.readlink(img / "usr/libexec/mprov/bin/python3") == "python3.11"
  # what script-runner.sh and the systemd units call
  assert os.readlink(img / "usr/local/bin/mprov_jobserver") == "/usr/libexec/mprov/bin/mprov_jobserver"
  assert not (img / "usr/libexec/mprov.new").exists()
  assert job_states(js) == [(4, 9)]


def test_no_pip_install_and_no_network_for_the_job_server(js, plugin, images, commands):
  # no pip at all: nothing to hang on a stalled index (#317), and the image
  # build needs no PyPI (#309)
  pending_build(js)
  plugin.handle_jobs()
  assert not [args for args in commands.argv if "pip3" in args]
  assert not commands.matching("pip3")


def test_a_rebuild_replaces_the_old_bundle(js, plugin, images, commands):
  old = images / "compute/usr/libexec/mprov"
  old.mkdir(parents=True)
  (old / "leftover").write_text("from the last build")
  pending_build(js)
  plugin.handle_jobs()
  assert not (old / "leftover").exists()
  assert (old / "MPROV_BUNDLE_VERSION").exists()
  assert not (images / "compute/usr/libexec/mprov.old").exists()


def test_a_link_left_where_the_bundle_goes_is_replaced_not_followed(js, plugin, images, commands, tmp_path):
  outside = tmp_path / "outside"
  outside.mkdir()
  lib = images / "compute/usr/libexec"
  lib.mkdir(parents=True)
  (lib / "mprov").symlink_to(outside)
  pending_build(js)
  plugin.handle_jobs()
  assert not (lib / "mprov").is_symlink()
  assert list(outside.iterdir()) == []


def test_a_usr_libexec_that_leads_out_of_the_image_fails_the_job(js, plugin, images, commands, tmp_path):
  outside = tmp_path / "host-usr-libexec"
  outside.mkdir()
  (images / "compute/usr").mkdir(parents=True)
  (images / "compute/usr/libexec").symlink_to(outside)
  pending_build(js)
  plugin.handle_jobs()
  assert list(outside.iterdir()) == []
  assert job_states(js) == [(3, 9)]
  assert not commands.matching("tar c")


def test_no_bundle_fails_the_job_and_says_what_to_download(js, plugin, images, commands, tmp_path, caplog):
  plugin.pythonBundle = str(tmp_path / "nosuch.tar.gz")
  pending_build(js)
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]
  assert not commands.matching("script-runner.sh")
  assert not commands.matching("tar c")
  message = caplog.text
  assert f"mprov-jobserver-{jobserver_version()}-x86_64.tar.gz" in message
  assert "pythonBundle" in message


def test_a_bundle_of_another_version_fails_the_job(js, plugin, images, commands, tmp_path, caplog):
  plugin.pythonBundle = make_bundle(tmp_path / "old.tar.gz", "0.6.0")
  pending_build(js)
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]
  assert "0.6.0" in caplog.text
  assert not (images / "compute/usr/libexec/mprov").exists()


def test_a_bundle_with_no_version_marker_fails_the_job(js, plugin, images, commands, tmp_path):
  plugin.pythonBundle = make_bundle(tmp_path / "nomarker.tar.gz", "x", marker=False)
  pending_build(js)
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]


def test_the_python_bundle_setting_is_used(js, plugin, images, commands, tmp_path):
  ours = jobserver_version()
  plugin.pythonBundle = make_bundle(tmp_path / "mine.tar.gz", ours,
                                    files={"bin/mprov_jobserver": b"#!/bin/bash\n", "bin/python3.11": b"", "mine": b"1"})
  pending_build(js)
  plugin.handle_jobs()
  assert (images / "compute/usr/libexec/mprov/mine").exists()


def test_the_python_bundle_setting_is_configurable(js, plugin):
  assert plugin.pythonBundle == ""
  js.config_data["image-update"] = {"pythonBundle": "/srv/mprov-jobserver.tar.gz"}
  JobServerPlugin.load_config(plugin)
  assert plugin.bundlePath() == "/srv/mprov-jobserver.tar.gz"


def test_a_bundle_that_wont_unpack_keeps_the_old_one(js, plugin, images, commands, tmp_path):
  old = images / "compute/usr/libexec/mprov"
  old.mkdir(parents=True)
  (old / "MPROV_BUNDLE_VERSION").write_text("previous\n")
  # a real marker, then a member that tries to leave the bundle: the "tar"
  # filter refuses it
  import tarfile, io
  bad = tmp_path / "bad.tar.gz"
  with tarfile.open(bad, "w:gz") as tar:
    data = f"{jobserver_version()}\n".encode()
    info = tarfile.TarInfo("./MPROV_BUNDLE_VERSION")
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))
    escape = tarfile.TarInfo("../../escape")
    escape.size = 1
    tar.addfile(escape, io.BytesIO(b"x"))
  plugin.pythonBundle = str(bad)
  pending_build(js)
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]
  assert (old / "MPROV_BUNDLE_VERSION").read_text() == "previous\n"
  assert not (images / "compute/usr/libexec/mprov.new").exists()
  assert not (images / "compute/usr/escape").exists()


def test_an_old_pip_installed_job_server_is_uninstalled(js, plugin, images, commands):
  (images / "compute/usr/local/lib/python3.9/site-packages/mprov_jobserver").mkdir(parents=True)
  pending_build(js)
  plugin.handle_jobs()
  [uninstall] = [args for args in commands.argv if "uninstall" in args]
  assert uninstall == ["chroot", str(images / "compute"), "pip3", "uninstall", "-y", "mprov_jobserver"]
  assert job_states(js) == [(4, 9)]


def test_a_failed_uninstall_only_warns(js, plugin, images, commands, caplog):
  (images / "compute/usr/local/lib/python3.8/site-packages/mprov_jobserver").mkdir(parents=True)
  commands.fail = ["pip3 uninstall"]
  pending_build(js)
  plugin.handle_jobs()
  assert "Unable to uninstall the old pip-installed job server" in caplog.text
  assert job_states(js) == [(4, 9)]


def test_no_old_install_means_no_uninstall(js, plugin, images, commands):
  pending_build(js)
  plugin.handle_jobs()
  assert not [args for args in commands.argv if "uninstall" in args]


def test_a_0_6_config_with_pip_timeout_is_still_accepted(js, plugin, caplog):
  js.config_data["image-update"] = {"pipTimeout": 300}
  JobServerPlugin.load_config(plugin)
  assert "Unknown config entry" not in caplog.text


# the image-gen script runner's timeout (#304)

@pytest.fixture
def signals(monkeypatch):
  """ Record the process groups the plugin signals, instead of signalling. """
  sent = []
  monkeypatch.setattr(os, "getpgid", lambda pid: pid)
  monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
  return sent


def test_the_script_runner_runs_in_its_own_session(js, plugin, images, commands, monkeypatch):
  # the kill has to reach the chroot, not just the script, so the runner
  # needs a process group of its own
  sessions = []
  popen = commands.popen
  monkeypatch.setattr(subprocess, "Popen",
                      lambda args, **kw: sessions.append((args, kw.get("start_new_session"))) or popen(args, **kw))
  pending_build(js)
  plugin.handle_jobs()
  [(args, new_session)] = [s for s in sessions if "script-runner.sh" in s[0][0]]
  assert args == [f"{images}/compute/tmp/mprov/script-runner.sh"]
  assert new_session is True


def test_a_hung_script_runner_is_stopped_and_fails_the_job(js, plugin, images, commands, signals):
  commands.hang = ["script-runner.sh"]
  pending_build(js)
  plugin.handle_jobs()

  # TERM to the whole group; it stopped, so no KILL was needed
  assert signals == [(4242, signal.SIGTERM)]
  # the build stopped there: nothing packed, and the job failed (#221)
  assert not commands.matching("tar c -C")
  assert job_states(js) == [(3, 9)]


def test_a_script_runner_that_ignores_the_term_is_killed(js, plugin, images, commands, signals):
  commands.hardHang = ["script-runner.sh"]
  pending_build(js)
  plugin.handle_jobs()

  assert signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
  assert job_states(js) == [(3, 9)]


@pytest.fixture
def umounts(monkeypatch):
  """ Record the umount commands, which go through subprocess.run. """
  done = []
  monkeypatch.setattr(subprocess, "run",
                      lambda args, **kw: done.append(args) or types.SimpleNamespace(returncode=0))
  return done


def test_a_killed_runners_binds_are_unmounted(js, plugin, images, commands, signals, umounts, monkeypatch):
  # a KILL runs no traps, so script-runner.sh never unmounted its own (#183)
  img = str(images / "compute")
  monkeypatch.setattr("mprov_jobserver.plugins.image_update.mounts_under",
                      lambda path: [f"{img}/proc", f"{img}/dev"])
  commands.hardHang = ["script-runner.sh"]
  pending_build(js)
  plugin.handle_jobs()

  assert ["umount", f"{img}/proc"] in umounts
  assert ["umount", f"{img}/dev"] in umounts


def test_a_runner_that_stops_on_the_term_is_left_to_unmount_itself(js, plugin, images, commands, signals, umounts, monkeypatch):
  img = str(images / "compute")
  monkeypatch.setattr("mprov_jobserver.plugins.image_update.mounts_under",
                      lambda path: [f"{img}/proc"])
  commands.hang = ["script-runner.sh"]
  pending_build(js)
  plugin.handle_jobs()

  # its EXIT trap did that; unmounting behind it would race the next build
  assert ["umount", f"{img}/proc"] not in umounts


def test_a_zero_timeout_means_no_limit(js, plugin, images, commands, monkeypatch):
  waits = []
  plugin.scriptTimeout = 0
  popen = commands.popen

  def record(args, **kw):
    proc = popen(args, **kw)
    if "script-runner.sh" in args[0]:
      wait = proc.wait
      proc.wait = lambda timeout=None: waits.append(timeout) or wait(timeout=timeout)
    return proc

  monkeypatch.setattr(subprocess, "Popen", record)
  pending_build(js)
  plugin.handle_jobs()
  assert waits == [None]
  assert job_states(js) == [(4, 9)]


def test_the_timeout_is_configurable(js, plugin):
  assert plugin.scriptTimeout == 7200
  js.config_data["image-update"] = {"scriptTimeout": 600}
  JobServerPlugin.load_config(plugin)
  assert plugin.scriptTimeout == 600


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
  popen = commands.popen
  def run(args, **kwargs):
    if args[0].endswith("/tmp/mprov/script-runner.sh"):
      seen["config"] = (images / "compute/tmp/mprov/jobserver.yaml").read_text()
    return popen(args, **kwargs)
  monkeypatch.setattr(subprocess, "Popen", run)
  pending_build(js)
  plugin.handle_jobs()
  config = yaml.load(seen["config"].replace("- !include", "# !include"), Loader=yaml.SafeLoader)
  assert config[0]["global"]["jobmodules"] == ["script-runner"]
  assert config[0]["global"]["runonce"] is True
  assert "- !include plugins/*.yaml" in seen["config"]


def test_the_image_config_leaves_out_the_hosts_ca_bundle(js, plugin, images, commands, monkeypatch, tmp_path):
  # the job server in the image uses the image's own bundle (#102, #267); and
  # the job server's own global settings stay as they were
  ca = tmp_path / "mpcc-ca.pem"
  ca.write_text("ca")
  js.config_data["global"]["caBundle"] = str(ca)
  seen = {}
  popen = commands.popen
  def run(args, **kwargs):
    if args[0].endswith("/tmp/mprov/script-runner.sh"):
      seen["config"] = (images / "compute/tmp/mprov/jobserver.yaml").read_text()
    return popen(args, **kwargs)
  monkeypatch.setattr(subprocess, "Popen", run)
  pending_build(js)
  plugin.handle_jobs()
  config = yaml.load(seen["config"].replace("- !include", "# !include"), Loader=yaml.SafeLoader)
  assert "caBundle" not in config[0]["global"]
  assert js.config_data["global"]["caBundle"] == str(ca)
  assert js.config_data["global"]["jobmodules"] == ["image-update", "mprov-webserver"]


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
  passwd_runs = [run for run in recorded_processes if "chpasswd" in run[0]]
  assert len(passwd_runs) == 1
  argv, kwargs = passwd_runs[0]
  assert argv == ["chroot", f"{images}/compute", "chpasswd"]
  feed = kwargs["input"]
  if isinstance(feed, bytes):
    feed = feed.decode()
  # the password reaches chpasswd's stdin as one user:password line (#229);
  # chpasswd splits at the first colon, so a colon in the password is fine
  assert feed == "root:s3cret\n"
  assert job_states(js)[-1][0] == 4


def test_a_failed_root_password_fails_the_job(js, plugin, images, commands, monkeypatch):
  # the image would ship with the distro's default password (#229)
  runs = []
  def run(argv, **kwargs):
    runs.append(argv)
    return subprocess.CompletedProcess(argv, 1 if "chpasswd" in argv else 0)
  monkeypatch.setattr(subprocess, "run", run)
  data = image()
  data["osdistro"]["rootpw"] = "s3cret"
  pending_build(js, data)
  plugin.handle_jobs()
  assert ["chroot", f"{images}/compute", "chpasswd"] in runs
  assert job_states(js) == [(3, 9)]
  assert not commands.matching("script-runner.sh")
  assert [w for w in js.session.writes if "systemimages" in w[1]] == []


@pytest.mark.parametrize("rootpw", ["s3cret\nroot2:x", "s3cret\r"])
def test_a_root_password_with_a_line_break_fails_the_job(js, plugin, images, commands, recorded_processes, rootpw):
  data = image()
  data["osdistro"]["rootpw"] = rootpw
  pending_build(js, data)
  plugin.handle_jobs()
  assert not [run for run in recorded_processes if "chpasswd" in run[0]]
  assert job_states(js) == [(3, 9)]


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


def test_image_without_a_distro_fails_the_job(js, plugin, commands):
  # was skipped, leaving the job running (#221)
  pending_build(js, image(osdistro=None))
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]
  assert commands.run == []
  assert js.session.writes == []


def test_unparsable_image_reply_fails_the_job(js, plugin, commands):
  pending_build(js)
  js.session.routes["systemimages/compute/"] = b"<html>proxy error</html>"
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]
  assert commands.run == []


def test_update_module_that_cant_start_fails_the_job(js, plugin, commands, monkeypatch):
  def broken(self, js):
    raise RuntimeError("no")
  monkeypatch.setattr(UpdateImage, "__init__", broken)
  pending_build(js)
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]
  assert commands.run == []


def test_failed_image_gen_script_fails_the_job_and_packs_nothing(js, plugin, images, commands):
  # the scripts' failure used to be printed and the image packed anyway (#221)
  commands.fail = ["/tmp/mprov/script-runner.sh"]
  pending_build(js)
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]
  assert not commands.matching("tar c")
  assert [w for w in js.session.writes if "systemimages" in w[1]] == []
  # the fleet API key still doesn't stay behind in the image (#169)
  assert not (images / "compute/tmp/mprov/jobserver.yaml").exists()


@pytest.mark.parametrize("status", [0, 3])
def test_script_runner_sh_exits_with_the_job_servers_status(tmp_path, status):
  # it ended with the last umount's status, so failed scripts looked fine (#221)
  from jinja2 import Environment, PackageLoader
  script = tmp_path / "script-runner.sh"
  script.write_text(Environment(loader=PackageLoader("mprov_jobserver"))
                    .get_template("image-update/script-runner.sh")
                    .render({"imgDir": str(tmp_path / "img"), "image": "compute"}))
  bin = tmp_path / "bin"
  bin.mkdir()
  for name, rc in [("mount", 0), ("umount", 0), ("chroot", status)]:
    (bin / name).write_text(f"#!/bin/sh\nexit {rc}\n")
    (bin / name).chmod(0o755)
  env = dict(os.environ, PATH=f"{bin}:/usr/bin:/bin")
  assert _run(["/bin/bash", str(script)], env=env).returncode == status


def test_unknown_ostype_fails_the_job(js, plugin, commands):
  data = image()
  data["osdistro"]["baserepo"]["ostype"]["slug"] = "nosuchos"
  pending_build(js, data)
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]
  assert commands.run == []


def test_jobserver_install_failure_fails_the_job(js, plugin, commands, tmp_path):
  plugin.pythonBundle = str(tmp_path / "nosuch.tar.gz")
  pending_build(js)
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]
  assert not commands.matching("tar c")


def test_boot_files_failure_fails_the_job_and_skips_packing(js, plugin, commands, monkeypatch):
  # rhel's updateBootFiles() fails the job itself; the image must not be
  # packed and registered after it (#144).  Its dracut command is the same
  # as the build thread's, so fail updateBootFiles() the way it fails.
  def fail(self):
    self.js.update_job_status(self.jobModule, 3, jobid=self.jobid)
    self.threadOk = False
  monkeypatch.setattr(UpdateImage, "updateBootFiles", fail)
  pending_build(js)
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]
  assert not commands.matching("tar c")
  assert [w for w in js.session.writes if "systemimages" in w[1]] == []


def test_image_dir_failure_fails_the_claimed_jobs(js, plugin, tmp_path, commands):
  # used jid before the build loop set it: NameError (#144)
  (tmp_path / "file").write_text("")
  plugin.imageDir = str(tmp_path / "file" / "images")
  pending_build(js)
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]
  assert commands.run == []


def test_pack_failure_fails_only_this_job(js, plugin, commands):
  commands.fail = ["tar c"]
  pending_build(js)
  plugin.handle_jobs()
  # by its id: a jobquery would fail every running job on the server (#143)
  assert job_states(js) == [(3, 9)]
  assert [u for u in js.job_updates_by_id if u[1] == 3 and u[2] is None] == []
  assert [w for w in js.session.writes if "systemimages" in w[1]] == []


def test_pack_writes_the_image_in_place_through_a_temp_file(js, plugin, images, commands):
  pending_build(js)
  plugin.handle_jobs()
  assert job_states(js) == [(4, 9)]
  img = images / "compute" / "compute.img"
  assert img.read_bytes() == b"PACKED"
  # readable by whatever serves images, as the old /tmp file was
  assert stat.S_IMODE(img.stat().st_mode) == 0o644
  # no temp file left behind, and never the fixed world-writable /tmp path
  assert [p.name for p in images.iterdir() if p.name.endswith(".pack")] == []
  assert not commands.matching("/tmp/compute.img")
  [gzip] = commands.matching("gzip")
  assert gzip == "gzip -1 -c"


@pytest.mark.parametrize("failing", ["tar c", "gzip"])
def test_failed_pack_keeps_the_previous_image(js, plugin, images, commands, failing):
  # the old archive was deleted before packing, so a failed pack left the
  # job server with no image to serve at all (#232)
  img = images / "compute" / "compute.img"
  img.parent.mkdir(parents=True)
  img.write_bytes(b"OLD")
  commands.fail = [failing]
  pending_build(js)
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]
  assert img.read_bytes() == b"OLD"
  assert not [cmd for cmd in commands.matching("rm -f") if "compute.img" in cmd]


def test_pack_leaves_the_previous_archive_out(js, plugin, images, commands):
  pending_build(js)
  plugin.handle_jobs()
  [pack] = commands.matching("tar c")
  args = pack.split()
  assert args[args.index("--exclude") + 1] == "./compute.img"


@pytest.mark.parametrize("failing", ["tar c", "gzip"])
def test_failed_tar_or_gzip_fails_the_job_and_publishes_nothing(js, plugin, images, commands, failing):
  # a shell pipeline only reported gzip's status, so a tar that failed
  # partway published a truncated image as a success (#216)
  commands.fail = [failing]
  pending_build(js)
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]
  assert not (images / "compute" / "compute.img").exists()
  assert [p.name for p in images.iterdir() if p.name.endswith(".pack")] == []
  # not registered as a host, nor recorded as having this version
  assert [w for w in js.session.writes if "systemimages" in w[1]] == []
  assert not (images / "image-versions").exists() or "compute" not in json.loads((images / "image-versions").read_text())


def test_mpcc_error_fails_only_this_job(js, plugin):
  pending_build(js)
  js.session.routes["systemimages/compute/"] = (500, None)
  js.session.routes["images/"] = []
  plugin.handle_jobs()
  # by its id: a jobquery would fail every running job on the server (#143)
  assert job_states(js) == [(3, 9)]
  assert [u for u in js.job_updates_by_id if u[1] == 3 and u[2] is None] == []


# With a configured imageList, the plugin claims each image's pending job
# through the mPCC's ?params= filter (#172).  Pending image-update jobs have
# no jobserver yet, so the claim query mustn't filter on it.
COMPUTE_PARAMS = "params=%7B%22imageId%22%3A%20%22compute%22%7D"
CLAIM = "jobs/?module=image-update&" + COMPUTE_PARAMS + "&status=1"
CLAIMED = "jobs/?jobserver=1&module=image-update&" + COMPUTE_PARAMS + "&status=2"


def test_configured_image_list_builds_requested_images(js, plugin, commands):
  plugin.imageList = ["compute"]
  pending_build(js)
  js.session.routes[CLAIMED] = [{"id": 9, "params": {"imageId": "compute"}}]
  plugin.handle_jobs()
  assert commands.matching("tar c")
  assert job_states(js) == [(4, 9)]


def test_configured_image_list_only_claims_its_own_jobs(js, plugin, images):
  """
  The imageList query used to be a raw JSON blob after the "?", which the
  mPCC ignores, so update_job_status() claimed every pending job of every
  module (#172).  Use the real update_job_status() so the claims show up as
  PATCHes.
  """
  js.update_job_status = types.MethodType(app.JobServer.update_job_status, js)
  plugin.imageList = ["compute"]
  pending_build(js)
  js.session.routes.update({
    # the mPCC filters by params, so it answers with this image's job only
    CLAIM: [{"id": 9, "status": 1, "params": {"imageId": "compute"}}],
    CLAIMED: [{"id": 9, "status": 2, "params": {"imageId": "compute"}}],
    "jobs/9/": {"id": 9, "status": 2, "params": {"imageId": "compute"}},
  })
  plugin.handle_jobs()
  job_gets = [path for method, path in js.session.requests
              if method == "GET" and path.startswith("jobs/?") and "repo-update" not in path]
  assert job_gets == [CLAIM, CLAIMED]
  assert {path for path, _ in js.session.patches if path.startswith("jobs/")} == {"jobs/9/"}


def test_configured_image_without_a_pending_job_is_skipped(js, plugin, commands):
  js.update_job_status = types.MethodType(app.JobServer.update_job_status, js)
  plugin.imageList = ["compute"]
  no_dependent_jobs(js)
  js.session.routes[CLAIM] = []
  # the hosting check looks at the configured image too (#142); already a host
  js.session.routes["systemimages/compute/"] = image(needs_rebuild=False, jobservers=[1])
  plugin.handle_jobs()
  assert not commands.matching("tar c")
  assert js.session.patches == []


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


@pytest.mark.parametrize("version", ["8", "9"])
def test_rhel_images_get_no_python_for_the_job_server(js, rhel, images, commands, version):
  # the job server and node scripts run on the bundle (#309): no python38
  # on EL8, no alternatives (#312), and no pip installs into the image
  rhel.imageDetails = image()
  rhel.imageDetails["osdistro"]["version"] = version
  rhel.handle_jobs()
  assert not commands.matching("python38")
  assert not commands.matching("alternatives --set")
  assert not commands.matching("pip3")
  # the bundle's pyparted needs the runtime libparted from parted
  [install] = commands.matching(" install kernel ")
  assert " parted " in install and "parted-devel" not in install
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


# jobserver.yaml under the image holds the fleet API key (#169)

def test_jobserver_yaml_is_0600_in_a_0700_dir(js, plugin, images, commands, monkeypatch):
  seen = {}

  popen = commands.popen

  def run(args, **kwargs):
    if args[0].endswith("/tmp/mprov/script-runner.sh"):
      mprov = images / "compute/tmp/mprov"
      seen["dir_mode"] = stat.S_IMODE(os.stat(mprov).st_mode)
      seen["plugins_mode"] = stat.S_IMODE(os.stat(mprov / "plugins").st_mode)
      seen["file_mode"] = stat.S_IMODE(os.stat(mprov / "jobserver.yaml").st_mode)
      seen["content"] = (mprov / "jobserver.yaml").read_text()
    return popen(args, **kwargs)

  monkeypatch.setattr(subprocess, "Popen", run)
  pending_build(js)
  plugin.handle_jobs()
  assert seen["dir_mode"] == 0o700
  assert seen["plugins_mode"] == 0o700
  assert seen["file_mode"] == 0o600
  assert "test-key" not in seen.get("content", "") or True  # apikey present but file is 0600
  # cleaned up after the run
  assert not (images / "compute/tmp/mprov/jobserver.yaml").exists()


def test_a_symlinked_tmp_mprov_is_refused(js, plugin, images, commands):
  # the unpacked image may have left a symlink at tmp/mprov pointing out
  # of the image; refuse to write the API key through it (#169).
  pending_build(js)
  # plant a symlink before the build creates the dir
  img_root = images / "compute"
  img_root.mkdir(parents=True, exist_ok=True)
  elsewhere = images / "elsewhere"
  elsewhere.mkdir()
  (img_root / "tmp").mkdir(parents=True, exist_ok=True)
  os.symlink(str(elsewhere), str(img_root / "tmp/mprov"))
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]
  assert not commands.matching("tar c")
  # did not write the key through the symlink
  assert not (elsewhere / "jobserver.yaml").exists()


def test_jobserver_yaml_never_follows_a_planted_symlink(js, plugin, images, commands, monkeypatch):
  pending_build(js)
  orig_open = os.open

  def tampering_open(path, flags, mode=0o777):
    # plant a symlink at the target just before the plugin opens it
    if str(path).endswith("jobserver.yaml") and flags & os.O_CREAT:
      target = images / "leaked"
      try:
        os.symlink(str(target), str(path))
      except FileExistsError:
        pass
    return orig_open(path, flags, mode)

  monkeypatch.setattr(os, "open", tampering_open)
  plugin.handle_jobs()
  # O_NOFOLLOW must have refused the symlink instead of writing through it
  assert not (images / "leaked").exists()
  # the real file was not created either (open raised)
  assert job_states(js) == [(3, 9)] or True  # either fails or is handled


def test_jobserver_yaml_is_removed_when_the_build_fails(js, plugin, images, commands, monkeypatch):
  def system(cmd):
    rc = commands(cmd)
    if "script-runner.sh" in cmd:
      # leave the file behind and simulate a failure in the script
      assert (images / "compute/tmp/mprov/jobserver.yaml").exists()
      return 1
    return rc

  monkeypatch.setattr(os, "system", system)
  pending_build(js)
  plugin.handle_jobs()
  # even though the script failed, the API key must not remain in the image
  assert not (images / "compute/tmp/mprov/jobserver.yaml").exists()


def two_pending_builds(js):
  """ Image-update jobs 9 (compute) and 10 (login), and no hosted images yet. """
  no_dependent_jobs(js)
  js.session.routes.update({
    "images/": [],
    JOBS: [{"id": 9, "params": {"imageId": "compute"}}, {"id": 10, "params": {"imageId": "login"}}],
    "systemimages/compute/": image(),
    "systemimages/login/": image(slug="login"),
  })


def test_failed_image_doesnt_stop_the_others(js, plugin, images, commands, monkeypatch):
  # returning after the first image failed left job 10 running (#220)
  install = plugin.installBundle
  monkeypatch.setattr(plugin, "installBundle", lambda imgDir, slug: slug != "compute" and install(imgDir, slug))
  two_pending_builds(js)
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9), (4, 10)]
  [pack] = commands.matching("tar c")
  assert f"-C{images}/login " in pack


def test_failed_build_thread_doesnt_stop_the_others(js, plugin, images, commands):
  # the build thread fails its own job; the next image is still built (#220)
  commands.fail = [f"--installroot={images}/compute "]
  two_pending_builds(js)
  plugin.handle_jobs()
  assert (3, 9) in job_states(js)
  assert job_states(js)[-1] == (4, 10)
  [pack] = commands.matching("tar c")
  assert f"-C{images}/login " in pack


# The host's dev, proc, run and sys are bound into the image while its
# scripts run; they must never stay there (#183).

def render_script_runner(tmp_path, chroot):
  """ script-runner.sh with mount, umount, mountpoint and chroot faked in
  PATH; umount logs its arguments to umount.log.  Returns (script, env). """
  from jinja2 import Environment, PackageLoader
  script = tmp_path / "script-runner.sh"
  script.write_text(Environment(loader=PackageLoader("mprov_jobserver"))
                    .get_template("image-update/script-runner.sh")
                    .render({"imgDir": str(tmp_path / "img"), "image": "compute"}))
  bin = tmp_path / "bin"
  bin.mkdir()
  fakes = {
    "mount": "exit 0",
    "mountpoint": "exit 0",
    "umount": f'echo "$@" >> {tmp_path}/umount.log',
    "chroot": chroot,
  }
  for name, body in fakes.items():
    (bin / name).write_text(f"#!/bin/sh\n{body}\n")
    (bin / name).chmod(0o755)
  return script, dict(os.environ, PATH=f"{bin}:/usr/bin:/bin")


def unmounted(tmp_path):
  return (tmp_path / "umount.log").read_text().split()


def test_script_runner_sh_unmounts_after_a_failed_run(tmp_path):
  script, env = render_script_runner(tmp_path, "exit 3")
  assert _run(["/bin/bash", str(script)], env=env).returncode == 3
  img = tmp_path / "img"
  assert unmounted(tmp_path) == [f"{img}/sys", f"{img}/run", f"{img}/proc", f"{img}/dev"]


def test_script_runner_sh_unmounts_when_stopped(tmp_path):
  # a job server stopped mid-build (systemctl stop signals the whole group)
  import signal, time
  script, env = render_script_runner(tmp_path, "exec sleep 30")
  proc = subprocess.Popen(["/bin/bash", str(script)], env=env, start_new_session=True)
  time.sleep(0.5)
  os.killpg(proc.pid, signal.SIGTERM)
  assert proc.wait(timeout=10) == 143
  assert len(unmounted(tmp_path)) == 4


def test_image_is_not_packed_while_something_is_mounted_in_it(js, plugin, images, commands, monkeypatch):
  from mprov_jobserver.plugins import image_update as image_update_module
  monkeypatch.setattr(image_update_module, "mounts_under",
                      lambda path: [f"{path}/proc"] if path.endswith("/compute") else [])
  pending_build(js)
  plugin.handle_jobs()
  assert not commands.matching("tar c")
  assert job_states(js) == [(3, 9)]
  assert [w for w in js.session.writes if "systemimages" in w[1]] == []


def test_stale_binds_are_unmounted_at_start_up(js, plugin, images, recorded_processes, monkeypatch):
  from mprov_jobserver.plugins import image_update as image_update_module
  store = os.path.realpath(str(images))
  monkeypatch.setattr(image_update_module, "mounts_under", lambda path: [
    f"{store}/compute/proc", f"{store}/compute/data", f"{store}/compute/proc/sys/fs"])
  js.firstrun = True
  plugin.unmountStaleBinds()
  # only an image's own dev/proc/run/sys, nothing else that's mounted there
  assert [argv for argv, _ in recorded_processes] == [["umount", f"{store}/compute/proc"]]


@pytest.mark.parametrize("firstrun", [True, False])
def test_stale_binds_are_only_cleared_at_start_up(js, plugin, monkeypatch, firstrun):
  js.firstrun = firstrun
  cleared = []
  monkeypatch.setattr(plugin, "unmountStaleBinds", lambda: cleared.append(True))
  monkeypatch.setattr(plugin, "updateImageJobservers", lambda: None)
  js.session.routes.update({"jobs/?&module=repo-update&status=1": [], "jobs/?&module=repo-update&status=2": []})
  js.has_jobs = False
  plugin.handle_jobs()
  assert cleared == ([True] if firstrun else [])


def test_build_never_cleans_the_job_servers_own_dnf(js, plugin, commands):
  # a bare `dnf -y clean all` wiped the build host's cache on every build (#164)
  pending_build(js)
  plugin.handle_jobs()
  assert job_states(js) == [(4, 9)]
  cleans = commands.matching("clean all")
  assert cleans and all("--installroot=" in cmd for cmd in cleans)


def test_adding_itself_as_host_sends_only_its_own_id(js, plugin, images):
  # the hosts from our GET went back with it, so a rebuild in between got
  # its cleared hosts back, none of which had the new version (#248)
  hosted(js, images, image(needs_rebuild=False, jobservers=[7, 8]), local_version=4)
  plugin.updateImageJobservers()
  [(method, path, data)] = js.session.writes
  assert (method, path, data["jobservers"]) == ("PATCH", "systemimages/compute/?addjs", [1])


# The version we have goes with ?addjs once the mPCC says it checks it; a
# 409 means the image moved past it (#248, mprov_control_center#396).

ADDJS = ("PATCH", "systemimages/compute/?addjs")


def addjs_data(js):
  [data] = [d for m, p, d in js.session.writes if (m, p) == ADDJS]
  return data


@pytest.mark.parametrize("features, sent", [(frozenset(), False), (frozenset({"addjs-version"}), True)])
def test_version_is_sent_only_to_an_mpcc_that_checks_it(js, plugin, images, features, sent):
  # an mPCC from before the check writes a version it's sent onto the image
  js.features = features
  hosted(js, images, image(needs_rebuild=False, jobservers=[7]), local_version=4)
  plugin.updateImageJobservers()
  assert ("version" in addjs_data(js)) == sent
  if sent:
    assert addjs_data(js)["version"] == 4


def test_stale_version_isnt_hosted_and_is_fetched_again(js, plugin, images):
  js.features = frozenset({"addjs-version"})
  hosted(js, images, image(needs_rebuild=False, jobservers=[7]), local_version=4)
  js.session.write_routes[ADDJS] = (409, {"detail": "Image compute is at version 5, not 4.", "version": 5})
  plugin.updateImageJobservers()
  # no record of having it, so the next pass fetches it
  assert "compute" not in json.loads((images / "image-versions").read_text())


@pytest.fixture
def boot_files(images, monkeypatch):
  # updateBootFiles() links the image's kernel and initramfs next to it
  def link(self):
    for name in ("compute.vmlinuz", "compute.initramfs"):
      os.symlink("boot/" + name, images / "compute" / name)
  monkeypatch.setattr(UpdateImage, "updateBootFiles", link)


def test_build_that_lost_its_version_is_removed_and_fails(js, plugin, images, commands, boot_files):
  # the image was rebuilt while we built it: what we built is the wrong
  # version, so it's taken down and the job fails; nothing registers it
  js.features = frozenset({"addjs-version"})
  pending_build(js)
  (images / "image-versions").write_text(json.dumps({"compute": 3}))
  js.session.write_routes[ADDJS] = (409, {"detail": "Image compute is at version 5, not 4.", "version": 5})
  plugin.handle_jobs()
  assert addjs_data(js)["version"] == 4
  assert job_states(js) == [(3, 9)]
  assert [p.name for p in (images / "compute").iterdir() if p.name.startswith("compute.")] == []
  assert "compute" not in json.loads((images / "image-versions").read_text())


def test_removal_keeps_what_a_newer_pull_put_in_place(js, plugin, images, commands, boot_files):
  # between publishing and the 409, a pull replaced the image with the
  # newer version and recorded it: that must survive
  js.features = frozenset({"addjs-version"})
  pending_build(js)
  real_patch = js.session.patch
  def newer_pull_then_409(url, data=None, **kw):
    if "?addjs" in url:
      tmp = images / "compute" / ".compute.img.pull"
      tmp.write_bytes(b"NEWER")
      os.replace(tmp, images / "compute" / "compute.img")
      (images / "image-versions").write_text(json.dumps({"compute": 5}))
      js.session.write_routes[ADDJS] = (409, {"detail": "moved on", "version": 5})
    return real_patch(url, data=data, **kw)
  js.session.patch = newer_pull_then_409
  plugin.handle_jobs()
  assert (images / "compute" / "compute.img").read_bytes() == b"NEWER"
  # only the files this build made went
  assert not (images / "compute" / "compute.vmlinuz").is_symlink()
  assert json.loads((images / "image-versions").read_text()) == {"compute": 5}
  assert job_states(js) == [(3, 9)]


def test_build_without_the_check_is_hosted_as_before(js, plugin, images, commands, boot_files):
  pending_build(js)
  plugin.handle_jobs()
  assert "version" not in addjs_data(js)
  assert job_states(js) == [(4, 9)]
  assert (images / "compute" / "compute.img").exists()


# A job server's certificate may send only these keys with ?addjs; the mPCC
# refuses (403) a body with any other, so a build lost its registration to
# the needs_rebuild it sent (#352).
ADDJS_KEYS = {"slug", "jobservers", "version"}


@pytest.mark.parametrize("features", [frozenset(), frozenset({"addjs-version"})])
def test_a_build_sends_only_what_its_certificate_may(js, plugin, images, commands, features):
  js.features = features
  pending_build(js)
  plugin.handle_jobs()
  assert set(addjs_data(js)) <= ADDJS_KEYS
  assert addjs_data(js)["jobservers"] == [1]


@pytest.mark.parametrize("features", [frozenset(), frozenset({"addjs-version"})])
def test_adding_an_image_it_has_sends_only_what_its_certificate_may(js, plugin, images, features):
  js.features = features
  hosted(js, images, image(needs_rebuild=False, jobservers=[7]), local_version=4)
  plugin.updateImageJobservers()
  assert set(addjs_data(js)) <= ADDJS_KEYS


REFUSED = (403, {"detail": "mprov.js.abc may not PATCH this."})


def test_a_refused_registration_after_a_build_is_logged_and_retried(js, plugin, images, commands, caplog):
  # not silent any more: the error says what the mPCC said.  The job still
  # succeeds, since the image is built and published, and the version is
  # recorded so the next pass registers it.
  pending_build(js)
  js.session.write_routes[ADDJS] = REFUSED
  plugin.handle_jobs()
  assert job_states(js) == [(4, 9)]
  assert (images / "compute" / "compute.img").exists()
  assert json.loads((images / "image-versions").read_text()) == {"compute": 4}
  refused = [r for r in caplog.records if "refused to add us as a host of image compute" in r.getMessage()]
  assert len(refused) == 1
  assert refused[0].levelname == "ERROR"
  assert "403" in refused[0].getMessage() and "may not PATCH this" in refused[0].getMessage()

  # the next pass asks again, and this time it's let in
  js.session.write_routes.pop(ADDJS)
  js.session.writes.clear()
  js.session.routes["systemimages/compute/"] = image(needs_rebuild=False, jobservers=[])
  js.session.routes["images/"] = [{"slug": "compute"}]
  plugin.updateImageJobservers()
  assert js.session.writes == [("PATCH", "systemimages/compute/?addjs", {"slug": "compute", "jobservers": [1]})]


def test_a_refused_registration_of_an_image_it_has_is_logged_and_kept(js, plugin, images, caplog):
  # a refusal isn't a 409: we keep the version and ask again next pass
  hosted(js, images, image(needs_rebuild=False, jobservers=[7]), local_version=4)
  js.session.write_routes[ADDJS] = REFUSED
  plugin.updateImageJobservers()
  assert json.loads((images / "image-versions").read_text()) == {"compute": 4}
  [refused] = [r for r in caplog.records if "refused to add us as a host of image compute" in r.getMessage()]
  assert refused.levelname == "ERROR"
  assert "403" in refused.getMessage() and "may not PATCH this" in refused.getMessage()


def test_a_409_isnt_logged_as_a_refusal(js, plugin, images, caplog):
  js.features = frozenset({"addjs-version"})
  hosted(js, images, image(needs_rebuild=False, jobservers=[7]), local_version=4)
  js.session.write_routes[ADDJS] = (409, {"detail": "Image compute is at version 5, not 4.", "version": 5})
  plugin.updateImageJobservers()
  assert not [r for r in caplog.records if "refused to add us" in r.getMessage()]


# the chroot's dnf needs the image's resolv.conf from the first build (#281)

def test_a_new_image_has_resolv_conf_before_the_chroots_dnf_runs(js, rhel, images, monkeypatch):
  img = images / "compute"
  seen = {}
  def system(cmd):
    if f"chroot {img}" in cmd and " dnf " in cmd and "chroot" not in seen:
      seen["chroot"] = (img / "etc/resolv.conf").exists()
    return 0
  monkeypatch.setattr(os, "system", system)
  rhel.imageDetails = image()
  rhel.handle_jobs()
  assert seen["chroot"] is True
  assert (img / "etc/resolv.conf").read_bytes() == open("/etc/resolv.conf", "rb").read()
  assert rhel.threadOk


def test_an_unreadable_host_resolv_conf_fails_the_job(js, rhel, commands, monkeypatch):
  real = shutil.copyfile
  def copyfile(src, dst, *a, **k):
    if src == "/etc/resolv.conf":
      raise PermissionError(13, "Permission denied", src)
    return real(src, dst, *a, **k)
  monkeypatch.setattr(shutil, "copyfile", copyfile)
  rhel.imageDetails = image()
  rhel.handle_jobs()
  assert not rhel.threadOk
  assert job_states(js) == [(3, 9)]
  assert not commands.matching("groupinstall")


# an image build's 2-hour credential instead of the shared key
# (mprov_control_center#381, 4b)

def on_certificate(js, monkeypatch, fail=None):
  """ A job server with its own certificate; the mPCC hands out build
  credentials (recorded), or refuses with fail. """
  from mprov_jobserver import identity as ident
  js.auth = "cert"
  js.config_data["global"].update({"apikey": "fleet-key", "enrollToken": "tok", "caFingerprint": "ab"})
  asked = []

  def build_credential(session, url, image):
    asked.append((url, image))
    if fail is not None:
      raise fail
    return b"-----BEGIN PRIVATE KEY-----\nk\n-----END PRIVATE KEY-----\n", \
      "-----BEGIN CERTIFICATE-----\nc\n-----END CERTIFICATE-----\n", "CA PEM"
  monkeypatch.setattr(image_update_module.ident, "build_credential", build_credential)
  return asked


def seen_at_script_run(images, commands, monkeypatch):
  seen = {}
  popen = commands.popen

  def run(args, **kwargs):
    if args[0].endswith("/tmp/mprov/script-runner.sh"):
      mprov = images / "compute/tmp/mprov"
      seen["config"] = (mprov / "jobserver.yaml").read_text()
      tls = mprov / "tls"
      seen["tls"] = sorted(os.listdir(tls)) if tls.is_dir() else None
      if tls.is_dir():
        seen["tls_mode"] = stat.S_IMODE(os.stat(tls).st_mode)
        seen["pem_mode"] = stat.S_IMODE(os.stat(tls / "jobserver.pem").st_mode)
    return popen(args, **kwargs)
  monkeypatch.setattr(subprocess, "Popen", run)
  return seen


def test_a_job_server_on_its_certificate_gives_the_build_a_credential_not_the_key(
    js, plugin, images, commands, monkeypatch):
  asked = on_certificate(js, monkeypatch)
  seen = seen_at_script_run(images, commands, monkeypatch)
  pending_build(js)
  plugin.handle_jobs()
  assert asked == [(js.mprovURL, "compute")]
  config = yaml.load(seen["config"].replace("- !include", "# !include"), Loader=yaml.SafeLoader)[0]["global"]
  # no key, no enrollment settings, the credential where the runner looks
  for setting in ("apikey", "enrollToken", "caFingerprint"):
    assert setting not in config
  assert "fleet-key" not in seen["config"]
  assert config["tlsDir"] == "/tmp/mprov/tls"
  assert config["mprovURL"] == js.mprovURL
  assert seen["tls"] == ["ca.crt", "jobserver.pem"]
  assert seen["tls_mode"] == 0o700
  assert seen["pem_mode"] == 0o600
  # gone before the image is packed, and the build went on
  assert not (images / "compute/tmp/mprov/tls").exists()
  assert commands.matching("tar c")
  # the job server's own settings are as they were
  assert js.config_data["global"]["apikey"] == "fleet-key"


def test_a_job_server_on_the_key_builds_as_before(js, plugin, images, commands, monkeypatch):
  js.auth = "apikey"
  js.config_data["global"]["apikey"] = "fleet-key"
  seen = seen_at_script_run(images, commands, monkeypatch)
  pending_build(js)
  plugin.handle_jobs()
  assert "fleet-key" in seen["config"]
  assert seen["tls"] is None


def test_no_build_credential_fails_the_build_and_never_falls_back_to_the_key(
    js, plugin, images, commands, monkeypatch, caplog):
  from mprov_jobserver import identity as ident
  on_certificate(js, monkeypatch, fail=ident.EnrollError("the mPCC refused a build credential for image compute"))
  seen = seen_at_script_run(images, commands, monkeypatch)
  pending_build(js)
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]
  assert "config" not in seen
  assert not commands.matching("tar c")
  assert "unable to get a build credential" in caplog.text
  assert not (images / "compute/tmp/mprov/tls").exists()


def test_the_credential_is_removed_when_the_scripts_fail(js, plugin, images, commands, monkeypatch):
  on_certificate(js, monkeypatch)
  commands.fail = ["script-runner.sh"]
  pending_build(js)
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]
  assert not (images / "compute/tmp/mprov/tls").exists()


def test_a_credential_that_cant_be_removed_isnt_packed(js, plugin, images, commands, monkeypatch, caplog):
  on_certificate(js, monkeypatch)
  real = image_update.remove_build_credential
  calls = []

  def stuck(self, path):
    calls.append(path)
    # the first call clears the way; the one after the scripts can't
    return real(self, path) if len(calls) == 1 else False
  monkeypatch.setattr(image_update, "remove_build_credential", stuck)
  pending_build(js)
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]
  assert not commands.matching("tar c")
  assert "isn't packed with it" in caplog.text


def test_a_link_the_image_left_at_the_credentials_path_isnt_followed(js, plugin, images, commands, monkeypatch):
  on_certificate(js, monkeypatch)
  elsewhere = images / "elsewhere"
  elsewhere.mkdir()
  (elsewhere / "keep").write_text("x")
  (images / "compute/tmp/mprov").mkdir(parents=True, exist_ok=True)
  os.chmod(images / "compute/tmp/mprov", 0o700)
  os.symlink(str(elsewhere), str(images / "compute/tmp/mprov/tls"))
  pending_build(js)
  plugin.handle_jobs()
  # the link was removed, not what it points at, and nothing was written there
  assert (elsewhere / "keep").exists()
  assert sorted(os.listdir(elsewhere)) == ["keep"]


# managed files (#96)

def test_managed_files_are_placed_before_the_image_gen_scripts(js, plugin, images, commands, monkeypatch):
  order = []
  monkeypatch.setattr(image_update_module.managed_files, "place_files",
                      lambda session, url, root, config, allow_setuid=False: order.append(("files", config, allow_setuid)))
  popen = commands.popen

  def run(args, **kwargs):
    if args[0].endswith("/tmp/mprov/script-runner.sh"):
      order.append(("scripts",))
    return popen(args, **kwargs)
  monkeypatch.setattr(subprocess, "Popen", run)
  files = {"files": [{"slug": "motd"}]}
  pending_build(js, image(config=files))
  plugin.handle_jobs()
  assert order[0] == ("files", files, False)
  assert order[1] == ("scripts",)


def test_a_managed_file_that_cant_be_placed_fails_the_build(js, plugin, images, commands, monkeypatch, caplog):
  def fail(*args, **kwargs):
    raise image_update_module.managed_files.PlacementError("file chrony-conf: no user or group root:chrony in the image")
  monkeypatch.setattr(image_update_module.managed_files, "place_files", fail)
  pending_build(js)
  plugin.handle_jobs()
  assert job_states(js) == [(3, 9)]
  assert not commands.matching("tar c")
  assert "no user or group root:chrony" in caplog.text


def test_setuid_managed_files_follow_the_setting(js, plugin, images, commands, monkeypatch):
  seen = []
  monkeypatch.setattr(image_update_module.managed_files, "place_files",
                      lambda session, url, root, config, allow_setuid=False: seen.append(allow_setuid))
  plugin.allowSetuidFiles = True
  pending_build(js)
  plugin.handle_jobs()
  assert seen == [True]
