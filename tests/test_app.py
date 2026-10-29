"""
JobServer (app.py) loads jobserver.yaml, registers with the mPCC, runs the
plugins as threads, and moves jobs between states for them.  These tests
build a JobServer around the fake session from conftest.py, without its
__init__ (which would log in and register), except where __init__ is what's
being tested.
"""
import json
import os
import sys
import types

import pytest

import mprov_jobserver.app as app
import mprov_jobserver.plugins
from mprov_jobserver.app import JobServer
from mprov_jobserver.plugins.plugin import JobServerPlugin
from conftest import FakeSession, MPROV_URL


@pytest.fixture
def server(monkeypatch):
  server = JobServer.__new__(JobServer)
  server.session = FakeSession()
  server.mprovURL = MPROV_URL
  server.id = 5
  server.config_data = {"global": {}}
  server.jobmodules = ["libgenders"]
  server.register = True
  server.sessionOk = True
  server.running = True
  server.running_threads = {}
  server.ip_address = "10.0.0.2"
  monkeypatch.setattr(app.platform, "node", lambda: "js01.cluster.example")
  monkeypatch.setattr(app.os, "getloadavg", lambda: (0.1, 0.1, 0.1))
  return server


def patches(server):
  return [(path, data) for method, path, data in server.session.writes if method == "PATCH"]


# update_job_status()

def test_moves_the_modules_pending_jobs_to_running(server):
  server.session.routes["jobs/?search=libgenders"] = [{"id": 11, "status": 1}, {"id": 12, "status": 1}]
  assert server.update_job_status("libgenders", 2) == 2
  (path, data), _ = patches(server)
  assert path == "jobs/11/"
  assert (data["id"], data["pk"], data["status"], data["jobserver"]) == (11, "libgenders", 2, 5)
  assert data["start_time"] and data["end_time"] is None


def test_success_sets_an_end_time(server):
  server.session.routes["jobs/?search=libgenders"] = [{"id": 11, "status": 2}]
  server.update_job_status("libgenders", 4)
  [(_, data)] = patches(server)
  assert data["status"] == 4 and data["end_time"] and "start_time" not in data


def test_jobs_already_in_that_state_or_finished_are_left_alone(server):
  server.session.routes["jobs/?search=libgenders"] = [
    {"id": 11, "status": 2}, {"id": 12, "status": 3}, {"id": 13, "status": 4}, {"id": 14, "status": 1},
  ]
  assert server.update_job_status("libgenders", 2) == 1
  assert [path for path, _ in patches(server)] == ["jobs/14/"]


def test_a_job_id_updates_just_that_job(server):
  # the mPCC returns a single object for jobs/<id>/
  server.session.routes["jobs/42/"] = {"id": 42, "status": 2}
  assert server.update_job_status("image-update", 3, jobid=42) == 1
  assert [path for path, _ in patches(server)] == ["jobs/42/"]


def test_a_job_query_is_used_as_is(server):
  server.session.routes["jobs/?jobserver=5&module=image-delete&status=1"] = [{"id": 7, "status": 1}]
  assert server.update_job_status("image-delete", 2, jobquery="jobserver=5&module=image-delete&status=1") == 1


@pytest.mark.parametrize("route", [[], (500, None), (404, None)])
def test_no_jobs_or_an_mpcc_error_returns_false(server, route):
  server.session.routes["jobs/?search=libgenders"] = route
  assert server.update_job_status("libgenders", 2) is False
  assert patches(server) == []


def test_a_reply_that_isnt_json_returns_false(server):
  server.session.routes["jobs/?search=libgenders"] = b"<html>Server Error</html>"
  assert server.update_job_status("libgenders", 2) is False


# register_server()

def test_register_posts_the_heartbeat_and_takes_the_id(server):
  server.jobmodules = ["mprov-webserver", "image-sync"]
  server.config_data["mprov-webserver"] = {"serverPort": 8081}
  # the mPCC double-encodes this reply (mprov_control_center#268)
  server.session.write_routes[("POST", "jobservers/")] = json.dumps({"pk": 9})
  server.register_server()
  [(method, path, data)] = server.session.writes
  assert (method, path) == ("POST", "jobservers/")
  assert data["name"] == "js01"
  assert data["address"] == "10.0.0.2"
  assert data["jobmodules"] == ["mprov-webserver", "image-sync"]
  assert data["port"] == 8081
  assert server.id == 9


def test_register_defaults_the_webserver_port(server):
  server.config_data["mprov-webserver"] = {}
  server.session.write_routes[("POST", "jobservers/")] = json.dumps({"pk": 9})
  server.register_server()
  assert server.session.writes[0][2]["port"] == 8080


def test_register_accepts_an_object_reply(server):
  # what the mPCC sends once it stops double-encoding (mprov_control_center#268, #155)
  server.session.write_routes[("POST", "jobservers/")] = {"pk": 9}
  server.register_server()
  assert server.id == 9


