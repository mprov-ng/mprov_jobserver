"""
The mprov_jobserver launcher runs ``python -m mprov_jobserver.main``, so the
process's exit status is whatever the module does when run as __main__.
"""
import runpy
import sys
import pytest
import mprov_jobserver.app as app


@pytest.mark.parametrize("status", [0, 1])
def test_exits_with_the_job_servers_status(monkeypatch, tmp_path, status):
  # main()'s return value was dropped, so a runonce session with failed
  # scripts exited 0 (#221)
  class FakeJobServer:
    def __init__(self, **kwargs):
      pass
    def start(self):
      return status
  monkeypatch.setattr(app, "JobServer", FakeJobServer)
  monkeypatch.setattr(sys, "argv", ["mprov_jobserver", "-r", "-c", str(tmp_path / "jobserver.yaml")])
  sys.modules.pop("mprov_jobserver.main", None)
  with pytest.raises(SystemExit) as exit:
    runpy.run_module("mprov_jobserver.main", run_name="__main__")
  assert exit.value.code == status
