from http import server
import os
from .plugin import JobServerPlugin
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from http import HTTPStatus

class mProvHTTPReqestHandler(SimpleHTTPRequestHandler):
  # TODO: make maxConn and maxConnFileSize configurable through
  # the config yaml.
  connCount=0
  maxConn=10
  maxConnFileSize=104857600 #100MB
  def checkFileSize(self):
    self.directory = self.server.rootDir
    path = self.translate_path(self.path)
    if not os.path.isdir(path):
      if path.endswith("/"):
        self.send_error(HTTPStatus.NOT_FOUND, "File not found")
        return False
    f=None
    try:
      f = open(path, 'rb')
    except OSError:
      self.send_error(HTTPStatus.NOT_FOUND, "File not found")
      return False
    try:
      fs = os.fstat(f.fileno())
      if fs[6] >= mProvHTTPReqestHandler.maxConnFileSize:
        if mProvHTTPReqestHandler.connCount >= mProvHTTPReqestHandler.maxConn:
          # 404 will result in a retry from the mPCC/client hopefully to another server.
          self.send_error(HTTPStatus.NOT_FOUND, "File not found")
          return False
    except:
      f.close()
      print("Exception")
      raise
    return True

  def do_GET(self):
    retVal = None
    if self.checkFileSize():
      # we are ok to serve.
      mProvHTTPReqestHandler.connCount += 1
      retVal = super().do_GET()
      mProvHTTPReqestHandler.connCount -= 1
    else:
      print("Error: checkFileSize failed.")

    return retVal

  def do_HEAD(self):
    if not self.checkFileSize():
      print("Error: checkFileSize failed.")
      return None
    return super().do_HEAD()

class mProvHTTPServer(ThreadingHTTPServer):
  rootDir = ""      

class mprov_webserver(JobServerPlugin):
  jobModule = 'mprov-webserver'
  hostName = "0.0.0.0"
  serverPort = 8080
  serverInstance = None
  rootDir = ""
  def handle_jobs(self):
    print(f"Starting mProv Webserver on port {self.serverPort}...")

    serverInstance = mProvHTTPServer((self.hostName, self.serverPort), mProvHTTPReqestHandler)
    serverInstance.rootDir = self.rootDir
    serverInstance.timeout=0.5
    # this should allow us to exit out ok.
    while(self.js.running):
      serverInstance.handle_request()
    
    print("Stopping mProv Webserver.")
    
    