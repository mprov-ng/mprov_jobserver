"""
repo_sync mirrors each repo whose mPCC version differs from ours, from the
mPCC's osrepos/, into repoDir/<repo id>.
"""
import json
import os

import pytest

from mprov_jobserver.plugins import repo_sync as repo_sync_module
from mprov_jobserver.plugins.repo_sync import repo_sync

# Name of the ownership marker file mprov writes into the repo
# directories it creates; the sweep may only delete marked directories (#171).
MARKER = ".mprov-managed"


def mark_owned(d):
  """Create a directory the way repo_sync creates its own: with a marker."""
  os.makedirs(d)
  with open(f"{d}/{MARKER}", "w") as marker:
    marker.write("mprov owns this directory\n")


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
  mark_owned(f"{plugin.repoDir}/9")
  js.session.routes["repos/"] = []
  plugin.handle_jobs()
  assert not os.path.exists(f"{plugin.repoDir}/9")


def test_sweep_never_touches_unmarked_directories(js, plugin, sh):
  """#171: a directory mprov did not create must never be deleted."""
  plugin.repoList = None
  os.makedirs(f"{plugin.repoDir}/operator-data")
  with open(f"{plugin.repoDir}/operator-data/precious", "w") as f:
    f.write("keep")
  mark_owned(f"{plugin.repoDir}/9")
  js.session.routes["repos/"] = []
  plugin.handle_jobs()
  with open(f"{plugin.repoDir}/operator-data/precious") as f:
    assert f.read() == "keep"
  assert not os.path.exists(f"{plugin.repoDir}/9")


def test_mirrored_repo_directory_carries_the_mprov_marker(js, plugin, sh):
  js.session.routes["repos/5/"] = {"id": 5, "version": 3, "hosted_by": [1]}
  plugin.handle_jobs()
  assert os.path.exists(f"{plugin.repoDir}/5/{MARKER}")


def test_load_config_refuses_repo_dir_outside_the_data_root(js, tmp_path):
  js.jobmodules = ["repo-sync", "mprov-webserver"]
  js.config_data = {"repo-sync": {
    "dataRoot": str(tmp_path / "mprov"),
    "repoDir": str(tmp_path / "elsewhere"),
  }}
  with pytest.raises(SystemExit):
    repo_sync(js).load_config()


def test_load_config_refuses_without_a_data_root(js, tmp_path):
  js.jobmodules = ["repo-sync", "mprov-webserver"]
  js.config_data = {"repo-sync": {"repoDir": str(tmp_path / "repos")}}
  with pytest.raises(SystemExit):
    repo_sync(js).load_config()


def test_load_config_accepts_repo_dir_inside_the_data_root(js, tmp_path):
  js.jobmodules = ["repo-sync", "mprov-webserver"]
  root = tmp_path / "mprov"
  js.config_data = {"repo-sync": {
    "dataRoot": str(root),
    "repoDir": str(root / "repos"),
  }}
  plugin = repo_sync(js)
  assert plugin.load_config() is True
  assert plugin.repoDir == str(root / "repos")


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


@pytest.mark.parametrize("configured", [[4, 5], ["4", "5"], [4, "5"]])
def test_configured_repo_ids_are_kept_through_the_sweep(js, tmp_path, sh, configured):
  # YAML reads `repoList: [4, 5]` as ints, which never matched the
  # directory names, so every pass deleted and re-mirrored them (#235)
  js.jobmodules = ["repo-sync", "mprov-webserver"]
  root = tmp_path / "mprov"
  js.config_data = {"repo-sync": {"dataRoot": str(root), "repoDir": str(root / "repos"), "repoList": configured}}
  plugin = repo_sync(js)
  assert plugin.load_config() is True
  assert plugin.repoList == ["4", "5"]
  mark_owned(f"{plugin.repoDir}/4")
  mark_owned(f"{plugin.repoDir}/5")
  with open(f"{plugin.repoDir}/repo-versions", "w") as f:
    f.write('{"4": 3, "5": 3}')
  js.session.routes.update({"repos/4/": {"id": 4, "version": 3, "hosted_by": [1]},
                            "repos/5/": {"id": 5, "version": 3, "hosted_by": [1]}})
  plugin.handle_jobs()
  assert os.path.exists(f"{plugin.repoDir}/4") and os.path.exists(f"{plugin.repoDir}/5")
  assert sh.wget_calls == []


def test_a_single_configured_repo_id_is_a_list_of_one(js, tmp_path):
  js.jobmodules = ["repo-sync", "mprov-webserver"]
  root = tmp_path / "mprov"
  js.config_data = {"repo-sync": {"dataRoot": str(root), "repoDir": str(root / "repos"), "repoList": 4}}
  plugin = repo_sync(js)
  assert plugin.load_config() is True
  assert plugin.repoList == ["4"]
