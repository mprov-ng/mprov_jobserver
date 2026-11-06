"""
The mProv-Version header (#287): the job server sends its own major.minor
on every request to the mPCC, and refuses a reply from an mPCC that speaks
a different one.
"""
import socket
import types

import pytest
import requests

import mprov_jobserver.app as app
from mprov_jobserver import version
from mprov_jobserver.app import JobServer
from mprov_jobserver.script import MProvScript


@pytest.fixture(autouse=True)
def forget_reported_mismatches():
  """ The mismatch log is only written once per version pair. """
  version._reported.clear()


def reply(headers=None, url="http://mpcc/"):
  return types.SimpleNamespace(headers=headers or {}, url=url)


# api_version()

@pytest.mark.parametrize("full,expected", [
  ("0.1.0", "0.1"),
  ("0.1.0.dev7", "0.1"),
  ("0.1", "0.1"),
  ("10.20.30", "10.20"),
  ("0.0.63.dev219", "0.0"),
])
def test_the_major_and_minor_are_sent_not_the_patch(full, expected):
  assert version.api_version(full) == expected


@pytest.mark.parametrize("full", ["", "not-a-version", "1"])
def test_a_version_that_isnt_major_minor_matches_nothing(full):
  assert version.api_version(full) == "0.0"


def test_the_running_version_is_used_when_none_is_given(monkeypatch):
  monkeypatch.setattr(version, "jobserver_version", lambda: "3.4.5")
  assert version.api_version() == "3.4"


# jobserver_version()

def test_the_installed_packages_version_is_used(monkeypatch):
  monkeypatch.setattr(version, "package_version", lambda name: "1.2.3")
  assert version.jobserver_version() == "1.2.3"


def test_a_source_checkout_falls_back_to_the_VERSION_file(monkeypatch, tmp_path):
  def not_installed(name):
    raise version.PackageNotFoundError(name)
  monkeypatch.setattr(version, "package_version", not_installed)
  version_file = tmp_path / "VERSION"
  version_file.write_text("9.8.7.dev0")
  monkeypatch.setattr(version, "VERSION_FILE", str(version_file))
  assert version.jobserver_version() == "9.8.7.dev0"


def test_neither_one_doesnt_stop_the_job_server_starting(monkeypatch, tmp_path):
  def not_installed(name):
    raise version.PackageNotFoundError(name)
  monkeypatch.setattr(version, "package_version", not_installed)
  monkeypatch.setattr(version, "VERSION_FILE", str(tmp_path / "missing"))
  assert version.jobserver_version() == "0.0.0"


# check_response()

def test_a_matching_mPCC_is_let_through(monkeypatch):
  monkeypatch.setattr(version, "api_version", lambda *a: "0.1")
  response = reply({version.VERSION_HEADER: "0.1"})
  assert version.check_response(response) is response


def test_surrounding_space_in_the_header_still_matches(monkeypatch):
  monkeypatch.setattr(version, "api_version", lambda *a: "0.1")
  response = reply({version.VERSION_HEADER: " 0.1 "})
  assert version.check_response(response) is response


def test_a_mismatched_mPCC_is_refused(monkeypatch):
  monkeypatch.setattr(version, "api_version", lambda *a: "0.1")
  with pytest.raises(version.VersionMismatch):
    version.check_response(reply({version.VERSION_HEADER: "0.2"}))


def test_a_refusal_is_a_request_failure_so_callers_already_handle_it(monkeypatch):
  monkeypatch.setattr(version, "api_version", lambda *a: "0.1")
  with pytest.raises(requests.exceptions.RequestException):
    version.check_response(reply({version.VERSION_HEADER: "0.2"}))


def test_the_mismatch_is_logged_once_per_pair_not_once_per_request(monkeypatch, caplog):
  monkeypatch.setattr(version, "api_version", lambda *a: "0.1")
  for _ in range(3):
    with pytest.raises(version.VersionMismatch):
      version.check_response(reply({version.VERSION_HEADER: "0.2"}))
  mismatches = [r for r in caplog.records if "0.2" in r.getMessage()]
  assert len(mismatches) == 1
  assert "0.1" in mismatches[0].getMessage()


