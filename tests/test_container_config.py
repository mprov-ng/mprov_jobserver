"""
The container brings /etc/mprov, a volume it shares with the mPCC, up to
date with the config it ships on every start (#334,
mprov-ng/mprov-compose#21): missing files installed, files from an earlier
release replaced, a site's edits kept (plugin configs) or backed up (the
template), the plugin code earlier images copied there removed, and
nothing else touched.
"""
import hashlib
import importlib.resources
import logging
import os
import pathlib
import stat

import pytest

from mprov_jobserver import container_config
from mprov_jobserver.container_config import main, refresh, released_hashes, shipped_files

IMAGE_CONFIG = "/usr/share/mprov/config"
DATA = pathlib.Path(__file__).resolve().parent / "data"

TEMPLATE_OLD = b"- global:\n    mprovURL: {{env['MPROV_URL']}}\n"
TEMPLATE_NEW = TEMPLATE_OLD + b"{%- if env.get('MPROV_LOG_LEVEL') %}\n    logLevel: x\n{%- endif %}\n"
WEB_OLD = b"- mprov-webserver:\n    port: 8080\n"
WEB_NEW = WEB_OLD + b"    maxDownloads: 4\n"
UPDATE = b"- image-update:\n    imageDir: '/export/images'\n"


def sha(data):
  return hashlib.sha256(data).hexdigest()


@pytest.fixture
def src(tmp_path):
  """ What an image ships in /usr/share/mprov/config, with one earlier release's versions listed too. """
  path = tmp_path / "share"
  (path / "plugins").mkdir(parents=True)
  (path / "jobserver.yaml.j2").write_bytes(TEMPLATE_NEW)
  (path / "plugins/mprov-webserver.yaml").write_bytes(WEB_NEW)
  (path / "plugins/image-update.yaml").write_bytes(UPDATE)
  (path / "shipped-config.sha256").write_text("\n".join([
    "# sha256 of every released version",
    f"{sha(TEMPLATE_OLD)}  jobserver.yaml.j2",
    f"{sha(TEMPLATE_NEW)}  jobserver.yaml.j2",
    f"{sha(WEB_OLD)}  plugins/mprov-webserver.yaml",
    f"{sha(WEB_NEW)}  plugins/mprov-webserver.yaml",
    f"{sha(UPDATE)}  plugins/image-update.yaml",
  ]) + "\n")
  return path


@pytest.fixture
def dest(tmp_path):
  """ /etc/mprov: the volume, as the mPCC leaves it before the job server's first start. """
  path = tmp_path / "etc"
  path.mkdir()
  for name in ("apikey", "enroll_token", "secret_key", "superuser_password"):
    (path / name).write_text(f"{name} secret\n")
    (path / name).chmod(0o600)
  return path


def an_earlier_release(dest):
  (dest / "plugins").mkdir()
  (dest / "jobserver.yaml.j2").write_bytes(TEMPLATE_OLD)
  (dest / "plugins/mprov-webserver.yaml").write_bytes(WEB_OLD)
  (dest / "plugins/image-update.yaml").write_bytes(UPDATE)


def test_a_volume_the_mpcc_wrote_first_gets_every_shipped_file(src, dest):
  # mprov-compose#21 case 1: the job server restart-looped without its template
  assert refresh(str(src), str(dest)) == {
    "jobserver.yaml.j2": "installed",
    "plugins/image-update.yaml": "installed",
    "plugins/mprov-webserver.yaml": "installed",
  }
  assert (dest / "jobserver.yaml.j2").read_bytes() == TEMPLATE_NEW
  assert (dest / "plugins/mprov-webserver.yaml").read_bytes() == WEB_NEW
  assert stat.S_IMODE((dest / "plugins/image-update.yaml").stat().st_mode) == 0o600


def test_files_from_an_earlier_release_are_replaced(src, dest, caplog):
  # mprov-compose#21 case 2: an upgrade kept the old template, so new settings did nothing
  an_earlier_release(dest)
  caplog.set_level(logging.INFO)
  assert refresh(str(src), str(dest)) == {
    "jobserver.yaml.j2": "updated",
    "plugins/image-update.yaml": "current",
    "plugins/mprov-webserver.yaml": "updated",
  }
  assert (dest / "jobserver.yaml.j2").read_bytes() == TEMPLATE_NEW
  assert (dest / "plugins/mprov-webserver.yaml").read_bytes() == WEB_NEW
  assert not list(dest.glob("**/*.new")) and not list(dest.glob("*.bak"))
  assert "updated jobserver.yaml.j2 from an earlier release's" in caplog.text