@pytest.mark.parametrize("reply", [
  {"name": "js01"},              # no pk
  json.dumps({"name": "js01"}),  # double-encoded, no pk
  "not json",                    # a string that isn't JSON inside
  [{"pk": 9}],                   # not an object
  b"<html>Bad Gateway</html>",   # not JSON at all
])
def test_register_ignores_an_invalid_reply(server, reply):
  server.session.write_routes[("POST", "jobservers/")] = reply
  server.register_server()
  assert server.id == 5


def test_register_400_drops_the_session(server):
  server.session.write_routes[("POST", "jobservers/")] = (400, {"jobmodules": ["bad"]})
  server.register_server()
  assert server.sessionOk is False


@pytest.mark.parametrize("body", [json.dumps({"msg": "boom"}), {"msg": "boom"}])
def test_register_500_reports_the_message_and_exits(server, capsys, body):
  # double-encoded today, an object after mprov_control_center#268 (#155)
  server.session.write_routes[("POST", "jobservers/")] = (500, body)
  with pytest.raises(SystemExit):
    server.register_server()
  assert 'The mPCC said: "boom"' in capsys.readouterr().out


def test_register_500_without_a_message_exits(server, capsys):
  server.session.write_routes[("POST", "jobservers/")] = (500, b"Internal Server Error")
  with pytest.raises(SystemExit):
    server.register_server()
  assert "raw response: Internal Server Error" in capsys.readouterr().out


def test_no_register_flag_skips_registration(server):
  server.register = False
  server.register_server()
  assert server.session.writes == []


@pytest.mark.xfail(strict=True, raises=KeyError,
                   reason="register_server() reads loadmon from config_data, where it never is (#147)")
def test_high_load_skips_the_heartbeat(server, monkeypatch):
  monkeypatch.setattr(app.os, "getloadavg", lambda: (99.0, 99.0, 99.0))
  server.register_server()
  assert server.session.writes == []


# load_config() and __init__

def write_config(tmp_path, global_cfg, plugins=None):
  cfg = [{"global": global_cfg}]
  text = json.dumps(cfg)  # JSON is YAML
  if plugins is not None:
    (tmp_path / "plugins").mkdir()
    for name, section in plugins.items():
      (tmp_path / "plugins" / f"{name}.yaml").write_text(json.dumps([{name: section}]))
    text = text[:-1] + ', !include "plugins/*.yaml"]'
  path = tmp_path / "jobserver.yaml"
  path.write_text(text)
  return str(path)


def bare_server(configfile):
  server = JobServer.__new__(JobServer)
  server.configfile = configfile
  server.load_config()
  return server


def test_load_config_maps_global_and_includes_plugin_files(tmp_path):
  path = write_config(tmp_path, {"mprovURL": "http://mpcc:8080", "apikey": "k", "jobmodules": ["libgenders"]},
                      plugins={"libgenders": {"gendersFile": "/tmp/genders"}})
  server = bare_server(path)
  # a trailing slash is added to the URL
  assert server.mprovURL == "http://mpcc:8080/"
  assert server.apikey == "k"
  assert server.jobmodules == ["libgenders"]
  assert server.config_data["libgenders"] == {"gendersFile": "/tmp/genders"}


def test_load_config_rejects_an_unknown_global_entry(tmp_path):
  path = write_config(tmp_path, {"mprovURL": "http://mpcc/", "nosuchsetting": 1})
  with pytest.raises(SystemExit):
    bare_server(path)


def test_load_config_without_a_config_file_exits(tmp_path, monkeypatch):
  monkeypatch.chdir(tmp_path)
  with pytest.raises(SystemExit):
    bare_server(str(tmp_path / "missing.yaml"))


def test_dash_m_runs_the_named_modules_once_without_registering(tmp_path, monkeypatch):
  path = write_config(tmp_path, {"mprovURL": "http://mpcc/", "jobmodules": ["libgenders"]})
  monkeypatch.setattr(app.sys, "argv", ["mprov_jobserver", "-m", "script-runner,nads"])
  monkeypatch.setattr(JobServer, "startSession", lambda self: True)
  monkeypatch.setattr(JobServer, "load_plugins", lambda self: None)
  posts = []
  monkeypatch.setattr(JobServer, "session", types.SimpleNamespace(post=lambda *a, **k: posts.append(a)))
  server = JobServer(configfile=path)
  assert server.jobmodules == ["script-runner", "nads"]
  assert server.runonce is True
  assert server.register is False
  assert posts == []


def test_load_plugins_needs_jobmodules(server):
  server.jobmodules = None
  with pytest.raises(SystemExit):
    server.load_plugins()


# startSession()

