import os
from .plugin import JobServerPlugin
from time import sleep
from http.server import BaseHTTPRequestHandler, HTTPServer
from mprov.common.exceptions import Exit

class HTTPImageServer(BaseHTTPRequestHandler):
  imageDir = ""
  def __init__(self):
    pass

  def __call__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)

  def do_GET(self):
      self.send_response(200)
      self.send_header("Content-type", "application/octet-stream")
      
      # sanitize self.path for directory escape characters.  Is this enough?
      self.path = "/" + os.path.relpath(os.path.normpath(os.path.join("/", self.path)), "/")

      imageName = os.path.splitext(self.path)[0]
      filename = self.imageDir + '/' + imageName + self.path
      # print(filename)
      size = os.path.getsize(filename)
      self.send_header("Content-Length", size)
      self.end_headers()

      with open(filename, "rb") as imageFile:
        self.wfile.write(imageFile.read())
  
      

class image_server(JobServerPlugin):
  jobModule = 'image-server'
  hostName = "localhost"
  serverPort = 80
  serverInstance = None
  imageDir = ""
  def handle_jobs(self):
    print("Starting Image Server...")
    ReqHandler = HTTPImageServer()
    ReqHandler.imageDir = self.imageDir
    serverInstance = HTTPServer((self.hostName, self.serverPort), ReqHandler)
    
    try:
      serverInstance.serve_forever()
    except Exit:
        pass
    serverInstance.server_close()