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


def raw_status(server, path, method="GET"):
  # http.client sends the path as given; urllib would tidy //images/ up.
  conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
  try:
    conn.request(method, path)
    response = conn.getresponse()
    response.read()
    return response.status
  finally:
    conn.close()


@pytest.fixture
def busy(js, monkeypatch):
  """ A server already at its connection limit, and registered with the mPCC. """
  js.register = True
  monkeypatch.setattr(mProvHTTPReqestHandler, "connCount", mProvHTTPReqestHandler.maxConn)
  yield
  # being full never stops the heartbeat: the mPCC would delete this job
  # server after a minute, with its images' "Hosted By" (#148)
  assert js.register is True


def test_busy_server_turns_image_requests_away(js, server, busy):
  # checked the filesystem path (<rootDir>/images/...) for /images/, so the
  # limit never applied (#148)
  with pytest.raises(urllib.error.HTTPError) as err:
    get(server, "/images/compute/compute.img")
  assert err.value.code == 404


@pytest.mark.parametrize("path", ["//images/compute/compute.img", "/osrepos/../images/compute/compute.img"])
def test_busy_server_limits_image_paths_however_they_are_spelled(js, server, busy, path):
  assert raw_status(server, path) == 404


def test_busy_server_refuses_image_heads(js, server, busy):
  assert raw_status(server, "/images/compute/compute.img", method="HEAD") == 404


def test_busy_server_serves_images_under_the_size_limit(js, server, busy):
  # compared with the handler's maxConnFileSize (always 0), not the server's (#148)
  server.maxConnFileSize = 1000
  assert get(server, "/images/compute/compute.img") == (200, b"IMAGE")
  assert js.register is True


@pytest.mark.parametrize("path", ["/osrepos/5/repomd.xml", "/images/compute/", "/images/compute/nope.img"])
def test_busy_server_leaves_everything_else_alone(js, server, busy, path):
  # only image files count toward and are refused by the limit
  try:
    status = get(server, path)[0]
  except urllib.error.HTTPError as err:
    status = err.code
  assert status == (404 if path.endswith("nope.img") else 200)
  assert js.register is True
  assert mProvHTTPReqestHandler.connCount == mProvHTTPReqestHandler.maxConn


def test_heads_never_take_a_slot(server, monkeypatch):
  # the mPCC HEADs a job server before each redirect; that mustn't use up
  # the limit
  seen = []
  head = webserver_module.SimpleHTTPRequestHandler.do_HEAD
  def spy(self):
    seen.append(mProvHTTPReqestHandler.connCount)
    return head(self)
  monkeypatch.setattr(webserver_module.SimpleHTTPRequestHandler, "do_HEAD", spy)
  monkeypatch.setattr(mProvHTTPReqestHandler, "connCount", mProvHTTPReqestHandler.maxConn - 1)
  for _ in range(3):
    assert get(server, "/images/compute/compute.img", method="HEAD") == (200, b"")
  assert seen == [mProvHTTPReqestHandler.maxConn - 1] * 3
  assert mProvHTTPReqestHandler.connCount == mProvHTTPReqestHandler.maxConn - 1


def test_refused_download_takes_no_slot(server, busy):
  for _ in range(3):
    with pytest.raises(urllib.error.HTTPError):
      get(server, "/images/compute/compute.img")
  assert mProvHTTPReqestHandler.connCount == mProvHTTPReqestHandler.maxConn


def test_failed_download_gives_its_slot_back(server, monkeypatch):
  # a client that hangs up mid-download raises out of the handler
  def hang_up(self):
    raise ConnectionResetError("client went away")
  monkeypatch.setattr(webserver_module.SimpleHTTPRequestHandler, "do_GET", hang_up)
  with pytest.raises((urllib.error.URLError, ConnectionError, http.client.HTTPException)):
    get(server, "/images/compute/compute.img")
  for _ in range(100):
    if mProvHTTPReqestHandler.connCount == 0:
      break
    time.sleep(0.01)
  assert mProvHTTPReqestHandler.connCount == 0


def test_only_image_downloads_count_toward_the_limit(server, monkeypatch):
  # every GET counted, so busy repo mirrors would turn images away (#148)
  seen = []
  serve = webserver_module.SimpleHTTPRequestHandler.do_GET
  def spy(self):
    seen.append((self.path, mProvHTTPReqestHandler.connCount))
    return serve(self)
  monkeypatch.setattr(webserver_module.SimpleHTTPRequestHandler, "do_GET", spy)
  get(server, "/osrepos/5/repomd.xml")
  get(server, "/images/compute/compute.img")
  assert seen == [("/osrepos/5/repomd.xml", 0), ("/images/compute/compute.img", 1)]


def test_high_load_turns_image_requests_away(js, server, monkeypatch):
  monkeypatch.setattr(webserver_module.os, "getloadavg", lambda: (99.0, 99.0, 99.0))
  with pytest.raises(urllib.error.HTTPError) as err:
    get(server, "/images/compute/compute.img")
  assert err.value.code == 404
  # however the path is spelled (#148); http.server already folds a
  # leading // into /, so it's the .. spelling that got past the raw check
  assert raw_status(server, "/osrepos/../images/compute/compute.img") == 404
  assert get(server, "/osrepos/5/repomd.xml") == (200, b"<repomd/>")


def test_high_load_serves_images_when_loadmon_is_off(js, server, monkeypatch):
  monkeypatch.setattr(webserver_module.os, "getloadavg", lambda: (99.0, 99.0, 99.0))
  js.loadmon = False
  assert get(server, "/images/compute/compute.img") == (200, b"IMAGE")
