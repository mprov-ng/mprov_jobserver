"""
repo_delete removes the mirrors of repos the mPCC deleted (#161).  The mPCC
queues one "Delete Repos" job, with no params, when a repo is deleted, so
the plugin compares repoDir with /repos/.
"""
import pytest

from mprov_jobserver.plugins.repo_delete import repo_delete

JOBS = "jobs/?jobserver=1&module=repo-delete&status=2"


@pytest.fixture
def repos(tmp_path):
  path = tmp_path / "repos"
  path.mkdir()
  for repo in ("1", "2", "5"):
    (path / repo / "repodata").mkdir(parents=True)
  return path


def plugin(js, repos, mpcc_repos=(1, 2)):
  js.session.routes.update({
    JOBS: [{"id": 7, "params": {}}],
    "repos/": [{"id": repo} for repo in mpcc_repos],
  })
  p = repo_delete(js)
  p.repoDir = str(repos)
  return p


def outcome(js):
  return [(status, jobid) for _, status, jobid in js.job_updates_by_id if jobid is not None]


def test_a_deleted_repos_mirror_is_removed(js, repos):
  plugin(js, repos).handle_jobs()
  assert sorted(p.name for p in repos.iterdir()) == ["1", "2"]
  # claimed by module and status (the job has no job server yet), then done by id
  assert js.job_updates[0] == ("repo-delete", 2, "module=repo-delete&status=1")
  assert outcome(js) == [(4, 7)]


def test_only_repo_mirrors_are_touched(js, repos, tmp_path):
  (repos / "repo-versions").write_text("{}")
  (repos / "robots.txt").write_text("")
  (repos / "site-mirror").mkdir()
  (repos / "9").write_text("a file, not a mirror")
  outside = tmp_path / "elsewhere"
  outside.mkdir()
  (repos / "8").symlink_to(outside)
  plugin(js, repos).handle_jobs()
  assert sorted(p.name for p in repos.iterdir()) == ["1", "2", "8", "9", "repo-versions", "robots.txt", "site-mirror"]
  assert outside.exists()


def test_no_job_no_deleting(js, repos):
  js.has_jobs = False
  plugin(js, repos).handle_jobs()
  assert len(list(repos.iterdir())) == 3


@pytest.mark.parametrize("reply", [(500, []), (200, {"detail": "x"}), (404, None)])
def test_without_a_real_repo_list_nothing_is_removed_and_the_job_fails(js, repos, reply):
  p = plugin(js, repos)
  js.session.routes["repos/"] = reply
  p.handle_jobs()
  assert len(list(repos.iterdir())) == 3
  assert outcome(js) == [(3, 7)]
  assert p.failed


def test_an_empty_repo_list_removes_every_mirror(js, repos):
  plugin(js, repos, mpcc_repos=()).handle_jobs()
  assert list(repos.iterdir()) == []
  assert outcome(js) == [(4, 7)]


def test_a_failed_removal_fails_the_job(js, repos, monkeypatch):
  import shutil
  def broken(path):
    raise OSError("busy")
  monkeypatch.setattr(shutil, "rmtree", broken)
  p = plugin(js, repos)
  p.handle_jobs()
  assert outcome(js) == [(3, 7)]


def test_no_repo_dir_yet_is_nothing_to_delete(js, tmp_path):
  p = plugin(js, tmp_path / "missing")
  p.handle_jobs()
  assert outcome(js) == [(4, 7)]


def test_no_repo_dir_configured_fails_the_job(js, repos):
  p = plugin(js, repos)
  p.repoDir = ""
  p.handle_jobs()
  assert outcome(js) == [(3, 7)]
  assert len(list(repos.iterdir())) == 3


def test_repo_dir_defaults_to_repo_updates(js):
  js.config_data = {"repo-update": {"repoDir": "/export/repos"}, "repo-delete": {"repoDir": ""}}
  p = repo_delete(js)
  assert p.load_config()
  assert p.repoDir == "/export/repos"
  js.config_data["repo-delete"] = {"repoDir": "/data/mirrors"}
  p = repo_delete(js)
  p.load_config()
  assert p.repoDir == "/data/mirrors"
