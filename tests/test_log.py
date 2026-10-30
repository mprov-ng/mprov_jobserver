"""
The job server's log lines (#104): "[E] image-update: message", with the
journald priority prefix when stdout is the journal, and one line per job
transition (#103).
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

from mprov_jobserver import log as joblog
from mprov_jobserver.app import JobServer
from conftest import FakeSession, MPROV_URL


@pytest.fixture(autouse=True)
def default_log(monkeypatch):
  monkeypatch.delenv("JOURNAL_STREAM", raising=False)
  joblog.configure("info")
  yield
  monkeypatch.delenv("JOURNAL_STREAM", raising=False)
  joblog.configure("info")


@pytest.mark.parametrize("name, short", [
  ("mprov_jobserver.app", "app"),
  ("mprov_jobserver.plugins.image_update", "image-update"),
  ("mprov_jobserver.plugins.image_update_mod.rhel", "image-update/rhel"),
  ("mprov_jobserver.plugins.dnsmasq_mod.config", "dnsmasq/config"),
  ("mprov_jobserver.script", "script"),
])
def test_module_names(name, short):
  assert joblog.module_name(name) == short


def test_lines_are_tagged_with_level_and_module(capsys):
  log = joblog.get_logger("mprov_jobserver.plugins.image_update")
  log.error("unable to create initramfs")
  log.warning("slow")
  log.info("\nbuilding")
  log.debug("hidden at info")
  assert capsys.readouterr().out.splitlines() == [
    "[E] image-update: unable to create initramfs",
    "[W] image-update: slow",
    "[I] image-update: building",
  ]


def test_debug_level_shows_debug_lines(capsys):
  assert joblog.configure("debug")
  joblog.get_logger("mprov_jobserver.app").debug("GET http://mpcc/jobs/")
  assert capsys.readouterr().out == "[D] app: GET http://mpcc/jobs/\n"


def test_warning_level_hides_info(capsys):
  assert joblog.configure("warning")
  log = joblog.get_logger("mprov_jobserver.app")
  log.info("chatter")
  log.warning("look")
  assert capsys.readouterr().out == "[W] app: look\n"


def test_an_unknown_level_is_refused():
  assert joblog.configure("loud") is False


def journal_stdout(tmp_path, monkeypatch, matching=True):
  out = open(tmp_path / "journal", "w+")
  st = os.fstat(out.fileno())
  monkeypatch.setattr(sys, "stdout", out)
  monkeypatch.setenv("JOURNAL_STREAM", f"{st.st_dev}:{st.st_ino + (0 if matching else 1)}")
  return out


def test_journald_gets_the_priority_prefix(tmp_path, monkeypatch):
  out = journal_stdout(tmp_path, monkeypatch)
  joblog.configure("debug")
  log = joblog.get_logger("mprov_jobserver.plugins.dnsmasq")
  log.error("e"); log.warning("w"); log.info("i"); log.debug("d")
  out.seek(0)
  assert out.read().splitlines() == ["<3>[E] dnsmasq: e", "<4>[W] dnsmasq: w", "<6>[I] dnsmasq: i", "<7>[D] dnsmasq: d"]


def test_no_prefix_when_stdout_isnt_the_journal(tmp_path, monkeypatch):
  # e.g. a child of the unit with its own stdout, or docker logs
  out = journal_stdout(tmp_path, monkeypatch, matching=False)
  joblog.configure("info")
  joblog.get_logger("mprov_jobserver.app").error("e")
  out.seek(0)
  assert out.read() == "[E] app: e\n"


def test_optional_timestamps(capsys):
  joblog.configure("info", timestamps=True)
  joblog.get_logger("mprov_jobserver.app").info("x")
  line = capsys.readouterr().out
  assert line.endswith(" [I] app: x\n")
  datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")


# job transitions (#103)

@pytest.fixture
def server():
  server = JobServer.__new__(JobServer)
  server.session = FakeSession()
  server.mprovURL = MPROV_URL
  server.id = 5
  return server


def test_claiming_logs_job_started(server, capsys):
  server.session.routes["jobs/?module=image-update&status=1"] = [
    {"id": 42, "status": 1, "module": "image-update", "params": {"imageId": "compute"}}]
  server.update_job_status("image-update", 2)
  assert "[I] app: job 42 image-update compute started\n" in capsys.readouterr().out


@pytest.mark.parametrize("status, line", [
  (4, "[I] app: job 43 repo-update 7 succeeded ("),
  (3, "[E] app: job 43 repo-update 7 failed ("),
])
def test_finishing_logs_the_outcome_and_duration(server, capsys, status, line):
  started = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat().replace("+00:00", "Z")
  server.session.routes["jobs/43/"] = {"id": 43, "status": 2, "module": "repo-update",
                                       "params": {"repo_id": 7}, "start_time": started}
  server.update_job_status("repo-update", status, jobid=43)
  out = capsys.readouterr().out
  assert line in out
  seconds = int(out.split(line)[1].split("s)")[0])
  assert 89 <= seconds <= 120


def test_a_job_without_a_start_time_has_no_duration(server, capsys):
  server.session.routes["jobs/44/"] = {"id": 44, "status": 2, "module": "libgenders", "start_time": None}
  server.update_job_status("libgenders", 4, jobid=44)
  assert "[I] app: job 44 libgenders succeeded\n" in capsys.readouterr().out
