from http import server
import os
from .plugin import JobServerPlugin
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from http import HTTPStatus
import multiprocessing

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
    if not os.path.isdir(path):
      if path.endswith("/"):
        self.send_error(HTTPStatus.NOT_FOUND, "File not found, filename invalid")
        return False
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
      
    if os.getloadavg()[0] >= multiprocessing.cpu_count():
      print("Not Registering, high load.")
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

class mprov_webserver(JobServerPlugin):
  jobModule = 'mprov-webserver'
  hostName = "0.0.0.0"
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
    # this should allow us to exit out ok.
    while(self.js.running):
      serverInstance.handle_request()
    
    print("Stopping mProv Webserver.")
    
    
