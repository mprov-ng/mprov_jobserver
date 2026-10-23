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
