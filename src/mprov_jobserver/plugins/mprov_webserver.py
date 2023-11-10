from http import server
import os
from .plugin import JobServerPlugin
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from http import HTTPStatus
import multiprocessing, socket

class mProvHTTPReqestHandler(SimpleHTTPRequestHandler):
  # TODO: make maxConn and maxConnFileSize configurable through
  # the config yaml.
  connCount=0
  maxConn=10
  maxConnFileSize=0 
  def checkFileSize(self):
    self.maxConnFileSize = self.server.maxConnFileSize
    self.directory = self.server.rootDir
    path = self.translate_path(self.path)
    # only apply to images.
    if not path.startswith("/images/") :
      return True
    if not os.path.isdir(path):
      if path.endswith("/"):
        self.send_error(HTTPStatus.NOT_FOUND, "File not found, filename invalid")
        return False
    else:
      return True
    f=None
    try:
      f = open(path, 'rb')
    except OSError:
      self.send_error(HTTPStatus.NOT_FOUND, "File not found, unable to open")
      return False
    try:
      fs = os.fstat(f.fileno())
      if fs[6] >= mProvHTTPReqestHandler.maxConnFileSize:
        if mProvHTTPReqestHandler.connCount >= mProvHTTPReqestHandler.maxConn:
          # 404 will result in a retry from the mPCC/client hopefully to another server.
          self.send_error(HTTPStatus.NOT_FOUND, "File not found, max connections reached")
          if self.server.js is not None:
            self.server.js.register = False
          return False
    except:
      f.close()
      print("Exception")
      raise
    return True

  def do_GET(self):
    print(mProvHTTPReqestHandler.connCount)
      
    if os.getloadavg()[1] >= multiprocessing.cpu_count() \
        and self.js.config_data['loadmon']\
        and self.path.startswith("/images/"):
      print("Not Serving, high load.")
      self.send_error(HTTPStatus.NOT_FOUND, "Unable to serve, High load")
      return False
      
    retVal = None
    if self.checkFileSize():
      # we are ok to serve.
      mProvHTTPReqestHandler.connCount += 1
      try:
        retVal = super().do_GET()
      finally:
        mProvHTTPReqestHandler.connCount -= 1
        if mProvHTTPReqestHandler.connCount < mProvHTTPReqestHandler.maxConn:
          if self.server.js is not None:
            self.server.js.register = True

    return retVal

  def do_HEAD(self):
    if not self.checkFileSize():
      return None
    return super().do_HEAD()

class mProvHTTPServer(ThreadingHTTPServer):
  rootDir = ""  
  maxConnFileSize = 0
  js = None    
  def __init__(self, server_address, RequestHandlerClass, bind_and_activate=True):
    if ":" in server_address[0]:
      # we have an IPv6 bind address
      self.address_family = socket.AF_INET6
    super().__init__(server_address, RequestHandlerClass, bind_and_activate)
    # self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)

class mprov_webserver(JobServerPlugin):
  jobModule = 'mprov-webserver'
  hostName = "::"
  serverPort = 8080
  serverInstance = None
  rootDir = ""
  maxConnFileSize = 0 

  def handle_jobs(self):
    print(f"Starting mProv Webserver on port {self.serverPort}...")

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
    
    
