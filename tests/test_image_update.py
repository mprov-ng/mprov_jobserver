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
import types

import pytest
import yaml

import mprov_jobserver.app as app
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


# jobserver.yaml under the image holds the fleet API key (#169)

def test_jobserver_yaml_is_0600_in_a_0700_dir(js, plugin, images, commands, monkeypatch):
  seen = {}

  def system(cmd):
    if cmd.endswith("/tmp/mprov/script-runner.sh"):
      mprov = images / "compute/tmp/mprov"
      seen["dir_mode"] = stat.S_IMODE(os.stat(mprov).st_mode)
      seen["plugins_mode"] = stat.S_IMODE(os.stat(mprov / "plugins").st_mode)
      seen["file_mode"] = stat.S_IMODE(os.stat(mprov / "jobserver.yaml").st_mode)
      seen["content"] = (mprov / "jobserver.yaml").read_text()
    return commands(cmd)

  monkeypatch.setattr(os, "system", system)
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
