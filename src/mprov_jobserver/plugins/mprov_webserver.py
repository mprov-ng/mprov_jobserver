import os
from .plugin import JobServerPlugin
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from http import HTTPStatus
import multiprocessing, socket, stat, threading, time
from mprov_jobserver.log import get_logger

log = get_logger(__name__)
# node boots as the job server sees them, for a time-to-provision metric
# (#105): "[I] provisioning: c01 (10.0.0.21) requested image compute"
plog = get_logger('mprov_jobserver.provisioning')

# reverse names of clients, as (name, when looked up); a node's name comes
# from the job server's own dnsmasq, so a lookup is quick, but not every
# request needs one
NAME_TTL = 300
_names = {}
_namesLock = threading.Lock()


def client_ip(address):
  # the server listens on ::, so IPv4 clients come in as ::ffff:a.b.c.d
  ip = address[0]
  return ip[len('::ffff:'):] if ip.startswith('::ffff:') else ip


def client_name(ip):
  """ The client's short host name from a reverse lookup, or None. """
  now = time.monotonic()
  with _namesLock:
    cached = _names.get(ip)
  if cached is not None and now - cached[1] < NAME_TTL:
    return cached[0]
  try:
    name = socket.gethostbyaddr(ip)[0].split('.')[0] or None
  except (OSError, UnicodeError):
    name = None
  with _namesLock:
    _names[ip] = (name, now)
  return name


def size_text(size):
  for unit in ('B', 'KB', 'MB', 'GB'):
    if size < 1024 or unit == 'GB':
      return f"{size:.1f} {unit}" if unit != 'B' else f"{size} B"
    size /= 1024

def safe_root(rootDir):
  # The directory to serve, or None if serving it would expose the whole
  # filesystem: an empty rootDir means the working directory, which is /
  # under systemd (#214).
  if not rootDir:
    return None
  real = os.path.realpath(rootDir)
  if real == "/" or not os.path.isdir(real):
    return None
  return real

class mProvHTTPReqestHandler(SimpleHTTPRequestHandler):
  # TODO: make maxConn configurable through the config yaml.
  # connCount counts the image downloads the connection limit applies to
  # (files of the server's maxConnFileSize or bigger), across the server's
  # request threads, so it's only changed under connLock (#148).
  connCount=0
  maxConn=10
  connLock=threading.Lock()

  def isImageRequest(self):
    # judge the path we'd serve, not the raw URL, so //images/ and
    # /x/../images/ are images too (#148).
    rel = os.path.relpath(self.translate_path(self.path), self.directory)
    return rel == "images" or rel.startswith("images" + os.sep)

  def isLimitedImage(self):
    # an image file big enough for the connection limit; stat it without
    # holding a descriptor (#185).  Anything else, missing files included,
    # is left to SimpleHTTPRequestHandler.
    if not self.isImageRequest():
      return False
    try:
      st = os.stat(self.translate_path(self.path))
    except OSError:
      return False
    return stat.S_ISREG(st.st_mode) and st.st_size >= self.server.maxConnFileSize

  def bootFile(self):
    """
    (image slug, kind) for a node's boot file under images/<slug>/:
    "kernel" for <slug>.vmlinuz, "image" for <slug>.img; None otherwise.
    """
    if not self.isImageRequest():
      return None
    rel = os.path.relpath(self.translate_path(self.path), self.directory)
    parts = rel.split(os.sep)
    if len(parts) != 3:
      return None
    slug, name = parts[1], parts[2]
    if name == slug + '.vmlinuz':
      return slug, 'kernel'
    if name == slug + '.img':
      return slug, 'image'
    return None

  def node(self):
    ip = client_ip(self.client_address)
    name = client_name(ip)
    return f"{name} ({ip})" if name else ip

  def send_response(self, code, message=None):
    # remember the status, to tell a served image from a refused one
    self.sentCode = code
    super().send_response(code, message)

  def refuseBusy(self):
    # 404 will result in a retry from the mPCC/client hopefully to another
    # server: the mPCC HEADs each job server before redirecting a client,
    # and skips one that refuses.  Keep heartbeating while full, or the
    # mPCC deletes this job server after a minute: it drops out of its
    # images' "Hosted By", and its jobs lose their job server (#148).
    self.send_error(HTTPStatus.NOT_FOUND, "File not found, max connections reached")

  def do_GET(self):
    boot = self.bootFile()
    # the job server is the server's, not the handler's (#147)
    if os.getloadavg()[1] >= multiprocessing.cpu_count() \
        and self.server.js is not None and self.server.js.loadmon \
        and self.isImageRequest():
      log.warning("Not Serving, high load.")
      if boot:
        plog.warning(f"{self.node()} turned away from image {boot[0]}: high load")
      self.send_error(HTTPStatus.NOT_FOUND, "Unable to serve, High load")
      return False

    if not self.isLimitedImage():
      return self.serveBootFile(boot)

    with mProvHTTPReqestHandler.connLock:
      busy = mProvHTTPReqestHandler.connCount >= mProvHTTPReqestHandler.maxConn
      if not busy:
        mProvHTTPReqestHandler.connCount += 1
    if busy:
      if boot:
        plog.warning(f"{self.node()} turned away from image {boot[0]}: {mProvHTTPReqestHandler.maxConn} downloads already running")
      self.refuseBusy()
      return None
    # we are ok to serve; give the slot back however the download ends.
    try:
      return self.serveBootFile(boot)
    finally:
      with mProvHTTPReqestHandler.connLock:
        mProvHTTPReqestHandler.connCount -= 1

  def serveBootFile(self, boot):
    """
    Serve the request; for a node's kernel or image, log the provisioning
    steps: the kernel request starts a boot, the image download is timed
    (#105).
    """
    if boot is None:
      return super().do_GET()
    slug, kind = boot
    node = self.node()
    if kind == 'kernel':
      super().do_GET()
      if getattr(self, 'sentCode', None) == HTTPStatus.OK:
        plog.info(f"{node} requested image {slug}")
      return None
    plog.info(f"{node} downloading image {slug}")
    start = time.monotonic()
    try:
      super().do_GET()
    except OSError as err:
      # the node went away mid-download (a reset, a reboot)
      plog.warning(f"{node} image {slug} download failed after {time.monotonic() - start:.0f} s ({err.strerror or err})")
      self.close_connection = True
      return None
    took = time.monotonic() - start
    if getattr(self, 'sentCode', None) != HTTPStatus.OK:
      plog.warning(f"{node} image {slug} not served (HTTP {getattr(self, 'sentCode', '?')})")
      return None
    try:
      size = size_text(os.stat(self.translate_path(self.path)).st_size)
    except OSError:
      size = "?"
    plog.info(f"{node} got image {slug} ({size} in {took:.0f} s)")
    return None

  def do_HEAD(self):
    # a HEAD never takes a slot: the mPCC probes with one per redirect.
    if self.isLimitedImage() and mProvHTTPReqestHandler.connCount >= mProvHTTPReqestHandler.maxConn:
      self.refuseBusy()
      return None
    return super().do_HEAD()

