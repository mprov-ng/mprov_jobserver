"""
JobServerPlugin (plugins/plugin.py) is the base class every plugin runs on:
it maps the plugin's section of jobserver.yaml onto its attributes, and
reports job states to the mPCC through the job server.
"""
import pytest

from mprov_jobserver.plugins.plugin import JobServerPlugin


class Example(JobServerPlugin):
  jobModule = "example"
  imageDir = "/default"
  imageList = None
  handled = False

  def handle_jobs(self):
    self.handled = True


@pytest.fixture
def plugin(js):
  return Example(js)


def test_config_section_sets_attributes(js, plugin):
  js.config_data = {"example": {"imageDir": "/srv/images", "imageList": ["a", "b"]}}
  assert plugin.load_config()
  assert plugin.imageDir == "/srv/images"
  assert plugin.imageList == ["a", "b"]


def test_no_config_section_keeps_the_defaults(js, plugin):
  js.config_data = {"global": {}}
  assert plugin.load_config()
  assert plugin.imageDir == "/default"


def test_unknown_config_entry_is_reported_and_skipped(js, plugin, capsys):
  js.config_data = {"example": {"nosuchsetting": 1, "imageDir": "/srv"}}
  assert plugin.load_config()
  assert not hasattr(plugin, "nosuchsetting")
  assert plugin.imageDir == "/srv"
  assert "Unknown config entry nosuchsetting in example" in capsys.readouterr().out


def test_empty_config_section_stops_the_plugin(js, plugin):
  # "example:" with nothing under it loads as None.
  js.config_data = {"example": None}
  assert not plugin.load_config()


def test_no_config_at_all_stops_the_plugin(js, plugin):
  js.config_data = None
  assert not plugin.load_config()


def test_run_loads_config_then_handles_jobs(js, plugin):
  js.config_data = {}
  plugin.run()
  assert plugin.handled


def test_run_skips_jobs_when_config_fails(js, plugin):
  js.config_data = None
  plugin.run()
  assert not plugin.handled


def test_job_states(js, plugin):
  plugin.set_job_running()
  plugin.set_job_success()
  assert plugin.threadOk
  plugin.set_job_failure()
  assert not plugin.threadOk
  # RUNNING = 2, SUCCESS = 4, FAILURE = 3
  assert [u[:2] for u in js.job_updates] == [("example", 2), ("example", 4), ("example", 3)]


@pytest.mark.parametrize("status, ok", [(199, False), (200, True), (204, True), (299, True), (300, False), (404, False)])
def test_check_http_status(plugin, status, ok):
  assert plugin.checkHTTPStatus(status) is ok


def test_default_handle_jobs_does_nothing_without_jobs(js, monkeypatch):
  # The base handle_jobs() simulates 15 seconds of work, but only for a pending job.
  monkeypatch.setattr("mprov_jobserver.plugins.plugin.sleep", lambda s: pytest.fail("slept"))
  js.has_jobs = False
  JobServerPlugin(js).handle_jobs()
  assert [u[1] for u in js.job_updates] == [2]


def test_mounts_under_lists_mounts_in_a_tree_deepest_first(tmp_path):
  from mprov_jobserver.plugins.plugin import mounts_under
  info = tmp_path / "mountinfo"
  root = tmp_path / "images"
  info.write_text("".join(
    f"{n} 1 0:1 / {point} rw - tmpfs tmpfs rw\n" for n, point in enumerate([
      "/", "/proc",
      f"{root}/compute/dev", f"{root}/compute/dev/pts",
      f"{root}-other/x",              # a sibling with the same prefix
      f"{root}/with\\040space/proc",  # mountinfo escapes spaces
    ])))
  found = mounts_under(str(root), str(info))
  assert set(found) == {f"{root}/with space/proc", f"{root}/compute/dev/pts", f"{root}/compute/dev"}
  # a mount inside another comes first, so it can be unmounted first
  assert found.index(f"{root}/compute/dev/pts") < found.index(f"{root}/compute/dev")
  assert mounts_under(str(tmp_path / "nothing"), str(info)) == []
