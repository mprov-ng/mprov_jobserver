"""
repo_update claims the mPCC's repo-update jobs and hands each repo to its
ostype's sub-module; repo_update (rhel) mirrors a repo with dnf reposync
into repoDir/<repo id>.
"""
import os

import pytest

from mprov_jobserver.plugins.repo_update import repo_update
from mprov_jobserver.plugins.repo_update_mod.rhel import UpdateRepo

REPO = {"id": 5, "name": "Rocky BaseOS", "repo_package_url": "https://mirror.example/rocky/9/BaseOS/x86_64/os/"}


@pytest.fixture
def plugin(js, tmp_path):
  plugin = UpdateRepo(js)
  plugin.jobModule = "repo-update"
  plugin.repo = dict(REPO)
  plugin.repoDir = str(tmp_path / "repos")
  plugin.jobid = 9
  return plugin


def test_reposync_uses_an_absolute_destdir_without_chdir(plugin, js, monkeypatch, tmp_path):
  commands = []
  monkeypatch.setattr(os, "system", lambda cmd: commands.append(cmd) or 0)
  cwd = os.getcwd()
  plugin.handle_jobs()

  # no chdir: other plugin threads share the working directory (#130)
  assert os.getcwd() == cwd
  [cmd] = commands
  assert cmd.startswith("dnf reposync ")
  assert f"--destdir={plugin.repoDir}/ " in cmd
  assert f"--installroot={plugin.repoDir}/5" in cmd
  assert "./" not in cmd
  assert os.path.isdir(f"{plugin.repoDir}/5")
  # the repo is marked as hosted here, and the job done
  assert js.session.patches == [("repos/5/", {"id": 5, "hosted_by": [1]})]
  assert js.job_updates[-1][:2] == ("repo-update", 4)


def test_reposync_failure_fails_the_job(plugin, js, monkeypatch):
  monkeypatch.setattr(os, "system", lambda cmd: 1)
  plugin.handle_jobs()
  assert js.session.patches == []
  assert js.job_updates[-1][:2] == ("repo-update", 3)


def test_rhel_registers_its_ostype(js):
  UpdateRepo(js).load_config()
  [(method, path, data)] = js.session.writes
  # the URL has a double slash (#115)
  assert method == "POST" and path.endswith("ostypes/")
  assert data == {"slug": "rhel", "name": "Yum/DNF Based Linux"}


# repo_update.handle_jobs()

JOBS = "jobs/?jobserver=1&module=repo-update&status=2"


@pytest.fixture
def update(js, tmp_path, commands, monkeypatch):
  # Run the ostype sub-module in the test's thread, so it's done when handle_jobs() returns.
  monkeypatch.setattr(UpdateRepo, "start", lambda self: self.handle_jobs())
  monkeypatch.setattr(UpdateRepo, "join", lambda self, timeout=None: None)
  plugin = repo_update(js)
  plugin.repoDir = str(tmp_path / "repos")
  return plugin


def job_states(js):
  return [(status, jobid) for _, status, jobid in js.job_updates_by_id if jobid is not None]


def test_job_runs_the_repos_ostype_module(js, update, commands):
  js.session.routes.update({JOBS: [{"id": 21, "params": {"repo_id": 5}}], "repos/5/": dict(REPO, ostype="rhel")})
  update.handle_jobs()
  assert commands.matching("dnf reposync")
  assert os.path.exists(f"{update.repoDir}/robots.txt")
  assert job_states(js) == [(4, 21)]


def test_repo_without_an_ostype_fails_the_job(js, update, commands):
  js.session.routes.update({JOBS: [{"id": 21, "params": {"repo_id": 5}}], "repos/5/": dict(REPO, ostype=None)})
  update.handle_jobs()
  assert commands.run == []
  # failed once, without going on to import repo_update_mod.None (#145)
  assert job_states(js) == [(3, 21)]


def test_mpcc_error_for_the_repo_fails_the_job(js, update, commands):
  js.session.routes.update({JOBS: [{"id": 21, "params": {"repo_id": 5}}], "repos/5/": (404, None)})
  update.handle_jobs()
  assert commands.run == []
  assert job_states(js) == [(3, 21)]


def test_no_pending_jobs_does_nothing(js, update):
  js.has_jobs = False
  update.handle_jobs()
  assert js.session.requests == []


def test_job_with_bad_params_fails_the_job(js, update):
  # called self.module, which doesn't exist: AttributeError (#145)
  js.session.routes[JOBS] = [{"id": 21, "params": {}}]
  update.handle_jobs()
  assert job_states(js) == [(3, 21)]


def test_job_with_bad_params_doesnt_stop_the_others(js, update, commands):
  # returning after the bad job left job 22 running (#145)
  js.session.routes.update({
    JOBS: [{"id": 21}, {"id": 22, "params": {"repo_id": 5}}],
    "repos/5/": dict(REPO, ostype="rhel"),
  })
  update.handle_jobs()
  assert job_states(js) == [(3, 21), (4, 22)]


def test_repo_dir_failure_fails_the_claimed_jobs(js, update, tmp_path, commands):
  # self(self.js.id) raised TypeError, and the query failed every running job (#145)
  (tmp_path / "file").write_text("")
  update.repoDir = str(tmp_path / "file" / "repos")
  js.session.routes[JOBS] = [{"id": 21, "params": {"repo_id": 5}}, {"id": 22, "params": {"repo_id": 6}}]
  update.handle_jobs()
  assert job_states(js) == [(3, 21), (3, 22)]
  assert [u for u in js.job_updates_by_id if u[1] == 3 and u[2] is None] == []
  assert commands.run == []