def test_an_mPCC_that_sends_no_version_is_refused(monkeypatch):
  """ Too old to be version-locked counts as a mismatch, so every mPCC has
  to ship #452 before a job server of this version can talk to it. """
  monkeypatch.setattr(version, "api_version", lambda *a: "0.1")
  with pytest.raises(version.VersionMismatch):
    version.check_response(reply())


def test_a_missing_version_is_logged_as_missing_not_as_a_version(monkeypatch, caplog):
  monkeypatch.setattr(version, "api_version", lambda *a: "0.1")
  with pytest.raises(version.VersionMismatch):
    version.check_response(reply())
  assert "sends no API version" in caplog.text
  assert "None" not in caplog.text


# MPROV_SKIP_VERSION_CHECK

@pytest.mark.parametrize("value", ["1", "true", "yes", "anything"])
def test_the_escape_hatch_skips_the_check(monkeypatch, value):
  monkeypatch.setenv(version.SKIP_CHECK_ENV, value)
  monkeypatch.setattr(version, "api_version", lambda *a: "0.1")
  response = reply({version.VERSION_HEADER: "0.2"})
  assert version.check_response(response) is response


def test_the_escape_hatch_is_how_you_talk_to_an_mPCC_without_the_header(monkeypatch):
  monkeypatch.setenv(version.SKIP_CHECK_ENV, "1")
  response = reply()
  assert version.check_response(response) is response


@pytest.mark.parametrize("value", ["", "0", "false", "no"])
def test_the_check_is_on_unless_the_escape_hatch_is_set(monkeypatch, value):
  monkeypatch.setenv(version.SKIP_CHECK_ENV, value)
  monkeypatch.setattr(version, "api_version", lambda *a: "0.1")
  with pytest.raises(version.VersionMismatch):
    version.check_response(reply({version.VERSION_HEADER: "0.2"}))


def test_the_check_is_on_when_the_escape_hatch_is_unset(monkeypatch):
  monkeypatch.delenv(version.SKIP_CHECK_ENV, raising=False)
  assert version.skip_version_check() is False


# install()

def test_the_version_is_sent_on_a_sessions_requests(monkeypatch):
  monkeypatch.setattr(version, "api_version", lambda *a: "0.1")
  session = version.install(requests.Session())
  assert session.headers[version.VERSION_HEADER] == "0.1"


def test_replies_are_checked(monkeypatch):
  session = version.install(requests.Session())
  assert version.check_response in session.hooks["response"]


def test_installing_twice_doesnt_check_a_reply_twice():
  session = requests.Session()
  version.install(session)
  version.install(session)
  assert session.hooks["response"].count(version.check_response) == 1


# the sessions that talk to the mPCC

def start_session(obj, monkeypatch):
  obj.session = requests.Session()
  obj.apikey = "test-key"
  obj.caBundle = None
  obj.mprovURL = "http://mpcc/"
  obj.myaddress = None
  monkeypatch.setattr(obj.session, "get", lambda *a, **k: types.SimpleNamespace(status_code=200))
  return obj


def test_the_job_servers_session_sends_and_checks_the_version(monkeypatch):
  server = start_session(JobServer.__new__(JobServer), monkeypatch)
  server.startSession()
  assert version.VERSION_HEADER in server.session.headers
  assert version.check_response in server.session.hooks["response"]


def started_script(monkeypatch, script_version=None):
  """
  An MProvScript that has run startSession().  startSession() reads the
  address off the live connection (#160), so the reply has to carry a real
  connected socket.
  """
  listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  listener.bind(("127.0.0.1", 0))
  listener.listen(1)
  client = socket.create_connection(listener.getsockname())
  try:
    script = start_session(MProvScript.__new__(MProvScript), monkeypatch)
    script.script_version = script_version
    connected = types.SimpleNamespace(status_code=200, raw=client)
    monkeypatch.setattr(script.session, "get", lambda *a, **k: connected)
    assert script.startSession() is True
    return script
  finally:
    client.close()
    listener.close()


def test_a_script_reports_the_version_it_sets(monkeypatch):
  script = started_script(monkeypatch, script_version="0.6")
  assert script.session.headers[version.VERSION_HEADER] == "0.6"


