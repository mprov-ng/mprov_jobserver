"""
How the job server authenticates (mprov_control_center#381): with its own
certificate, else one it enrolls for with a token, else the shared API key
(deprecated, until 0.8.0).  A refused certificate is dropped and the job
server enrolls again; a certificate in its renewal window is renewed.
"""
import datetime
import json
import os

import pytest

import mprov_jobserver.app as app
from mprov_jobserver import identity as ident
from mprov_jobserver.app import JobServer
from conftest import FakeResponse, FakeSession
from test_identity import NOW, FakeCA, credential

URL = "https://mpcc/"


@pytest.fixture(scope="module")
def ca():
  return FakeCA()


class Session(FakeSession):
  """ The conftest session, for an mPCC on https://. """

  def __init__(self):
    super().__init__()
    self.cert = None
    self.verify = True

  def _path(self, url):
    # an mPCC on https://, as a certificate needs
    assert url.startswith(URL), f"request outside the mPCC: {url}"
    return url[len(URL):]


@pytest.fixture
def server(tmp_path, monkeypatch):
  server = JobServer.__new__(JobServer)
  server.session = Session()
  server.mprovURL = URL
  server.configfile = str(tmp_path / "etc" / "jobserver.yaml")
  (tmp_path / "etc").mkdir()
  server.tlsDir = str(tmp_path / "tls")
  server.enrollToken = ""
  server.enrollTokenFile = str(tmp_path / "etc" / "enroll_token")
  server.caFingerprint = ""
  server.caBundle = "/etc/pki/tls/certs/ca-bundle.crt"
  server._caBundleSet = False
  server.register = True
  server.apikey = ""
  server.hostId = "host-1"
  server.features = frozenset()
  monkeypatch.setattr(app.platform, "node", lambda: "js01.cluster.example")
  monkeypatch.setattr(app.time, "sleep", lambda s: None)
  return server


def enrolls_with(ca, monkeypatch, reply=None):
  """ Answer enrollments as the mPCC does; returns the requests made. """
  made = []

  def enroll(url, verify, token, host_id, name):
    made.append({"url": url, "verify": verify, "token": token, "host_id": host_id, "name": name})
    if reply is not None:
      raise reply
    key_pem, csr = ident.new_key_and_csr(name)
    return key_pem, ca.sign(csr), ca.pem
  monkeypatch.setattr(app.ident, "enroll", enroll)
  return made


def compose_token(server, tmp_path, ca, token="tok"):
  """ What the mPCC in the same compose stack leaves in /etc/mprov. """
  (tmp_path / "etc" / "enroll_token").write_text(token + "\n")
  (tmp_path / "etc" / "ca.crt").write_text(ca.pem)


# a certificate it already has

def test_a_job_server_with_a_certificate_uses_it(server, ca):
  ident.Identity(server.tlsDir).save(*credential(ca))
  server.session.headers["Authorization"] = "Api-Key old"
  assert server.use_identity() is True
  assert server.auth == "cert"
  assert server.session.cert == os.path.join(server.tlsDir, "jobserver.pem")
  # the mPCC is checked against the CA it sent with the certificate
  assert server.session.verify == os.path.join(server.tlsDir, "ca.crt")
  assert "Authorization" not in server.session.headers


def test_a_configured_ca_bundle_is_kept(server, ca):
  ident.Identity(server.tlsDir).save(*credential(ca))
  server.caBundle, server._caBundleSet = "/etc/mprov/site-ca.pem", True
  server.session.verify = "/etc/mprov/site-ca.pem"
  server.use_identity()
  assert server.session.verify == "/etc/mprov/site-ca.pem"


def test_a_certificate_isnt_used_over_http(server, ca, caplog):
  ident.Identity(server.tlsDir).save(*credential(ca))
  server.mprovURL = "http://mpcc/"
  server.apikey = "k"
  assert server.use_identity() is True
  assert server.auth == "apikey"
  assert "only be used over HTTPS" in caplog.text


# enrolling

