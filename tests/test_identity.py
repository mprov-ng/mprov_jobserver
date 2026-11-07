"""
The job server's own key and certificate (mprov_control_center#381):
making the key and CSR, keeping them, enrolling with a one-time token,
renewing, and fetching the mPCC's CA by its fingerprint.  A real CA made
here signs the certificates, the way the mPCC's does, so the checks run on
real certificates.
"""
import datetime
import os
import stat

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from mprov_jobserver import identity as ident

URL = "https://mpcc/"
NOW = datetime.datetime.now(datetime.timezone.utc)


class FakeCA:
  """ A CA that signs CSRs the way the mPCC's does. """

  def __init__(self):
    self.key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test mPCC CA")])
    self.cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                 .public_key(self.key.public_key()).serial_number(x509.random_serial_number())
                 .not_valid_before(NOW - datetime.timedelta(days=1))
                 .not_valid_after(NOW + datetime.timedelta(days=3650))
                 .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                 .sign(self.key, hashes.SHA256()))
    self.pem = self.cert.public_bytes(serialization.Encoding.PEM).decode()

  def sign(self, csr_pem, days=90, start=None):
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    start = start or NOW - datetime.timedelta(minutes=5)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "js:test")]))
            .issuer_name(self.cert.subject).public_key(csr.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(start).not_valid_after(start + datetime.timedelta(days=days))
            .sign(self.key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


@pytest.fixture(scope="module")
def ca():
  return FakeCA()


def credential(ca, days=90, start=None):
  """ (key PEM, certificate PEM, CA PEM) as an enrollment gives them. """
  key_pem, csr = ident.new_key_and_csr("js01")
  return key_pem, ca.sign(csr, days=days, start=start), ca.pem


class Reply:
  def __init__(self, status_code, body=None, content=None, headers=None):
    self.status_code = status_code
    self._body = body
    self.content = content if content is not None else b""
    self.text = content.decode() if content is not None else ("" if body is None else str(body))
    self.headers = headers or {}

  def json(self):
    if self._body is None:
      raise ValueError("no JSON")
    return self._body


# the key and CSR

def test_the_csr_is_for_a_new_p256_key_and_signed_with_it():
  key_pem, csr_pem = ident.new_key_and_csr("js01")
  key = serialization.load_pem_private_key(key_pem, password=None)
  csr = x509.load_pem_x509_csr(csr_pem.encode())
  assert isinstance(key.curve, ec.SECP256R1)
  assert csr.is_signature_valid
  assert csr.public_key().public_numbers() == key.public_key().public_numbers()
  assert csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "js01"


def test_a_long_name_is_cut_to_what_a_cn_can_hold():
  _, csr_pem = ident.new_key_and_csr("n" * 255)
  csr = x509.load_pem_x509_csr(csr_pem.encode())
  assert len(csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value) == 64


def test_each_csr_gets_its_own_key():
  assert ident.new_key_and_csr("js01")[0] != ident.new_key_and_csr("js01")[0]


# keeping the credential

def test_a_saved_credential_loads_back_with_private_modes(tmp_path, ca):
  identity = ident.Identity(str(tmp_path / "tls"))
  identity.save(*credential(ca))
  cert = identity.usable()
  assert cert is not None
  assert stat.S_IMODE(os.stat(tmp_path / "tls").st_mode) == 0o700
  assert stat.S_IMODE(os.stat(identity.credential_path).st_mode) == 0o600
  assert stat.S_IMODE(os.stat(identity.ca_path).st_mode) == 0o644
  assert (tmp_path / "tls" / "ca.crt").read_text() == ca.pem
  # the key and the certificate are kept together, in one file
  data = (tmp_path / "tls" / "jobserver.pem").read_text()
  assert "PRIVATE KEY" in data and "CERTIFICATE" in data
  # and nothing else is left behind
  assert sorted(os.listdir(tmp_path / "tls")) == ["ca.crt", "jobserver.pem"]


def test_no_credential_is_none(tmp_path):
  assert ident.Identity(str(tmp_path)).certificate() is None


def test_a_certificate_with_another_key_is_none(tmp_path, ca, caplog):
  key_pem, cert_pem, _ = credential(ca)
  other_key, _ = ident.new_key_and_csr("js01")
  (tmp_path / "jobserver.pem").write_bytes(other_key + cert_pem.encode())
  assert ident.Identity(str(tmp_path)).certificate() is None
  assert "isn't the certificate's" in caplog.text


def test_a_file_that_isnt_a_credential_is_none(tmp_path, caplog):
  (tmp_path / "jobserver.pem").write_text("nonsense")
  assert ident.Identity(str(tmp_path)).certificate() is None
  assert "doesn't hold a certificate and its key" in caplog.text


def test_an_expired_certificate_isnt_usable(tmp_path, ca, caplog):
  identity = ident.Identity(str(tmp_path))
  identity.save(*credential(ca, days=90, start=NOW - datetime.timedelta(days=91)))
  assert identity.certificate() is not None
  assert identity.usable() is None
  assert "expired" in caplog.text


def test_a_certificate_is_due_in_the_last_29_days(tmp_path, ca):
  identity = ident.Identity(str(tmp_path))
  identity.save(*credential(ca))
  cert = identity.usable()
  assert not identity.due(cert)
  assert not identity.due(cert, now=cert.not_valid_after_utc - datetime.timedelta(days=30))
  assert identity.due(cert, now=cert.not_valid_after_utc - datetime.timedelta(days=29))


def test_a_failed_save_leaves_the_old_credential(tmp_path, ca, monkeypatch):
  identity = ident.Identity(str(tmp_path))
  identity.save(*credential(ca))
  before = (tmp_path / "jobserver.pem").read_bytes()

  def fail(*args):
    raise OSError("disk full")
  monkeypatch.setattr(ident.os, "replace", fail)
  with pytest.raises(OSError):
    identity.save(*credential(ca))
  assert (tmp_path / "jobserver.pem").read_bytes() == before
  assert sorted(os.listdir(tmp_path)) == ["ca.crt", "jobserver.pem"]


# fingerprints and the CA

def test_a_fingerprint_is_matched_without_colons_or_case(ca):
  fp = ident.fingerprint(ca.cert)
  shown = ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)).upper()
  assert ident.normalize_fingerprint(shown) == fp