class mProvHTTPServer(ThreadingHTTPServer):
  rootDir = "/export/"
  maxConnFileSize = 0
  js = None
  def __init__(self, server_address, RequestHandlerClass, bind_and_activate=True):
    if ":" in server_address[0]:
      # we have an IPv6 bind address
      self.address_family = socket.AF_INET6
    super().__init__(server_address, RequestHandlerClass, bind_and_activate)
    # self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)

  def finish_request(self, request, client_address):
    # hand the handler our root, so it never falls back to the working
    # directory; refuse the request if the root isn't safe to serve (#214).
    root = safe_root(self.rootDir)
    if root is None:
      return
    self.RequestHandlerClass(request, client_address, self, directory=root)

class mprov_webserver(JobServerPlugin):
  jobModule = 'mprov-webserver'
  hostName = "::"
  serverPort = 8080
  serverInstance = None
  rootDir = "/export/"
  maxConnFileSize = 0

  def handle_jobs(self):
    if safe_root(self.rootDir) is None:
      log.error(f"mProv Webserver not started: rootDir '{self.rootDir}' is empty, /, or not a directory.  Set rootDir in mprov-webserver.yaml.")
      self.failed = True
      return
    log.info(f"Starting mProv Webserver on port {self.serverPort} serving {self.rootDir} ...")

    serverInstance = mProvHTTPServer((self.hostName, self.serverPort), mProvHTTPReqestHandler)
    serverInstance.rootDir = self.rootDir
    serverInstance.timeout=0.5
    serverInstance.js = self.js
    serverInstance.maxConnFileSize = self.maxConnFileSize

    # serverInstance6 = None
    # try: 
    #   serverInstance6 = mProvHTTPServer((self.hostName6, self.serverPort), mProvHTTPReqestHandler)
    #   serverInstance6.rootDir = self.rootDir
    #   serverInstance6.timeout=0.5
    #   serverInstance6.js = self.js
    #   serverInstance6.maxConnFileSize = self.maxConnFileSize
      
    # except Exception as e:
    #   serverInstance6 = None
    #   print(f"{e}")
    #   pass
    # this should allow us to exit out ok.
    while(self.js.running):
      serverInstance.handle_request()
      # if serverInstance6 is not None:
      #   serverInstance6.handle_request()
    
    log.info("Stopping mProv Webserver.")
    
    