def test_start_session_sends_the_api_key(server):
  server.apikey = "secret"
  server.myaddress = "10.0.0.3"
  server.session.routes[""] = {}
  assert server.startSession() is True
  assert server.session.headers["Authorization"] == "Api-Key secret"
  assert server.ip_address == "10.0.0.3"
  assert server.sessionOk is True


def test_start_session_retries_when_the_mpcc_is_down(server, monkeypatch):
  def down(*args, **kwargs):
    raise ConnectionError("refused")
  server.session.get = down
  monkeypatch.setattr(app.time, "sleep", lambda s: None)
  server.startSession()
  assert server.sessionOk is False


# start()

class Recorder(JobServerPlugin):
  jobModule = "recorder"
  ran = []

  def handle_jobs(self):
    Recorder.ran.append(self.js)


def test_runonce_runs_each_module_once_and_waits_for_it(server, monkeypatch):
  monkeypatch.setattr(mprov_jobserver.plugins, "recorder_mod",
                      types.SimpleNamespace(recorder_mod=Recorder), raising=False)
  monkeypatch.setattr(app.time, "sleep", lambda s: None)
  Recorder.ran = []
  server.jobmodules = ["recorder-mod"]
  server.config_data = {}
  server.runonce = True
  server.heartbeatInterval = 10
  assert server.start() == 0
  assert Recorder.ran == [server]
  # runonce doesn't heartbeat
  assert server.session.writes == []


class ScriptRunnerModule:
  """ Stand-in for the mprov_jobserver.plugins.script_runner module attribute,
  so start() instantiates the Runner class instead of the real plugin. """

  def __init__(self, runner):
    self.script_runner = runner


def run_script_runner_once(server, monkeypatch, tmp_path, run_script):
  """ Run a script-runner runonce session through start(), with each script's
  running replaced by ``run_script`` (which records into scriptResults).
  Returns start()'s exit code. """
  import mprov_jobserver.plugins
  import mprov_jobserver.plugins.script_runner as script_runner_module
  from mprov_jobserver.plugins.script_runner import script_runner

  class Runner(script_runner):
    runScript = run_script

  monkeypatch.setattr(mprov_jobserver.plugins, "script_runner",
                      ScriptRunnerModule(Runner), raising=True)
  # start() reloads the plugin module before instantiating it; keep it from
  # replacing the stub above
  monkeypatch.setattr(app.importlib, "reload", lambda module: module)
  monkeypatch.setattr(sys, "argv", ["mprov_jobserver", "-i", "compute"])
  server.session.routes["systemimages/compute/"] = {
    "name": "compute", "slug": "compute",
    "osdistro": {"scripts": [], "config_params": ""},
    "systemgroups": [], "scripts": [
      {"slug": "good", "filename": "http://mpcc/media/scripts/good.sh",
       "scriptType": {"slug": "image-gen"}, "dependsOn": []},
      {"slug": "bad", "filename": "http://mpcc/media/scripts/bad.sh",
       "scriptType": {"slug": "image-gen"}, "dependsOn": []}],
    "config_params": "",
  }
  # stub script_runner's host touches: the nologin file and the entity cleanup
  monkeypatch.setattr(script_runner_module, "Path",
                      lambda p: types.SimpleNamespace(touch=lambda: None))
  monkeypatch.setattr(os, "unlink", lambda p: None)
  server.jobmodules = ["script-runner"]
  server.config_data = {}
  server.runonce = True
  server.heartbeatInterval = 10
  server.firstrun = True
  # the pending script-runner job the failure report moves to failed
  server.session.routes["jobs/?search=script-runner"] = [{"id": 11, "status": 1}]
  # start() spawns (and joins) the module thread itself; keep its scratch out
  # of the real /tmp
  real_init = Runner.__init__

  def init(self, js):
    real_init(self, js)
    self.scriptTmpDir = str(tmp_path)
  monkeypatch.setattr(Runner, "__init__", init)
  return server.start()


def test_runonce_exits_nonzero_when_a_script_fails(server, monkeypatch, tmp_path):
  # a failing script fails its plugin thread (#174); the runonce process exits
  # nonzero instead of reporting success, and the failure is reported to the mPCC
  def run_script(self, filename):
    self.scriptResults[filename] = 1 if "bad" in filename else 0
  assert run_script_runner_once(server, monkeypatch, tmp_path, run_script) == 1
  assert server.failed_runonce_modules == ["script-runner"]
  # the module's job was reported failed, not successful
  [(_, data)] = server.session.patches
  assert (data["id"], data["pk"], data["status"]) == (11, "script-runner", 3)


def test_runonce_still_exits_zero_when_everything_succeeds(server, monkeypatch, tmp_path):
  def run_script(self, filename):
    self.scriptResults[filename] = 0
  assert run_script_runner_once(server, monkeypatch, tmp_path, run_script) == 0
  assert server.failed_runonce_modules == []
  # runonce doesn't heartbeat and reports no failure
  assert server.session.patches == []