def test_the_ca_is_taken_when_its_fingerprint_matches(ca, monkeypatch):
  seen = {}

  def get(url, **kwargs):
    seen.update(url=url, **kwargs)
    return Reply(200, content=ca.pem.encode())
  monkeypatch.setattr(ident.requests, "get", get)
  assert ident.fetch_ca(URL, ident.fingerprint(ca.cert).upper()) == ca.pem
  assert seen["url"] == URL + "enroll/ca.crt"
  assert seen["verify"] is False


def test_a_ca_with_another_fingerprint_is_refused(ca, monkeypatch):
  monkeypatch.setattr(ident.requests, "get", lambda url, **kw: Reply(200, content=ca.pem.encode()))
  with pytest.raises(ident.EnrollError) as err:
    ident.fetch_ca(URL, "00" * 32)
  assert "not the configured caFingerprint" in str(err.value)
  assert err.value.retry is False


@pytest.mark.parametrize("reply", [Reply(404, content=b"No CA yet.\n"), Reply(200, content=b"<html>")])
def test_a_ca_that_cant_be_fetched_is_tried_again(reply, monkeypatch):
  monkeypatch.setattr(ident.requests, "get", lambda url, **kw: reply)
  with pytest.raises(ident.EnrollError) as err:
    ident.fetch_ca(URL, "00" * 32)
  assert err.value.retry is True


# enrolling

def test_enrolling_sends_the_token_and_a_csr_and_returns_the_credential(ca, monkeypatch):
  seen = {}

  def post(url, json, **kwargs):
    seen.update(url=url, body=json, **kwargs)
    return Reply(200, {"certificate": ca.sign(json["csr"]), "ca": ca.pem, "kind": "jobserver"})
  monkeypatch.setattr(ident.requests, "post", post)
  key_pem, cert_pem, ca_pem = ident.enroll(URL, "/etc/mprov/ca.crt", "tok", "host-1", "js01")
  assert seen["url"] == URL + "enroll/jobserver/"
  assert seen["verify"] == "/etc/mprov/ca.crt"
  assert seen["body"]["token"] == "tok"
  assert seen["body"]["host_id"] == "host-1"
  assert seen["body"]["name"] == "js01"
  # the certificate is for the key that stays here
  cert = x509.load_pem_x509_certificate(cert_pem.encode())
  key = serialization.load_pem_private_key(key_pem, password=None)
  assert cert.public_key().public_numbers() == key.public_key().public_numbers()
  assert ca_pem == ca.pem
  assert b"PRIVATE KEY" not in str(seen["body"]).encode()