def test_a_job_server_in_compose_enrolls_with_the_token_and_ca_the_mpcc_left(server, tmp_path, ca, monkeypatch, caplog):
  made = enrolls_with(ca, monkeypatch)
  compose_token(server, tmp_path, ca)
  assert server.use_identity() is True
  assert made == [{"url": URL, "verify": str(tmp_path / "etc" / "ca.crt"), "token": "tok",
                   "host_id": "host-1", "name": "js01"}]
  assert server.auth == "cert"
  assert ident.Identity(server.tlsDir).usable() is not None
  assert "enrolled with the mPCC" in caplog.text
  # the token is the mPCC's file: left alone
  assert (tmp_path / "etc" / "enroll_token").read_text() == "tok\n"


def test_a_configured_token_and_fingerprint_enroll_outside_compose(server, ca, monkeypatch):
  made = enrolls_with(ca, monkeypatch)
  server.enrollToken = "env-token"
  server.caFingerprint = ident.fingerprint(ca.cert)
  monkeypatch.setattr(app.ident, "fetch_ca", lambda url, fp: ca.pem if fp == server.caFingerprint else None)
  assert server.use_identity() is True
  assert made[0]["token"] == "env-token"
  assert made[0]["verify"] == os.path.join(server.tlsDir, "ca.crt")


def test_a_ca_bundle_is_what_the_mpcc_is_checked_against(server, ca, monkeypatch):
  made = enrolls_with(ca, monkeypatch)
  server.enrollToken = "tok"
  server.caBundle, server._caBundleSet = "/etc/mprov/site-ca.pem", True
  server.use_identity()
  assert made[0]["verify"] == "/etc/mprov/site-ca.pem"


def test_the_token_isnt_sent_to_an_mpcc_that_cant_be_checked(server, ca, monkeypatch, caplog):
  made = enrolls_with(ca, monkeypatch)
  server.enrollToken = "tok"
  assert server.use_identity() is False
  assert made == []
  assert "MPROV_CA_FINGERPRINT" in caplog.text
  # and it's tried again once there's a way to check, without a restart
  with open(server.beside_config("ca.crt"), "w") as f:
    f.write(ca.pem)
  assert server.use_identity() is True
  assert len(made) == 1


def test_the_token_isnt_sent_over_http(server, tmp_path, ca, monkeypatch, caplog):
  made = enrolls_with(ca, monkeypatch)
  compose_token(server, tmp_path, ca)
  server.mprovURL = "http://mpcc/"
  assert server.use_identity() is False
  assert made == []
  assert "enrolling needs HTTPS" in caplog.text


def test_a_refused_token_isnt_tried_again(server, tmp_path, ca, monkeypatch, caplog):
  made = enrolls_with(ca, monkeypatch, reply=ident.EnrollError("the mPCC refused the enrollment token"))
  compose_token(server, tmp_path, ca)
  for _ in range(3):
    assert server.use_identity() is False
  assert len(made) == 1
  assert caplog.text.count("refused the enrollment token") == 1
  # a new token is
  (tmp_path / "etc" / "enroll_token").write_text("new\n")
  server.use_identity()
  assert len(made) == 2


def test_an_mpcc_that_cant_sign_yet_is_tried_again(server, tmp_path, ca, monkeypatch, caplog):
  made = enrolls_with(ca, monkeypatch, reply=ident.EnrollError("503", retry=True))
  compose_token(server, tmp_path, ca)
  server.use_identity()
  server.use_identity()
  assert len(made) == 2
  assert caplog.text.count("unable to enroll") == 1


def test_no_host_id_no_enrollment(server, tmp_path, ca, monkeypatch, caplog):
  made = enrolls_with(ca, monkeypatch)
  compose_token(server, tmp_path, ca)
  monkeypatch.setattr(JobServer, "host_id", lambda self: None)
  assert server.use_identity() is False
  assert made == []
  assert "without a host id" in caplog.text


def test_a_script_runner_never_enrolls(server, tmp_path, ca, monkeypatch):
  made = enrolls_with(ca, monkeypatch)
  compose_token(server, tmp_path, ca)
  server.register = False
  assert server.use_identity() is True
  assert made == []
  # as before #381, whatever key it has, even none (the nads image)
  assert server.session.headers["Authorization"] == "Api-Key "