@pytest.mark.parametrize("reply", [b"<html>proxy error</html>", dict(REPO, ostype="nosuchos")])
def test_unusable_repo_fails_the_job(js, update, commands, reply):
  # an unparsable reply or an unknown ostype moved on and left the job running (#145)
  js.session.routes.update({JOBS: [{"id": 21, "params": {"repo_id": 5}}], "repos/5/": reply})
  update.handle_jobs()
  assert job_states(js) == [(3, 21)]
  assert commands.run == []


def test_ostype_module_that_cant_start_fails_the_job(js, update, commands, monkeypatch):
  def broken(self, js):
    raise RuntimeError("no")
  monkeypatch.setattr(UpdateRepo, "__init__", broken)
  js.session.routes.update({JOBS: [{"id": 21, "params": {"repo_id": 5}}], "repos/5/": dict(REPO, ostype="rhel")})
  update.handle_jobs()
  assert job_states(js) == [(3, 21)]
  assert commands.run == []


def test_run_waits_for_every_repo_it_started(js, tmp_path, monkeypatch):
  # returning while the reposyncs ran let the next heartbeat start a second
  # reposync of each repo still syncing (#157); they still run in parallel
  import threading
  both_running = threading.Barrier(2, timeout=5)
  finished = []
  def sync(self):
    both_running.wait()   # times out unless the two repos sync at once
    finished.append(self.jobid)
  monkeypatch.setattr(UpdateRepo, "handle_jobs", sync)
  js.session.routes.update({
    JOBS: [{"id": 21, "params": {"repo_id": 5}}, {"id": 22, "params": {"repo_id": 6}}],
    "repos/5/": dict(REPO, ostype="rhel"),
    "repos/6/": dict(REPO, id=6, ostype="rhel"),
  })
  plugin = repo_update(js)
  plugin.repoDir = str(tmp_path / "repos")
  plugin.handle_jobs()
  assert sorted(finished) == [21, 22]


@pytest.mark.parametrize("url", [
  'https://mirror.example/rocky/9/BaseOS/x86_64/os/',
  'https://mirror.example/repo"; touch /tmp/pwned; echo "',
  "https://mirror.example/$(id)/os/",
  "https://mirror.example/with space/os/",
])
def test_the_repo_url_stays_one_argument(plugin, js, monkeypatch, url):
  # a free-text field in the mPCC, so it's quoted, never part of the shell
  # syntax (#184)
  import shlex
  commands = []
  monkeypatch.setattr(os, "system", lambda cmd: commands.append(cmd) or 0)
  plugin.repo["repo_package_url"] = url
  plugin.handle_jobs()
  [cmd] = commands
  args = shlex.split(cmd)
  assert args[args.index("--repofrompath") + 1] == f"5,{url}"
  assert args[0:2] == ["dnf", "reposync"]


# newestOnly: mirror only the newest version of each package (#95)

@pytest.mark.parametrize("newest", [True, False])
def test_newest_only_is_passed_to_reposync(plugin, monkeypatch, newest):
  commands = []
  monkeypatch.setattr(os, "system", lambda cmd: commands.append(cmd) or 0)
  plugin.newestOnly = newest
  plugin.handle_jobs()
  [cmd] = commands
  assert (" --newest-only " in cmd) is newest
  # never --delete: older packages already mirrored stay
  assert "--delete" not in cmd


@pytest.mark.parametrize("setting", [True, False])
def test_the_setting_reaches_the_repo_worker(js, update, commands, setting):
  update.newestOnly = setting
  js.session.routes.update({JOBS: [{"id": 21, "params": {"repo_id": 5}}], "repos/5/": dict(REPO, ostype="rhel")})
  update.handle_jobs()
  [cmd] = commands.matching("dnf reposync")
  assert (" --newest-only " in cmd) is setting


def test_newest_only_is_off_without_the_setting(js):
  # sites whose repo-update.yaml predates it keep mirroring everything
  js.config_data = {"repo-update": {"repoDir": "/export/repos"}}
  plugin = repo_update(js)
  plugin.load_config()
  assert plugin.newestOnly is False
  js.config_data = {"repo-update": {"repoDir": "/export/repos", "newestOnly": True}}
  plugin = repo_update(js)
  plugin.load_config()
  assert plugin.newestOnly is True


def test_the_shipped_config_turns_it_on():
  import yaml, importlib.resources
  text = (importlib.resources.files("mprov_jobserver") / "plugins" / "repo-update.yaml").read_text()
  assert yaml.safe_load(text)[0]["repo-update"]["newestOnly"] is True



# registering as the repo's host after mirroring (#274)

@pytest.mark.parametrize("reply", [(400, {"hosted_by": ["bad"]}), (404, None), (500, {})])
def test_a_refused_host_registration_fails_the_job(plugin, js, monkeypatch, capsys, reply):
  monkeypatch.setattr(os, "system", lambda cmd: 0)
  js.session.write_routes[("PATCH", "repos/5/")] = reply
  plugin.handle_jobs()
  assert js.job_updates_by_id[-1][1:] == (3, 9)
  assert f"the mPCC answered {reply[0]}" in capsys.readouterr().out


def test_an_unreachable_mpcc_fails_the_job(plugin, js, monkeypatch):
  monkeypatch.setattr(os, "system", lambda cmd: 0)
  def down(*args, **kwargs):
    raise ConnectionError("refused")
  js.session.patch = down
  plugin.handle_jobs()
  assert js.job_updates_by_id[-1][1:] == (3, 9)
