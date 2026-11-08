"""
An mPCC that can't be reached (e.g. while it restarts) ended a job
module's thread with a traceback (#337): update_job_status() and the
plugins' own calls let requests' ConnectionError out of the thread.  Now
the poll is just "no jobs", and a module that hits it mid-run ends with one
log line; the job server starts it again on its next heartbeat.
"""
import pytest
import requests

import mprov_jobserver.app as app
from mprov_jobserver.app import JobServer
from mprov_jobserver.plugins.plugin import JobServerPlugin
from conftest import FakeSession, MPROV_URL


@pytest.fixture
def server(monkeypatch):
  server = JobServer.__new__(JobServer)
  server.session = FakeSession()
  server.mprovURL = MPROV_URL
  server.id = 5
  server.features = frozenset({"job-claim"})
  return server


def down(*args, **kwargs):
  raise requests.exceptions.ConnectionError("Connection refused")


def test_an_unreachable_mpcc_means_no_jobs_not_a_traceback(server, monkeypatch, caplog):
  monkeypatch.setattr(server.session, "get", down)
  for status in (2, 3, 4):
    assert server.update_job_status("libgenders", status) is False
  # said once while it's down, not on every run
  assert caplog.text.count("libgenders: unable to reach the mPCC") == 1
  assert "Connection refused" in caplog.text


def test_each_module_says_so_once(server, monkeypatch, caplog):
  monkeypatch.setattr(server.session, "get", down)
  for _ in range(3):
    server.update_job_status("libgenders", 2)
    server.update_job_status("dnsmasq", 2)
  assert caplog.text.count("libgenders: unable to reach the mPCC") == 1
  assert caplog.text.count("dnsmasq: unable to reach the mPCC") == 1


def test_reaching_it_again_is_logged_and_rearms_the_message(server, monkeypatch, caplog):
  monkeypatch.setattr(server.session, "get", down)
  server.update_job_status("libgenders", 2)
  monkeypatch.undo()
  server.session.routes["jobs/?module=libgenders&status=1"] = []
  server.update_job_status("libgenders", 2)
  assert "libgenders: reached the mPCC again" in caplog.text
  monkeypatch.setattr(server.session, "get", down)
  server.update_job_status("libgenders", 2)
  assert caplog.text.count("libgenders: unable to reach the mPCC") == 2


def test_a_failed_update_skips_that_job(server, monkeypatch, caplog):
  server.features = frozenset()
  server.session.routes["jobs/?jobserver=5&module=libgenders&status=2"] = [
    {"id": 7, "status": 2, "module": "libgenders"}, {"id": 8, "status": 2, "module": "libgenders"}]
  monkeypatch.setattr(server.session, "patch", down)
  assert server.update_job_status("libgenders", 4) == 0
  assert "libgenders: unable to reach the mPCC" in caplog.text


def test_a_failed_claim_claims_nothing(server, monkeypatch, caplog):
  server.session.routes["jobs/?module=libgenders&status=1"] = [{"id": 7, "status": 1, "module": "libgenders"}]
  monkeypatch.setattr(server.session, "post", down)
  assert server.update_job_status("libgenders", 2) == 0
  assert "libgenders: unable to reach the mPCC" in caplog.text


class Fetches(JobServerPlugin):
  """ A module that calls the mPCC itself, as most do. """
  jobModule = "fetches"

  def handle_jobs(self):
    self.js.session.get(self.js.mprovURL + "systems/")


def test_a_module_that_loses_the_mpcc_mid_run_ends_with_one_line(server, monkeypatch, caplog):
  monkeypatch.setattr(server.session, "get", down)
  server.config_data = {"global": {}}
  for _ in range(3):
    module = Fetches(server)
    module.run()
    assert module.failed is True
  assert caplog.text.count("fetches: unable to reach the mPCC") == 1
  assert "Traceback" not in caplog.text


def test_in_a_thread_too(server, monkeypatch, capsys):
  """ What the main loop does: no "Exception in thread" on stderr. """
  monkeypatch.setattr(server.session, "get", down)
  server.config_data = {"global": {}}
  module = Fetches(server)
  module.start()
  module.join()
  assert "Exception in thread" not in capsys.readouterr().err


def test_other_errors_still_end_the_thread_loudly(server):
  """ Only the mPCC being unreachable is expected; a bug still shows. """
  class Broken(JobServerPlugin):
    jobModule = "broken"

    def handle_jobs(self):
      raise KeyError("id")
  server.config_data = {"global": {}}
  with pytest.raises(KeyError):
    Broken(server).run()


def test_a_fake_job_server_without_the_helper_still_logs(caplog):
  class JS:
    config_data = {"global": {}}
    mprovURL = MPROV_URL
    session = type("S", (), {"get": staticmethod(down)})()
  module = Fetches(JS())
  module.run()
  assert "fetches: unable to reach the mPCC" in caplog.text