def test_a_script_runner_uses_a_certificate_that_is_there(server, ca):
  ident.Identity(server.tlsDir).save(*credential(ca))
  server.register = False
  server.use_identity()
  assert server.auth == "cert"


# the shared API key

def test_with_no_certificate_or_token_the_api_key_is_used_with_a_warning(server, caplog):
  server.apikey = "k"
  for _ in range(3):
    assert server.use_identity() is True
  assert server.session.headers["Authorization"] == "Api-Key k"
  assert server.session.cert is None
  assert caplog.text.count("shared API key, which is deprecated") == 1


def test_with_nothing_to_authenticate_with_it_says_so_once(server, caplog):
  for _ in range(3):
    assert server.use_identity() is False
  assert caplog.text.count("no certificate and nothing to enroll with") == 1


def test_start_session_waits_for_something_to_authenticate_with(server):
  assert server.startSession() is False
  assert server.sessionOk is False
  assert server.session.requests == []


def test_start_session_with_a_certificate(server, ca):
  ident.Identity(server.tlsDir).save(*credential(ca))
  server.myaddress = "10.0.0.3"
  server.session.routes[""] = {}
  assert server.startSession() is True
  assert server.sessionOk is True
  assert server.auth_refused in server.session.hooks["response"]


# a refused certificate

def refused(headers=None):
  response = FakeResponse(401, {"detail": "The client certificate is unknown, revoked or expired."})
  response.headers = headers if headers is not None else {"WWW-Authenticate": 'TLS-Client-Certificate realm="mprov"'}
  return response


def test_a_refused_certificate_is_dropped_and_a_new_token_used(server, tmp_path, ca, monkeypatch, caplog):
  ident.Identity(server.tlsDir).save(*credential(ca))
  server.use_identity()
  server.sessionOk = True
  for _ in range(2):
    server.auth_refused(refused())
  assert server.sessionOk is False
  assert server.auth is None
  assert caplog.text.count("refused this job server's certificate") == 1
  # with nothing to enroll with, it waits
  assert server.use_identity() is False
  # once the mPCC leaves a new token, it enrolls again
  made = enrolls_with(ca, monkeypatch)
  compose_token(server, tmp_path, ca)
  assert server.use_identity() is True
  assert len(made) == 1
  assert server.auth == "cert"


def test_a_refused_certificate_falls_back_to_the_api_key(server, ca):
  ident.Identity(server.tlsDir).save(*credential(ca))
  server.apikey = "k"
  server.use_identity()
  server.auth_refused(refused())
  server.use_identity()
  assert server.auth == "apikey"


@pytest.mark.parametrize("response", [refused(headers={}), FakeResponse(403, {}), FakeResponse(200, {})])
def test_other_answers_dont_drop_the_certificate(server, ca, response):
  if not hasattr(response, "headers"):
    response.headers = {}
  ident.Identity(server.tlsDir).save(*credential(ca))
  server.use_identity()
  server.sessionOk = True
  server.auth_refused(response)
  assert server.auth == "cert"
  assert server.sessionOk is True


def shared_key_refused():
  response = FakeResponse(403, {"detail": "The shared API key isn't accepted here (MPROV_SHARED_KEY_MODE=off)."})
  response.headers = {}
  return response


def test_a_refused_shared_key_tries_to_enroll_at_the_next_heartbeat(server, tmp_path, ca, monkeypatch, caplog):
  made = enrolls_with(ca, monkeypatch)
  server.apikey = "k"
  server.use_identity()
  server.sessionOk = True
  server._nextRenewCheck = app.time.monotonic() + 3000
  compose_token(server, tmp_path, ca)
  for _ in range(2):
    server.auth_refused(shared_key_refused())
  assert caplog.text.count("no longer accepts the shared API key") == 1
  # the session isn't dropped, so it doesn't retry in a tight loop
  assert server.sessionOk is True
  server.maybe_renew()
  assert len(made) == 1
  assert server.auth == "cert"


