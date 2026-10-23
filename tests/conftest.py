"""
Shared fixtures for the plugin tests.

The plugins talk to the mPCC through ``js.session`` and write files and
manage processes on the host.  These fixtures give them a fake jobserver
whose session serves canned mPCC responses by URL, and stub out anything
that would touch processes, so ``handle_jobs()`` can run for real and the
tests check the files it writes.
"""
import json
import subprocess

import pytest

MPROV_URL = "http://mpcc/"


class FakeResponse:
  """ The parts of a requests.Response the plugins use. """

  def __init__(self, status_code=200, body=None):
    self.status_code = status_code
    self._body = body
    self.text = json.dumps(body)

  def json(self):
    return json.loads(self.text)


class FakeSession:
  """
  Serves canned mPCC responses by URL path, relative to MPROV_URL, e.g.
  ``session.routes["systems/"] = [...]``.  A value can be a body (served as
  200) or a ``(status, body)`` tuple, for the mPCC's ``404`` with ``null``.
  Any URL without a route fails the test, so a test can't pass by reading
  something it didn't set up.
  """

  def __init__(self):
    self.routes = {}
    self.requests = []
    self.patches = []

  def get(self, url, **kwargs):
    assert url.startswith(MPROV_URL), f"request outside the mPCC: {url}"
    path = url[len(MPROV_URL):]
    self.requests.append(("GET", path))
    assert path in self.routes, f"no canned mPCC response for GET {path}"
    route = self.routes[path]
    if isinstance(route, tuple):
      return FakeResponse(*route)
    return FakeResponse(200, route)

  def patch(self, url, data=None, **kwargs):
    """ Records the PATCH (path, decoded body) and answers 200. """
    assert url.startswith(MPROV_URL), f"request outside the mPCC: {url}"
    path = url[len(MPROV_URL):]
    self.requests.append(("PATCH", path))
    self.patches.append((path, json.loads(data) if data else None))
    return FakeResponse(200, {})


class FakeJobServer:
  """ The parts of the JobServer (app.py) that plugins use. """

  def __init__(self):
    self.mprovURL = MPROV_URL
    self.session = FakeSession()
    self.id = 1
    self.firstrun = False
    self.config_data = {}
    self.job_updates = []

  def update_job_status(self, module, status, jobid=None, jobquery=None):
    # Report a pending job, so set_job_running() lets the plugin run.
    self.job_updates.append((module, status, jobquery))
    return True


@pytest.fixture
def js():
  return FakeJobServer()


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
  """
  The dnsmasq plugins look for a running dnsmasq to kill or start.  Report
  none running, and fail the test if one tries to start or kill a process.
  """
  import psutil
  monkeypatch.setattr(psutil, "process_iter", lambda *a, **k: iter([]))

  def refuse(*args, **kwargs):
    pytest.fail(f"plugin tried to run a process: {args}")
  monkeypatch.setattr(subprocess, "run", refuse)
  monkeypatch.setattr("os.kill", refuse)
  monkeypatch.setattr("os.system", refuse)