def test_a_script_that_sets_no_version_sends_none(monkeypatch):
  script = started_script(monkeypatch)
  assert version.VERSION_HEADER not in script.session.headers


@pytest.mark.parametrize("script_version", ["0.6", None])
def test_a_script_never_refuses_the_mPCCs_version(monkeypatch, script_version):
  """
  Scripts are baked into images that can't be rebuilt with every mPCC
  release, so an mPCC on a newer version has to be able to answer them: the
  mPCC's exemption list is useless if the script refuses the reply itself.
  """
  script = started_script(monkeypatch, script_version=script_version)
  assert script.session.hooks["response"] == []
  # the job server's own check would refuse this reply; the script takes it
  monkeypatch.setattr(version, "api_version", lambda *a: "0.1")
  response = reply({version.VERSION_HEADER: "0.9"})
  for hook in script.session.hooks["response"]:
    response = hook(response)


def test_a_per_address_session_checks_the_version(monkeypatch):
  """ #114's per-address sessions copy the headers, but hooks aren't
  copied with them. """
  server = JobServer.__new__(JobServer)
  server.session = version.install(requests.Session())
  server._addressSessions = None
  monkeypatch.setattr(app, "SourceAddressAdapter", lambda address: requests.adapters.HTTPAdapter())
  session = server.address_session("172.16.0.5")
  assert session is not server.session
  assert version.VERSION_HEADER in session.headers
  assert version.check_response in session.hooks["response"]


# the script runner (#303): warns instead of refusing

def test_a_script_runner_is_not_refused_a_mismatched_reply(monkeypatch):
  """
  image_update installs a job server into every image it builds and runs it
  with -d; refusing the reply there hangs the build rather than failing it.
  """
  monkeypatch.setattr(version, "api_version", lambda *a: "0.1")
  response = reply({version.VERSION_HEADER: "0.2"})
  assert version.warn_response(response) is response


def test_a_script_runner_is_not_refused_an_mPCC_with_no_version(monkeypatch):
  """ The case sites actually hit: a pre-0.6 mPCC sends no header at all. """
  monkeypatch.setattr(version, "api_version", lambda *a: "0.1")
  response = reply()
  assert version.warn_response(response) is response


def test_a_script_runner_says_so_once_per_pair(monkeypatch, caplog):
  monkeypatch.setattr(version, "api_version", lambda *a: "0.1")
  for _ in range(3):
    version.warn_response(reply())
  said = [r for r in caplog.records if "sends no API version" in r.getMessage()]
  assert len(said) == 1
  assert said[0].levelname == "WARNING"


def test_a_matching_mPCC_says_nothing_to_a_script_runner(monkeypatch, caplog):
  monkeypatch.setattr(version, "api_version", lambda *a: "0.1")
  version.warn_response(reply({version.VERSION_HEADER: "0.1"}))
  assert caplog.records == []


def test_a_script_runner_still_sends_its_own_version(monkeypatch):
  monkeypatch.setattr(version, "api_version", lambda *a: "0.1")
  session = version.install(requests.Session(), enforce=False)
  assert session.headers[version.VERSION_HEADER] == "0.1"
  assert version.warn_response in session.hooks["response"]
  assert version.check_response not in session.hooks["response"]


def test_installing_without_enforcement_replaces_the_refusing_hook():
  session = version.install(requests.Session())
  version.install(session, enforce=False)
  assert session.hooks["response"] == [version.warn_response]


def test_installing_twice_without_enforcement_doesnt_warn_twice():
  session = requests.Session()
  version.install(session, enforce=False)
  version.install(session, enforce=False)
  assert session.hooks["response"].count(version.warn_response) == 1


def test_a_job_server_that_registers_still_refuses(monkeypatch):
  server = start_session(JobServer.__new__(JobServer), monkeypatch)
  server.register = True
  server.startSession()
  assert version.check_response in server.session.hooks["response"]
  assert version.warn_response not in server.session.hooks["response"]


def test_a_script_runner_session_warns_instead(monkeypatch):
  """ -d (and -m) set register = False before startSession() runs. """
  server = start_session(JobServer.__new__(JobServer), monkeypatch)
  server.register = False
  server.startSession()
  assert version.warn_response in server.session.hooks["response"]
  assert version.check_response not in server.session.hooks["response"]

