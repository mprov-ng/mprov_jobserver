"""
MProvScript, the helper class node scripts subclass, finds its own address
on the connection to the mPCC (#160).
"""
import socket

import pytest

from mprov_jobserver.script import MProvScript


class ConnectedResponse:
  """ A streamed requests response whose raw socket is a real connection. """
  status_code = 200

  def __init__(self, sock):
    self.raw = sock


class Session:
  def __init__(self, response):
    self.response = response
    self.headers = {}
    # as a requests.Session has them, for the version check (#287)
    self.hooks = {"response": []}

  def get(self, url, **kwargs):
    return self.response


@pytest.mark.parametrize("family, host", [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")])
def test_ip_address_is_the_address_string(family, host):
  try:
    server = socket.socket(family, socket.SOCK_STREAM)
    server.bind((host, 0))
  except OSError:
    pytest.skip(f"no {host} here")
  server.listen(1)
  client = socket.create_connection(server.getsockname()[:2])
  try:
    script = MProvScript.__new__(MProvScript)
    script.session = Session(ConnectedResponse(client))
    assert script.startSession()
    # the address, not the (address, port) tuple
    assert script.ip_address == host
    # the connection itself is still open
    assert client.fileno() != -1
  finally:
    client.close()
    server.close()


# a booted node's own certificate (mprov_control_center#381, 4b)

@pytest.fixture
def connected():
  server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  server.bind(("127.0.0.1", 0))
  server.listen(1)
  client = socket.create_connection(server.getsockname()[:2])
  yield ConnectedResponse(client)
  client.close()
  server.close()


def test_a_node_script_uses_the_nodes_certificate(tmp_path, connected):
  from test_identity import FakeCA, node_files
  node = tmp_path / "node"
  node.mkdir()
  script = MProvScript.__new__(MProvScript)
  script.session = Session(connected)
  script.nodeTlsDir = node_files(node, FakeCA())
  script.mprovURL = "http://10.0.0.1/"
  script.apikey = "baked-key"
  assert script.startSession()
  assert script.session.cert == (str(node / "node.crt"), str(node / "node.key"))
  assert script.session.verify == str(node / "ca.crt")
  assert "Authorization" not in script.session.headers
  assert script.mprovURL == "https://10.0.0.1/"


def test_a_second_session_still_uses_the_nodes_ca(tmp_path, connected):
  # scripts call startSession() again after __init__() has (#360)
  from test_identity import FakeCA, node_files
  node = tmp_path / "node"
  node.mkdir()
  script = MProvScript.__new__(MProvScript)
  script.session = Session(connected)
  script.nodeTlsDir = node_files(node, FakeCA())
  script.mprovURL = "http://10.0.0.1/"
  script.caBundle = ""
  assert script.startSession()
  assert script.startSession()
  assert script.session.verify == str(node / "ca.crt")
  assert script.caBundle == ""
  assert script.mprovURL == "https://10.0.0.1/"


def test_a_configured_ca_bundle_is_used_every_time(tmp_path, connected):
  from test_identity import FakeCA, node_files
  node = tmp_path / "node"
  node.mkdir()
  bundle = tmp_path / "mpcc-ca.crt"
  bundle.write_text("CA\n")
  script = MProvScript.__new__(MProvScript)
  script.session = Session(connected)
  script.nodeTlsDir = node_files(node, FakeCA())
  script.mprovURL = "http://10.0.0.1/"
  script.caBundle = str(bundle)
  assert script.startSession()
  assert script.startSession()
  assert script.session.verify == str(bundle)


def test_a_node_script_without_a_certificate_uses_the_key(tmp_path, connected):
  script = MProvScript.__new__(MProvScript)
  script.session = Session(connected)
  script.nodeTlsDir = str(tmp_path / "none")
  script.mprovURL = "http://10.0.0.1/"
  script.apikey = "baked-key"
  assert script.startSession()
  assert script.session.headers["Authorization"] == "Api-Key baked-key"
  assert script.mprovURL == "http://10.0.0.1/"


def test_a_node_script_with_no_certificate_or_key_sends_no_authorization(tmp_path, connected):
  """ The nads image, or a node whose enrollment failed. """
  script = MProvScript.__new__(MProvScript)
  script.session = Session(connected)
  script.session.headers["Authorization"] = "Api-Key old"
  script.nodeTlsDir = str(tmp_path / "none")
  script.mprovURL = "http://10.0.0.1/"
  script.apikey = ""
  assert script.startSession()
  assert "Authorization" not in script.session.headers
