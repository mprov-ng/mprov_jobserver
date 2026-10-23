"""
The repo plugins: repo_update mirrors an OS repo with its ostype's
sub-module (repo_update_mod/rhel.py, dnf reposync) when the mPCC queues a
repo-update job; repo_sync copies every repo from the mPCC's hosts with
wget; repo_delete is still the base plugin.  Shell commands are recorded
instead of run.
"""
import json
import types

import pytest

from mprov_jobserver.plugins import repo_sync as repo_sync_module
from mprov_jobserver.plugins.repo_delete import repo_delete
from mprov_jobserver.plugins.repo_sync import repo_sync
from mprov_jobserver.plugins.repo_update import repo_update
from mprov_jobserver.plugins.repo_update_mod.rhel import UpdateRepo

JOBS = "jobs/?jobserver=1&module=repo-update&status=2"
REPO = {"id": 5, "name": "Rocky 9 BaseOS", "ostype": "rhel",
        "repo_package_url": "https://dl.rockylinux.org/pub/rocky/9/BaseOS/x86_64/os/"}


@pytest.fixture
def repos(tmp_path, monkeypatch):
  # repo_update and repo_sync chdir into the repo dir (#130, #131); keep the
  # test's working directory where it was afterwards.
  monkeypatch.chdir(tmp_path)
  path = tmp_path / "repos"
  path.mkdir()
  return path


def job_states(js):
  return [(status, jobid) for _, status, jobid in js.job_updates_by_id if jobid is not None]


# repo_update

@pytest.fixture
def update(js, repos, commands, monkeypatch):
  # Run the ostype sub-module in the test's thread, so it's done when handle_jobs() returns.
  monkeypatch.setattr(UpdateRepo, "start", lambda self: self.handle_jobs())
  plugin = repo_update(js)
  plugin.repoDir = str(repos)
  return plugin


def test_mirrors_a_repo_with_reposync(js, update, repos, commands):
  js.session.routes.update({JOBS: [{"id": 21, "params": {"repo_id": 5}}], "repos/5/": REPO})
  update.handle_jobs()
  [cmd] = commands.matching("dnf reposync")
  assert f'--repofrompath 5,"{REPO["repo_package_url"]}"' in cmd
  assert f"--installroot={repos}/5" in cmd
  assert "--download-metadata" in cmd
  assert (repos / "robots.txt").exists()
  assert (repos / "5").is_dir()
  # this job server is the repo's host now, and the update flag is cleared
  assert js.session.writes == [("PATCH", "repos/5/", {"id": 5, "hosted_by": [1], "update": False})]
  assert job_states(js) == [(4, 21)]


def test_reposync_failure_fails_the_job(js, update, commands):
  commands.fail = ["dnf reposync"]
  js.session.routes.update({JOBS: [{"id": 21, "params": {"repo_id": 5}}], "repos/5/": REPO})
  update.handle_jobs()
  assert js.session.writes == []
  assert job_states(js) == [(3, 21)]


def test_repo_without_an_ostype_fails_the_job(js, update, commands):
  js.session.routes.update({JOBS: [{"id": 21, "params": {"repo_id": 5}}], "repos/5/": dict(REPO, ostype=None)})
  update.handle_jobs()
  assert commands.run == []
  assert job_states(js) == [(3, 21)]


def test_mpcc_error_for_the_repo_fails_the_job(js, update, commands):
  js.session.routes.update({JOBS: [{"id": 21, "params": {"repo_id": 5}}], "repos/5/": (404, None)})
  update.handle_jobs()
  assert commands.run == []
  assert job_states(js) == [(3, 21)]


def test_no_pending_jobs_does_nothing(js, update, commands):
  js.has_jobs = False
  update.handle_jobs()
  assert js.session.requests == []


@pytest.mark.xfail(strict=True, raises=AttributeError,
                   reason="repo_update's error paths call self.module, which doesn't exist (see the PR)")
def test_job_with_bad_params_fails_the_job(js, update):
  js.session.routes[JOBS] = [{"id": 21, "params": {}}]
  update.handle_jobs()
  assert job_states(js) == [(3, 21)]


def test_rhel_registers_its_ostype(js):
  UpdateRepo(js).load_config()
  [(method, path, data)] = js.session.writes
  # the URL has a double slash (#115)
  assert method == "POST" and path.endswith("ostypes/")
  assert data == {"slug": "rhel", "name": "Yum/DNF Based Linux"}


# repo_sync

@pytest.fixture
def sync(js, repos, monkeypatch):
  js.jobmodules = ["repo-sync", "mprov-webserver"]
  plugin = repo_sync(js)
  plugin.repoDir = str(repos)
  plugin.wgets = []
  monkeypatch.setattr(repo_sync_module, "sh", types.SimpleNamespace(wget=plugin.wgets.append))
  return plugin


def test_sync_mirrors_a_changed_repo_and_adds_itself(js, sync, repos):
  js.session.routes.update({"repos/": [{"id": 5}], "repos/5/": {"version": 2, "hosted_by": [7]}})
  sync.handle_jobs()
  [args] = sync.wgets
  assert "--mirror" in args and args[-1] == "http://mpcc/osrepos/5/"
  assert json.loads((repos / "repo-versions").read_text()) == {"5": 2}
  assert js.session.writes == [("PATCH", "repos/5/?addjs", {"id": "5", "hosted_by": [7, 1]})]


def test_sync_skips_a_current_repo(js, sync, repos):
  (repos / "repo-versions").write_text(json.dumps({"5": 2}))
  (repos / "5").mkdir()
  js.session.routes.update({"repos/": [{"id": 5}], "repos/5/": {"version": 2, "hosted_by": [1]}})
  sync.handle_jobs()
  assert sync.wgets == []
  assert js.session.writes == []
  assert (repos / "5").exists()


def test_sync_removes_repos_it_no_longer_serves(js, sync, repos):
  (repos / "9").mkdir()
  js.session.routes["repos/"] = []
  sync.handle_jobs()
  assert not (repos / "9").exists()


def test_sync_needs_the_webserver(js):
  js.jobmodules = ["repo-sync"]
  with pytest.raises(SystemExit):
    repo_sync(js).load_config()


@pytest.mark.xfail(strict=True, raises=TypeError,
                   reason="repo_sync compares status_code with the string \"404\", so a deleted repo crashes the sync (see the PR)")
def test_sync_skips_a_repo_deleted_from_the_mpcc(js, sync):
  sync.repoList = ["4", "5"]
  js.session.routes.update({"repos/4/": (404, None), "repos/5/": {"version": 2, "hosted_by": [1]}})
  sync.handle_jobs()
  assert len(sync.wgets) == 1


# repo_delete

def test_repo_delete_is_still_the_base_plugin(js, monkeypatch):
  # It marks repo-delete jobs done after the base class's simulated work, without deleting anything.
  monkeypatch.setattr("mprov_jobserver.plugins.plugin.sleep", lambda s: None)
  repo_delete(js).handle_jobs()
  assert [u[:2] for u in js.job_updates] == [("repo-delete", 2), ("repo-delete", 4)]