def test_up_to_date_files_are_left_alone(src, dest):
  refresh(str(src), str(dest))
  before = {p: p.stat().st_mtime_ns for p in dest.rglob("*")}
  assert set(refresh(str(src), str(dest)).values()) == {"current"}
  assert {p: p.stat().st_mtime_ns for p in dest.rglob("*")} == before


def test_a_site_edited_plugin_config_is_kept_and_the_new_default_written_beside_it(src, dest, caplog):
  an_earlier_release(dest)
  edited = WEB_OLD.replace(b"8080", b"8181")
  (dest / "plugins/mprov-webserver.yaml").write_bytes(edited)
  assert refresh(str(src), str(dest))["plugins/mprov-webserver.yaml"] == "kept"
  assert (dest / "plugins/mprov-webserver.yaml").read_bytes() == edited
  assert (dest / "plugins/mprov-webserver.yaml.new").read_bytes() == WEB_NEW
  assert "plugins/mprov-webserver.yaml has local changes, so it's kept" in caplog.text
  # the next start keeps it again, with the same .new
  assert refresh(str(src), str(dest))["plugins/mprov-webserver.yaml"] == "kept"
  assert (dest / "plugins/mprov-webserver.yaml").read_bytes() == edited


def test_the_new_default_is_removed_once_the_site_takes_it(src, dest):
  an_earlier_release(dest)
  (dest / "plugins/mprov-webserver.yaml").write_bytes(b"- mprov-webserver:\n    port: 8181\n")
  refresh(str(src), str(dest))
  # the site merges the new default in
  os.replace(dest / "plugins/mprov-webserver.yaml.new", dest / "plugins/mprov-webserver.yaml")
  (dest / "plugins/mprov-webserver.yaml.new").write_bytes(WEB_NEW)
  assert refresh(str(src), str(dest))["plugins/mprov-webserver.yaml"] == "current"
  assert not (dest / "plugins/mprov-webserver.yaml.new").exists()


def test_an_edited_template_is_replaced_and_the_old_one_kept(src, dest, caplog):
  an_earlier_release(dest)
  edited = TEMPLATE_OLD + b"    heartbeatInterval: 30\n"
  (dest / "jobserver.yaml.j2").write_bytes(edited)
  assert refresh(str(src), str(dest))["jobserver.yaml.j2"] == "replaced"
  assert (dest / "jobserver.yaml.j2").read_bytes() == TEMPLATE_NEW
  assert (dest / "jobserver.yaml.j2.bak").read_bytes() == edited
  assert "kept the old one as jobserver.yaml.j2.bak" in caplog.text


def test_nothing_it_doesnt_ship_is_touched(src, dest):
  an_earlier_release(dest)
  theirs = {
    "apikey": 0o600, "enroll_token": 0o600, "secret_key": 0o600, "superuser_password": 0o600,
    "host-id": 0o644, "jobserver.yaml": 0o600, "mpcc-ca.pem": 0o644, "jobserver-local.yaml": 0o600,
    "plugins/local.yaml": 0o600, "plugins/iamge-delete.yaml": 0o644, "pki/agent.key": 0o600,
  }
  for name, mode in theirs.items():
    (dest / name).parent.mkdir(exist_ok=True)
    (dest / name).write_text(f"the site's {name}\n")
    (dest / name).chmod(mode)
  dest.chmod(0o700)
  refresh(str(src), str(dest))
  for name, mode in theirs.items():
    assert (dest / name).read_text() == f"the site's {name}\n"
    assert stat.S_IMODE((dest / name).stat().st_mode) == mode
  assert stat.S_IMODE(dest.stat().st_mode) == 0o700


def test_plugin_code_earlier_images_left_is_removed(src, dest, caplog):
  an_earlier_release(dest)
  plugins = dest / "plugins"
  for name in ("nads.py", "plugin.py", "__init__.py", "__pycache__/nads.cpython-311.pyc",
               "dnsmasq_mod/dhcp.py", "dnsmasq_mod/__pycache__/dhcp.cpython-311.pyc",
               "image_update_mod/rhel.py", "keep_mod/site-notes.txt"):
    (plugins / name).parent.mkdir(parents=True, exist_ok=True)
    (plugins / name).write_text("x")
  caplog.set_level(logging.INFO)
  refresh(str(src), str(dest))
  assert sorted(p.name for p in plugins.iterdir()) == [
    "image-update.yaml", "keep_mod", "mprov-webserver.yaml"]
  # a *_mod directory with something of the site's in it stays, with just that
  assert [p.name for p in (plugins / "keep_mod").iterdir()] == ["site-notes.txt"]
  assert "copies of plugin code earlier images left" in caplog.text


