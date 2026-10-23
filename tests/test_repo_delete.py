"""
repo_delete is still the base plugin: the mPCC queues one "Delete Repos"
job, with no params, when a repo is deleted.
"""
from mprov_jobserver.plugins.repo_delete import repo_delete


def test_repo_delete_is_still_the_base_plugin(js, monkeypatch):
  # It marks repo-delete jobs done after the base class's simulated work, without deleting anything.
  monkeypatch.setattr("mprov_jobserver.plugins.plugin.sleep", lambda s: None)
  repo_delete(js).handle_jobs()
  assert [u[:2] for u in js.job_updates] == [("repo-delete", 2), ("repo-delete", 4)]