def test_another_403_on_the_api_key_changes_nothing(server, monkeypatch):
  server.apikey = "k"
  server.use_identity()
  server._nextRenewCheck = later = app.time.monotonic() + 3000
  response = FakeResponse(403, {"detail": "You do not have permission to perform this action."})
  response.headers = {}
  server.auth_refused(response)
  assert server._nextRenewCheck == later


# registering and the per-address sessions

def test_a_certificate_always_registers_with_the_host_id(server, ca):
  ident.Identity(server.tlsDir).save(*credential(ca))
  server.use_identity()
  server.ip_address = "10.0.0.2"
  server.jobmodules = ["libgenders"]
  server.config_data = {"global": {}}
  server.loadmon = False
  server.session.write_routes[("POST", "jobservers/")] = (200, {"pk": 5})
  server.register_server()
  sent = [data for method, path, data in server.session.writes if path == "jobservers/"]
  assert sent[0]["host_id"] == "host-1"
  assert sent[0]["name"] == "js01"


def test_an_address_session_sends_the_certificate(server, ca):
  ident.Identity(server.tlsDir).save(*credential(ca))
  server.session = app.requests.Session()
  server.use_identity()
  session = server.address_session("10.20.0.5")
  assert session.cert == os.path.join(server.tlsDir, "jobserver.pem")
  assert session.verify == os.path.join(server.tlsDir, "ca.crt")
  assert server.auth_refused in session.hooks["response"]


def test_a_new_certificate_rebuilds_the_address_sessions(server, ca):
  ident.Identity(server.tlsDir).save(*credential(ca))
  server.session = app.requests.Session()
  server.use_identity()
  first = server.address_session("10.20.0.5")
  ident.Identity(server.tlsDir).save(*credential(ca))
  server.use_identity()
  assert server.address_session("10.20.0.5") is not first


# renewing

def due_credential(ca, server):
  """ A certificate with 20 days left. """
  ident.Identity(server.tlsDir).save(*credential(ca, days=90, start=NOW - datetime.timedelta(days=70)))
  server.use_identity()


def test_a_certificate_in_its_window_is_renewed_with_a_new_key(server, ca, monkeypatch, caplog):
  due_credential(ca, server)
  old = (ident.Identity(server.tlsDir).usable(), open(os.path.join(server.tlsDir, "jobserver.pem")).read())
  seen = []

  def renew(session, url, name):
    seen.append((session, url, name))
    key_pem, csr = ident.new_key_and_csr(name)
    return key_pem, ca.sign(csr), ca.pem
  monkeypatch.setattr(app.ident, "renew", renew)
  server.maybe_renew()
  assert seen == [(server.session, URL, "js01")]
  new = ident.Identity(server.tlsDir).usable()
  assert new.not_valid_after_utc > old[0].not_valid_after_utc
  assert open(os.path.join(server.tlsDir, "jobserver.pem")).read().split("-----BEGIN CERTIFICATE")[0] \
    != old[1].split("-----BEGIN CERTIFICATE")[0]
  assert server._certFingerprint == ident.fingerprint(new)
  assert "renewed this job server's certificate" in caplog.text


def test_renewal_is_checked_at_most_once_an_hour(server, ca, monkeypatch):
  due_credential(ca, server)
  calls = []
  monkeypatch.setattr(app.ident, "renew", lambda *a: calls.append(a))
  server.maybe_renew()
  server.maybe_renew()
  assert len(calls) == 1


def test_a_certificate_not_in_its_window_isnt_renewed(server, ca, monkeypatch):
  ident.Identity(server.tlsDir).save(*credential(ca))
  server.use_identity()
  monkeypatch.setattr(app.ident, "renew", lambda *a: pytest.fail("renewed too early"))
  server.maybe_renew()


def test_the_api_key_isnt_renewed(server, monkeypatch):
  server.apikey = "k"
  server.use_identity()
  monkeypatch.setattr(app.ident, "renew", lambda *a: pytest.fail("renewed an API key"))
  server.maybe_renew()


