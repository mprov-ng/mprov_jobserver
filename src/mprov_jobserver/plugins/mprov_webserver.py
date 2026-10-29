from http import server
import os
from .plugin import JobServerPlugin
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from http import HTTPStatus
import multiprocessing, socket, stat, threading

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

  def refuseBusy(self):
    # 404 will result in a retry from the mPCC/client hopefully to another
    # server: the mPCC HEADs each job server before redirecting a client,
    # and skips one that refuses.  Keep heartbeating while full, or the
    # mPCC deletes this job server after a minute, with its images'
    # "Hosted By" and its jobs (#148).
    self.send_error(HTTPStatus.NOT_FOUND, "File not found, max connections reached")

  def do_GET(self):
    print(mProvHTTPReqestHandler.connCount)
      
    # the job server is the server's, not the handler's (#147)
    if os.getloadavg()[1] >= multiprocessing.cpu_count() \
        and self.server.js is not None and self.server.js.loadmon \
        and self.isImageRequest():
      print("Not Serving, high load.")
      self.send_error(HTTPStatus.NOT_FOUND, "Unable to serve, High load")
      return False

    if not self.isLimitedImage():
      return super().do_GET()

    with mProvHTTPReqestHandler.connLock:
      busy = mProvHTTPReqestHandler.connCount >= mProvHTTPReqestHandler.maxConn
      if not busy:
        mProvHTTPReqestHandler.connCount += 1
    if busy:
      self.refuseBusy()
      return None
    # we are ok to serve; give the slot back however the download ends.
    try:
      return super().do_GET()
    finally:
      with mProvHTTPReqestHandler.connLock:
        mProvHTTPReqestHandler.connCount -= 1

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
      print(f"Error: mProv Webserver not started: rootDir '{self.rootDir}' is empty, /, or not a directory.  Set rootDir in mprov-webserver.yaml.")
      self.failed = True
      return
    print(f"Starting mProv Webserver on port {self.serverPort} serving {self.rootDir} ...")

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
    
    print("Stopping mProv Webserver.")
    
    
