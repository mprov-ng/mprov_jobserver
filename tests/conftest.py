"""
Shared fixtures for the plugin tests.

The plugins talk to the mPCC through ``js.session`` and write files and
manage processes on the host.  These fixtures give them a fake jobserver
whose session serves canned mPCC responses by URL, and stub out anything
that would touch processes, so ``handle_jobs()`` can run for real and the
tests check the files it writes.
"""
import json
import os
import subprocess

import pytest
import requests

MPROV_URL = "http://mpcc/"


class FakeResponse:
  """
  The parts of a requests.Response the plugins use.  A bytes body is served
  as a file download (``iter_content``); anything else as JSON.
  """

  def __init__(self, status_code=200, body=None):
    self.status_code = status_code
    if isinstance(body, bytes):
      self.content = body
      self.text = body.decode(errors="replace")
    else:
      self.content = json.dumps(body).encode()
      self.text = json.dumps(body)

  def json(self):
    return json.loads(self.text)

  def raise_for_status(self):
    if not 199 < self.status_code < 300:
      raise requests.HTTPError(f"{self.status_code} Error")

  def iter_content(self, chunk_size=1):
    for i in range(0, len(self.content), chunk_size):
      yield self.content[i:i + chunk_size]

  def __enter__(self):
    return self

  def __exit__(self, *exc):
    return False


class FakeSession:
  """
  Serves canned mPCC responses by URL path, relative to MPROV_URL, e.g.
  ``session.routes["systems/"] = [...]``.  A value can be a body (served as
  200) or a ``(status, body)`` tuple, for the mPCC's ``404`` with ``null``.
  Any URL without a route fails the test, so a test can't pass by reading
  something it didn't set up.

  POSTs and PATCHes are recorded in ``writes`` as ``(method, path, data)``,
  with ``data`` decoded from JSON (PATCHes also in ``patches`` as
  ``(path, data)``), and answer 200 with ``{}`` unless
  ``write_routes[(method, path)]`` says otherwise.
  """

  def __init__(self):
    self.routes = {}
    self.requests = []
    self.write_routes = {}
    self.writes = []
    self.headers = {}
    # PATCHes alone, as (path, data)
    self.patches = []

  def _path(self, url):
    assert url.startswith(MPROV_URL), f"request outside the mPCC: {url}"
    return url[len(MPROV_URL):]

  def _response(self, route):
    if isinstance(route, tuple):
      return FakeResponse(*route)
    return FakeResponse(200, route)

  def get(self, url, **kwargs):
    path = self._path(url)
    self.requests.append(("GET", path))
    assert path in self.routes, f"no canned mPCC response for GET {path}"
    return self._response(self.routes[path])

  def _write(self, method, url, data=None, **kwargs):
    path = self._path(url)
    self.requests.append((method, path))
    self.writes.append((method, path, json.loads(data) if data else None))
    if method == "PATCH":
      self.patches.append(self.writes[-1][1:])
    return self._response(self.write_routes.get((method, path), {}))

  def post(self, url, data=None, **kwargs):
    return self._write("POST", url, data)

  def patch(self, url, data=None, **kwargs):
    return self._write("PATCH", url, data)


class FakeJobServer:
  """ The parts of the JobServer (app.py) that plugins use. """

  def __init__(self):
    self.mprovURL = MPROV_URL
    self.session = FakeSession()
    self.id = 1
    self.firstrun = False
    self.config_data = {}
    self.jobmodules = []
    self.apikey = "test-key"
    self.runonce = False
    self.running = True
    self.loadmon = True
    self.job_updates = []
    self.job_updates_by_id = []
    # set to False to report no pending jobs to the plugin.
    self.has_jobs = True

  def update_job_status(self, module, status, jobid=None, jobquery=None, params_filter=None):
    # Report a pending job, so set_job_running() lets the plugin run.
    # params_filter is accepted to match the real JobServer signature; the
    # fake has no job list to filter.
    self.job_updates.append((module, status, jobquery))
    self.job_updates_by_id.append((module, status, jobid))
    return self.has_jobs


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


class Commands:
  """
  Records the shell commands a plugin runs with os.system.  Each succeeds
  unless it contains a string in ``fail``.
  """

  def __init__(self):
    self.run = []
    self.fail = []

  def __call__(self, cmd):
    self.run.append(cmd)
    return 1 if any(f in cmd for f in self.fail) else 0

  def matching(self, text):
    return [cmd for cmd in self.run if text in cmd]


@pytest.fixture
def commands(monkeypatch):
  """ Let the plugin "run" shell commands, and record them.  Use after no_processes. """
  recorder = Commands()
  monkeypatch.setattr(os, "system", recorder)
  return recorder
