"""
repo_update (rhel) mirrors a repo with dnf reposync into repoDir/<repo id>.
"""
import os

import pytest

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
  assert js.session.patches == [("repos/5/", {"id": 5, "hosted_by": [1], "update": False})]
  assert js.job_updates[-1][:2] == ("repo-update", 4)


def test_reposync_failure_fails_the_job(plugin, js, monkeypatch):
  monkeypatch.setattr(os, "system", lambda cmd: 1)
  plugin.handle_jobs()
  assert js.session.patches == []
  assert js.job_updates[-1][:2] == ("repo-update", 3)
