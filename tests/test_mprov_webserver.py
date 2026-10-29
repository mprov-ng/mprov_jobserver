"""
mprov_webserver serves the job server's image and repo directories over
HTTP.  It sends 404s for images while it's overloaded, so clients retry on
another job server.  Each test runs a real server on localhost.
"""
import http.client
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from mprov_jobserver.plugins import mprov_webserver as webserver_module
from mprov_jobserver.plugins.mprov_webserver import mProvHTTPServer, mProvHTTPReqestHandler, mprov_webserver


@pytest.fixture
def root(tmp_path):
  (tmp_path / "images" / "compute").mkdir(parents=True)
  (tmp_path / "images" / "compute" / "compute.img").write_bytes(b"IMAGE")
  (tmp_path / "osrepos" / "5").mkdir(parents=True)
  (tmp_path / "osrepos" / "5" / "repomd.xml").write_text("<repomd/>")
  return tmp_path


@pytest.fixture
def server(js, root):
  server = mProvHTTPServer(("127.0.0.1", 0), mProvHTTPReqestHandler)
  server.rootDir = str(root)
  server.js = js
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  yield server
  server.shutdown()
  server.server_close()


def url(server, path):
  return f"http://127.0.0.1:{server.server_address[1]}{path}"


def get(server, path, method="GET"):
  request = urllib.request.Request(url(server, path), method=method)
  with urllib.request.urlopen(request, timeout=5) as response:
    return response.status, response.read()


def test_serves_files_from_the_root_dir(server):
  assert get(server, "/images/compute/compute.img") == (200, b"IMAGE")
  assert get(server, "/osrepos/5/repomd.xml") == (200, b"<repomd/>")


def test_head(server):
  assert get(server, "/images/compute/compute.img", method="HEAD") == (200, b"")


def test_missing_file_is_404(server):
  with pytest.raises(urllib.error.HTTPError) as err:
    get(server, "/images/compute/nope.img")
  assert err.value.code == 404


def test_connection_count_goes_back_down(server):
  get(server, "/images/compute/compute.img")
  # the handler decrements the count after the response is sent, so the
  # client can get here first; give it a moment.
  for _ in range(100):
    if mProvHTTPReqestHandler.connCount == 0:
      break
    time.sleep(0.01)
  assert mProvHTTPReqestHandler.connCount == 0


def test_plugin_serves_until_the_job_server_stops(js, root):
  with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
  plugin = mprov_webserver(js)
  plugin.hostName, plugin.serverPort, plugin.rootDir = "127.0.0.1", port, str(root)
  thread = threading.Thread(target=plugin.handle_jobs, daemon=True)
  thread.start()
  for _ in range(50):
    try:
      with urllib.request.urlopen(f"http://127.0.0.1:{port}/images/compute/compute.img", timeout=1) as r:
        assert r.read() == b"IMAGE"
      break
    except urllib.error.URLError:
      thread.join(0.1)
  js.running = False
  thread.join(5)
  assert not thread.is_alive()


def test_defaults_to_serving_export():
  assert mprov_webserver.rootDir == "/export/"
  assert mProvHTTPServer.rootDir == "/export/"


@pytest.mark.parametrize("unsafe", ["", "/", "/no/such/dir"])
def test_unsafe_root_dir_serves_nothing(js, tmp_path, monkeypatch, unsafe):
  """
  An empty rootDir used to serve the working directory, which is / under
  systemd, so the API key in /etc/mprov/jobserver.yaml was one GET away
  (#214).  The server must close the connection instead.
  """
  (tmp_path / "secret").write_text("apikey")
  monkeypatch.chdir(tmp_path)
  server = mProvHTTPServer(("127.0.0.1", 0), mProvHTTPReqestHandler)
  server.rootDir = unsafe
  server.js = js
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  try:
    for path in ("/secret", "/etc/hostname"):
      with pytest.raises((urllib.error.URLError, ConnectionError, http.client.HTTPException)):
        get(server, path)
  finally:
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("unsafe", ["", "/", "/no/such/dir"])
def test_plugin_refuses_to_start_on_an_unsafe_root_dir(js, unsafe, capsys):
  with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
  plugin = mprov_webserver(js)
  plugin.hostName, plugin.serverPort, plugin.rootDir = "127.0.0.1", port, unsafe
  thread = threading.Thread(target=plugin.handle_jobs, daemon=True)
  thread.start()
  thread.join(5)
  assert not thread.is_alive()
  assert plugin.failed
  assert "not started" in capsys.readouterr().out
  # nothing is listening on the port
  with socket.socket() as s:
    assert s.connect_ex(("127.0.0.1", port)) != 0


@pytest.mark.xfail(strict=True, reason="checkFileSize() compares the filesystem path with /images/, so the connection limit never applies (#148)")
def test_busy_server_turns_image_requests_away(js, server, monkeypatch):
  monkeypatch.setattr(mProvHTTPReqestHandler, "connCount", mProvHTTPReqestHandler.maxConn)
  with pytest.raises(urllib.error.HTTPError) as err:
    get(server, "/images/compute/compute.img")
  assert err.value.code == 404
  assert js.register is False


@pytest.mark.xfail(strict=True, reason="under high load do_GET() reads self.js.config_data['loadmon']; the handler has no js (#147)")
def test_high_load_turns_image_requests_away(js, server, monkeypatch):
  monkeypatch.setattr(webserver_module.os, "getloadavg", lambda: (99.0, 99.0, 99.0))
  with pytest.raises(urllib.error.HTTPError) as err:
    get(server, "/images/compute/compute.img")
  assert err.value.code == 404