def test_links_are_never_followed(src, dest, tmp_path):
  an_earlier_release(dest)
  outside = tmp_path / "outside"
  outside.mkdir()
  (outside / "keep.py").write_text("x")
  (outside / "target.yaml").write_bytes(WEB_OLD)
  plugins = dest / "plugins"
  (plugins / "mprov-webserver.yaml").unlink()
  (plugins / "mprov-webserver.yaml").symlink_to(outside / "target.yaml")
  (plugins / "__pycache__").symlink_to(outside)
  (plugins / "evil_mod").symlink_to(outside)
  results = refresh(str(src), str(dest))
  assert results["plugins/mprov-webserver.yaml"] == "skipped"
  assert (outside / "target.yaml").read_bytes() == WEB_OLD
  assert (outside / "keep.py").exists()


def test_a_plugins_link_leaves_the_plugin_configs_alone(src, dest, tmp_path):
  outside = tmp_path / "outside"
  outside.mkdir()
  (dest / "plugins").symlink_to(outside)
  assert refresh(str(src), str(dest)) == {"jobserver.yaml.j2": "installed"}
  assert list(outside.iterdir()) == []


def test_a_failure_is_logged_and_the_rest_still_done(src, dest, monkeypatch, caplog):
  real = container_config.write_file

  def write_file(path, data):
    if path.endswith("image-update.yaml"):
      raise OSError(28, "No space left on device")
    real(path, data)
  monkeypatch.setattr(container_config, "write_file", write_file)
  results = refresh(str(src), str(dest))
  assert results["plugins/image-update.yaml"] == "failed"
  assert results["jobserver.yaml.j2"] == "installed"
  assert "unable to bring plugins/image-update.yaml up to date" in caplog.text


def test_its_lines_are_marked_container_config():
  # run with -m its __name__ is __main__, which logged as "--main--"
  from mprov_jobserver.log import module_name
  assert module_name(container_config.log.name) == "container-config"


def test_main_never_fails_the_start(tmp_path, caplog):
  # e.g. an image without the shipped copy
  assert main([str(tmp_path / "missing"), str(tmp_path)]) == 0
  assert "unable to bring" in caplog.text
  assert main([]) == 0


# the image this runs in

def test_the_image_ships_its_config_pristine():
  share = pathlib.Path(IMAGE_CONFIG)
  assert (share / "jobserver.yaml.j2").read_bytes() == (pathlib.Path(__file__).resolve().parent.parent / "jobserver.yaml.j2").read_bytes()
  package = {entry.name: entry.read_bytes()
             for entry in importlib.resources.files("mprov_jobserver.plugins").iterdir() if entry.name.endswith(".yaml")}
  assert {p.name: p.read_bytes() for p in (share / "plugins").iterdir()} == package
  # only the configs: no plugin code to copy into the volume
  assert not [p for p in share.rglob("*") if p.suffix == ".py"]


def test_the_images_own_etc_mprov_is_empty():
  # Docker seeds an empty volume from it; the start step fills it instead
  assert os.listdir("/etc/mprov") == []


def test_every_shipped_file_is_in_the_released_list():
  # change a shipped default and run container/gen-shipped-config.sh
  known = released_hashes(IMAGE_CONFIG)
  for rel in shipped_files(IMAGE_CONFIG):
    assert sha((pathlib.Path(IMAGE_CONFIG) / rel).read_bytes()) in known.get(rel, ()), rel


def test_a_0_6_2_volume_is_brought_up_to_date(dest):
  # v0.6.2's template, unedited, as the rig's upgraded stack had it
  (dest / "jobserver.yaml.j2").write_bytes((DATA / "jobserver.yaml.j2-v0.6.2").read_bytes())
  results = refresh(IMAGE_CONFIG, str(dest))
  assert results["jobserver.yaml.j2"] == "updated"
  assert b"MPROV_LOG_LEVEL" in (dest / "jobserver.yaml.j2").read_bytes()
  assert not (dest / "jobserver.yaml.j2.bak").exists()