def test_a_job_server_on_the_api_key_enrolls_once_it_can(server, tmp_path, ca, monkeypatch):
  """ e.g. the mPCC's ca.crt wasn't there yet when it started. """
  made = enrolls_with(ca, monkeypatch)
  server.apikey = "k"
  (tmp_path / "etc" / "enroll_token").write_text("tok\n")
  server.use_identity()
  assert server.auth == "apikey"
  assert made == []
  (tmp_path / "etc" / "ca.crt").write_text(ca.pem)
  server.maybe_renew()
  assert len(made) == 1
  assert server.auth == "cert"
  assert "Authorization" not in server.session.headers


def test_a_script_runner_on_the_api_key_never_enrolls(server, tmp_path, ca, monkeypatch):
  made = enrolls_with(ca, monkeypatch)
  compose_token(server, tmp_path, ca)
  server.register = False
  server.use_identity()
  server.maybe_renew()
  assert made == []


def test_a_failed_renewal_keeps_the_certificate_and_warns(server, ca, monkeypatch, caplog):
  due_credential(ca, server)
  before = open(os.path.join(server.tlsDir, "jobserver.pem")).read()

  def renew(*args):
    raise ident.EnrollError("renewal failed, the mPCC answered 503", retry=True)
  monkeypatch.setattr(app.ident, "renew", renew)
  server.maybe_renew()
  assert open(os.path.join(server.tlsDir, "jobserver.pem")).read() == before
  assert server.auth == "cert"
  assert "unable to renew" in caplog.text


def test_not_due_yet_by_the_mpccs_clock_changes_nothing(server, ca, monkeypatch):
  due_credential(ca, server)
  before = open(os.path.join(server.tlsDir, "jobserver.pem")).read()
  monkeypatch.setattr(app.ident, "renew", lambda *a: None)
  server.maybe_renew()
  assert open(os.path.join(server.tlsDir, "jobserver.pem")).read() == before


# the settings

def load(tmp_path, global_cfg, monkeypatch, env=None):
  for name in ("MPROV_TLS_DIR", "MPROV_ENROLL_TOKEN", "MPROV_CA_FINGERPRINT"):
    monkeypatch.delenv(name, raising=False)
  for name, value in (env or {}).items():
    monkeypatch.setenv(name, value)
  path = tmp_path / "jobserver.yaml"
  path.write_text(json.dumps([{"global": {"mprovURL": URL, "jobmodules": ["libgenders"], **global_cfg}}]))
  server = JobServer.__new__(JobServer)
  server.configfile = str(path)
  server.load_config()
  return server


def test_the_defaults(tmp_path, monkeypatch):
  server = load(tmp_path, {}, monkeypatch)
  assert server.tlsDir == "/var/lib/mprov/tls"
  assert server.enrollToken == ""
  assert server.enrollTokenFile == str(tmp_path / "enroll_token")
  assert server.caFingerprint == ""


def test_the_environment_sets_them(tmp_path, monkeypatch):
  server = load(tmp_path, {}, monkeypatch, env={"MPROV_TLS_DIR": "/srv/tls", "MPROV_ENROLL_TOKEN": "t",
                                                 "MPROV_CA_FINGERPRINT": "ab:cd"})
  assert (server.tlsDir, server.enrollToken, server.caFingerprint) == ("/srv/tls", "t", "ab:cd")


def test_the_config_wins_over_the_environment(tmp_path, monkeypatch):
  server = load(tmp_path, {"tlsDir": "/etc/js-tls", "enrollToken": "c", "caFingerprint": "ef",
                           "enrollTokenFile": "/run/token"},
                monkeypatch, env={"MPROV_TLS_DIR": "/srv/tls", "MPROV_ENROLL_TOKEN": "t",
                                  "MPROV_CA_FINGERPRINT": "ab"})
  assert (server.tlsDir, server.enrollToken, server.caFingerprint, server.enrollTokenFile) == \
    ("/etc/js-tls", "c", "ef", "/run/token")
