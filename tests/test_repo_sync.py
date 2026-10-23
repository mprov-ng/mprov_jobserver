"""
repo_sync mirrors each repo whose mPCC version differs from ours, from the
mPCC's osrepos/, into repoDir/<repo id>.
"""
import json
import os

import pytest

from mprov_jobserver.plugins import repo_sync as repo_sync_module
from mprov_jobserver.plugins.repo_sync import repo_sync


class FakeSh:
  """ Records sh.wget calls instead of running wget. """

  def __init__(self):
    self.wget_calls = []

  def wget(self, args):
    self.wget_calls.append(args)


@pytest.fixture
def sh(monkeypatch):
  fake = FakeSh()
  monkeypatch.setattr(repo_sync_module, "sh", fake)
  return fake


@pytest.fixture
def plugin(js, tmp_path):
  plugin = repo_sync(js)
  plugin.repoDir = str(tmp_path / "repos")
  plugin.repoList = ["5"]
  return plugin


def test_mirror_goes_to_repo_dir_without_chdir(js, plugin, sh):
  js.session.routes["repos/5/"] = {"id": 5, "version": 3, "hosted_by": []}
  cwd = os.getcwd()
  plugin.handle_jobs()

  # no chdir: other plugin threads share the working directory (#131)
  assert os.getcwd() == cwd
  [args] = sh.wget_calls
  assert args[args.index("-P") + 1] == plugin.repoDir
  assert args[-1] == "http://mpcc/osrepos/5/"
  assert "-nH" in args and "--cut-dirs=1" in args
  # our version is recorded, and we're added as a host for the repo
  with open(f"{plugin.repoDir}/repo-versions") as vfile:
    assert json.load(vfile) == {"5": 3}
  assert js.session.patches == [("repos/5/?addjs", {"id": "5", "hosted_by": [1]})]


def test_up_to_date_repo_is_not_mirrored(js, plugin, sh):
  os.makedirs(plugin.repoDir)
  with open(f"{plugin.repoDir}/repo-versions", "w") as vfile:
    json.dump({"5": 3}, vfile)
  js.session.routes["repos/5/"] = {"id": 5, "version": 3, "hosted_by": [1]}
  plugin.handle_jobs()
  assert sh.wget_calls == []
  assert js.session.patches == []


def test_repos_are_listed_from_the_mpcc_without_a_repo_list(js, plugin, sh):
  plugin.repoList = None
  js.session.routes.update({"repos/": [{"id": 5}], "repos/5/": {"id": 5, "version": 3, "hosted_by": [1]}})
  plugin.handle_jobs()
  [args] = sh.wget_calls
  assert args[-1] == "http://mpcc/osrepos/5/"


def test_repos_it_no_longer_serves_are_removed(js, plugin, sh):
  plugin.repoList = None
  os.makedirs(f"{plugin.repoDir}/9")
  js.session.routes["repos/"] = []
  plugin.handle_jobs()
  assert not os.path.exists(f"{plugin.repoDir}/9")


def test_needs_the_webserver(js):
  js.jobmodules = ["repo-sync"]
  with pytest.raises(SystemExit):
    repo_sync(js).load_config()


@pytest.mark.xfail(strict=True, raises=TypeError,
                   reason="repo_sync compares status_code with the string \"404\", so a deleted repo crashes the sync (#141)")
def test_repo_deleted_from_the_mpcc_is_skipped(js, plugin, sh):
  plugin.repoList = ["4", "5"]
  js.session.routes.update({"repos/4/": (404, None), "repos/5/": {"id": 5, "version": 3, "hosted_by": [1]}})
  plugin.handle_jobs()
  assert len(sh.wget_calls) == 1
