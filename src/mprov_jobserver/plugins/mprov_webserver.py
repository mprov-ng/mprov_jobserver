from http import server
import os
from .plugin import JobServerPlugin
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

class mProvHTTPReqestHandler(SimpleHTTPRequestHandler):

  def do_GET(self):
    self.directory = self.server.rootDir
    return super().do_GET()    


class mProvHTTPServer(ThreadingHTTPServer):
  rootDir = ""      

class mprov_webserver(JobServerPlugin):
  jobModule = 'mprov-webserver'
  hostName = "0.0.0.0"
  serverPort = 80
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
    
    