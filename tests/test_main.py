"""
The mprov_jobserver launcher runs ``python -m mprov_jobserver.main``, so the
process's exit status is whatever the module does when run as __main__.
"""
import runpy
import sys
import pytest
import mprov_jobserver.app as app
import mprov_jobserver.main as main_module


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


# the first-start config copy (#346)

def packaged(package):
  import importlib.resources
  return sorted(e.name for e in importlib.resources.files(package).iterdir() if e.name.endswith(".yaml"))


def test_the_default_config_is_copied_private(tmp_path):
  import stat
  from mprov_jobserver.main import copy_default_config
  assert copy_default_config(str(tmp_path)) is True
  assert sorted(p.name for p in tmp_path.glob("*.yaml")) == packaged("mprov_jobserver")
  assert sorted(p.name for p in (tmp_path / "plugins").glob("*.yaml")) == packaged("mprov_jobserver.plugins")
  for path in list(tmp_path.glob("*.yaml")) + list((tmp_path / "plugins").glob("*.yaml")):
    assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
  import importlib.resources
  assert (tmp_path / "jobserver.yaml").read_text() == \
    importlib.resources.files("mprov_jobserver").joinpath("jobserver.yaml").read_text()


def test_the_copy_uses_no_deprecated_api(tmp_path):
  import warnings
  from mprov_jobserver.main import copy_default_config
  with warnings.catch_warnings():
    warnings.simplefilter("error", DeprecationWarning)
    assert copy_default_config(str(tmp_path)) is True


def test_a_copy_that_fails_says_so(tmp_path, caplog):
  from mprov_jobserver.main import copy_default_config
  blocked = tmp_path / "blocked"
  blocked.write_text("a file, not a directory")
  assert copy_default_config(str(blocked)) is False
  assert "copying the default config" in caplog.text


def test_a_first_start_copies_the_config_and_stops(monkeypatch):
  copied = []
  monkeypatch.setattr(main_module.sys, "argv", ["mprov_jobserver"])
  monkeypatch.setattr(main_module.os.path, "isfile", lambda p: False)
  monkeypatch.setattr(main_module, "copy_default_config", lambda dest: copied.append(dest) or True)
  with pytest.raises(SystemExit) as exit:
    main_module.main()
  assert exit.value.code == 1
  assert copied == ["/etc/mprov"]
