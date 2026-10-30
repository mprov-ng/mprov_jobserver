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
