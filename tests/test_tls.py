"""
HTTPS to an mPCC with a site or self-signed CA (#102).  PyPI's requests
trusts only certifi, not the OS store, and startSession() hid the reason;
now the job server and MProvScript verify against the global caBundle, or
the OS bundle, and say why a connection failed.
"""
import http.server
import os
import ssl
import sys
import threading

import pytest
import requests

import mprov_jobserver.app as app
import mprov_jobserver.script as script_module
from mprov_jobserver import tls
from mprov_jobserver.app import JobServer
from mprov_jobserver.script import MProvScript
from tools import run


# ca_bundle()

def test_a_configured_bundle_is_used(tmp_path):
  ca = tmp_path / "ca.pem"
  ca.write_text("x")
  assert tls.ca_bundle(str(ca)) == str(ca)


def test_a_configured_bundle_that_doesnt_exist_is_an_error(tmp_path):
  with pytest.raises(FileNotFoundError):
    tls.ca_bundle(str(tmp_path / "missing.pem"))


def test_unset_falls_back_to_the_os_bundle(tmp_path, monkeypatch):
  os_bundle = tmp_path / "ca-certificates.crt"
  os_bundle.write_text("x")
  monkeypatch.setattr(tls, "OS_CA_BUNDLES", (str(tmp_path / "none"), str(os_bundle)))
  assert tls.ca_bundle("") == str(os_bundle)
  assert tls.ca_bundle(None) == str(os_bundle)


def test_no_os_bundle_leaves_requests_default(tmp_path, monkeypatch):
  monkeypatch.setattr(tls, "OS_CA_BUNDLES", (str(tmp_path / "none"),))
  assert tls.ca_bundle("") is None


def test_the_job_server_refuses_a_missing_configured_bundle(tmp_path):
  server = JobServer.__new__(JobServer)
  server.session = requests.Session()
  server.caBundle = str(tmp_path / "missing.pem")
  with pytest.raises(SystemExit):
    server.use_ca_bundle()


# against a real HTTPS server with a self-signed certificate

@pytest.fixture(scope="module")
def mpcc(tmp_path_factory):
  d = tmp_path_factory.mktemp("tls")
  ok, out = run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(d / "key.pem"), "-out", str(d / "ca.pem"), "-days", "1",
                 "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"])
  assert ok, out

  class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
      self.send_response(200)
      self.send_header("Content-Length", "2")
      self.end_headers()
      self.wfile.write(b"ok")
    def log_message(self, *args):
      pass

  server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
  context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
  context.load_cert_chain(str(d / "ca.pem"), str(d / "key.pem"))
  server.socket = context.wrap_socket(server.socket, server_side=True)
  threading.Thread(target=server.serve_forever, daemon=True).start()
  yield f"https://localhost:{server.server_address[1]}/", str(d / "ca.pem")
  server.shutdown()


@pytest.fixture
def no_os_ca(tmp_path, monkeypatch):
  # the test machine's own OS bundle doesn't have the test CA anyway; make
  # sure nothing else does either
  monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
  monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
  monkeypatch.setattr(app.time, "sleep", lambda s: None)


def job_server(url, ca):
  server = JobServer.__new__(JobServer)
  server.session = requests.Session()
  server.mprovURL = url
  server.apikey = "k"
  server.myaddress = "10.0.0.2"
  server.caBundle = ca
  server.use_ca_bundle()
  return server


def test_the_job_server_connects_with_the_mpccs_ca(mpcc, no_os_ca):
  url, ca = mpcc
  server = job_server(url, ca)
  assert server.startSession() is True
  assert server.sessionOk


def test_the_job_server_says_why_it_cant_connect(mpcc, no_os_ca, capsys):
  url, _ = mpcc
  server = job_server(url, "")
  assert not server.startSession()
  err = capsys.readouterr().err
  assert f"unable to reach the mPCC at {url}" in err
  assert "CERTIFICATE_VERIFY_FAILED" in err
  assert "set global caBundle" in err


def mprov_script(url, ca):
  s = MProvScript.__new__(MProvScript)
  s.session = requests.Session()
  s.mprovURL = url
  s.apikey = "k"
  s.caBundle = ca
  return s


def test_mprov_script_connects_with_the_mpccs_ca(mpcc, no_os_ca):
  url, ca = mpcc
  s = mprov_script(url, ca)
  assert s.startSession() is True
  assert s.ip_address == "127.0.0.1"


def test_mprov_script_says_why_it_cant_connect(mpcc, no_os_ca, capsys, monkeypatch):
  monkeypatch.setattr(script_module.time, "sleep", lambda s: None)
  url, _ = mpcc
  assert not mprov_script(url, "").startSession()
  assert "CERTIFICATE_VERIFY_FAILED" in capsys.readouterr().err