@pytest.mark.parametrize("status, body, says, retry", [
  (403, {"token": "Unknown, used or expired."}, "refused the enrollment token", False),
  (409, {"detail": "another job server has that name"}, "host id", False),
  (400, {"csr": "not a CSR."}, "400", False),
  (404, None, "is it 0.7.0 or later", False),
  (503, {"detail": "The mPCC can't sign right now; try again."}, "503", True),
])
def test_a_refused_enrollment_says_why(status, body, says, retry, monkeypatch):
  monkeypatch.setattr(ident.requests, "post", lambda url, json, **kw: Reply(status, body))
  with pytest.raises(ident.EnrollError) as err:
    ident.enroll(URL, True, "tok", "host-1", "js01")
  assert says in str(err.value)
  assert err.value.retry is retry


def test_a_server_that_fails_the_certificate_check_doesnt_get_the_token(monkeypatch):
  def post(url, json, **kwargs):
    raise requests.exceptions.SSLError("certificate verify failed")
  monkeypatch.setattr(ident.requests, "post", post)
  with pytest.raises(ident.EnrollError) as err:
    ident.enroll(URL, "/etc/mprov/ca.crt", "tok", "host-1", "js01")
  assert "the token wasn't sent" in str(err.value)
  assert err.value.retry is True


def test_an_unreachable_mpcc_is_tried_again(monkeypatch):
  def post(url, json, **kwargs):
    raise requests.exceptions.ConnectionError("refused")
  monkeypatch.setattr(ident.requests, "post", post)
  with pytest.raises(ident.EnrollError) as err:
    ident.enroll(URL, True, "tok", "host-1", "js01")
  assert err.value.retry is True


def test_an_invalid_reply_is_tried_again(monkeypatch):
  monkeypatch.setattr(ident.requests, "post", lambda url, json, **kw: Reply(200, {"nope": 1}))
  with pytest.raises(ident.EnrollError) as err:
    ident.enroll(URL, True, "tok", "host-1", "js01")
  assert err.value.retry is True


# renewing

class Session:
  def __init__(self, reply):
    self.reply = reply
    self.posts = []

  def post(self, url, json=None, **kwargs):
    self.posts.append((url, json))
    if isinstance(self.reply, Exception):
      raise self.reply
    return self.reply(json) if callable(self.reply) else self.reply


def test_renewing_sends_a_new_csr_on_the_session(ca):
  session = Session(lambda body: Reply(200, {"certificate": ca.sign(body["csr"]), "ca": ca.pem}))
  key_pem, cert_pem, ca_pem = ident.renew(session, URL, "js01")
  assert session.posts[0][0] == URL + "enroll/renew/"
  assert set(session.posts[0][1]) == {"csr"}
  assert x509.load_pem_x509_certificate(cert_pem.encode())


def test_renewing_too_early_is_none():
  assert ident.renew(Session(Reply(409, {"detail": "Not due: renew from 2027-01-01."})), URL, "js01") is None


@pytest.mark.parametrize("reply", [Reply(401, {"detail": "revoked"}), Reply(503, {"detail": "busy"}),
                                   requests.exceptions.ConnectionError("refused")])
def test_a_failed_renewal_raises(reply):
  with pytest.raises(ident.EnrollError):
    ident.renew(Session(reply), URL, "js01")


# the token

def test_a_configured_token_wins_over_the_file(tmp_path):
  (tmp_path / "enroll_token").write_text("from-file\n")
  assert ident.token_from(" set ", str(tmp_path / "enroll_token")) == "set"


def test_the_token_file_is_read(tmp_path):
  (tmp_path / "enroll_token").write_text("from-file\n")
  assert ident.token_from("", str(tmp_path / "enroll_token")) == "from-file"
  # and left as it was: it's the mPCC's
  assert (tmp_path / "enroll_token").read_text() == "from-file\n"


@pytest.mark.parametrize("content", [None, "", "\n"])
def test_no_token_is_none(tmp_path, content):
  path = tmp_path / "enroll_token"
  if content is not None:
    path.write_text(content)
  assert ident.token_from("", str(path)) is None
  assert ident.token_from(None, "") is None


def test_a_token_id_doesnt_give_the_token_away():
  assert "secret" not in ident.token_id("secret")
  assert ident.token_id("secret") == ident.token_id("secret") != ident.token_id("other")
