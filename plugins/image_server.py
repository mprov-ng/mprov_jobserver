from http import server
import os
from .plugin import JobServerPlugin
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class HTTPImageServer(BaseHTTPRequestHandler):
  imageDir = ""
  def __init__(self):
    pass

  def __call__(self, *args, **kwargs):
    try: 
      super().__init__(*args, **kwargs)
    except:
      pass

  def do_GET(self):
      self.send_response(200)
      self.send_header("Content-type", "application/octet-stream")
      
      # sanitize self.path for directory escape characters.  Is this enough?
      self.path = "/" + os.path.relpath(os.path.normpath(os.path.join("/", self.path)), "/")

      #imageName = self.path.split('.', 1)[0]
      imageName = self.path.split('.', 1)[0]
      filename = self.imageDir + '/' + imageName + self.path
      # print(filename)
      size = os.path.getsize(filename)
      self.send_header("Content-Length", size)
      self.send_header("Content-Disposition", "inline; filename=\"" + self.path + "\"")
      self.end_headers()

      with open(filename, "rb") as imageFile:
        self.wfile.write(imageFile.read())
  
      

class image_server(JobServerPlugin):
  jobModule = 'image-server'
  hostName = "0.0.0.0"
  serverPort = 80
  serverInstance = None
  imageDir = ""
  def handle_jobs(self):
    print("Starting Image Server...")
    ReqHandler = HTTPImageServer()
    ReqHandler.imageDir = self.imageDir
    serverInstance = ThreadingHTTPServer((self.hostName, self.serverPort), ReqHandler)
    serverInstance.timeout=0.5
    # this should allow us to exit out ok.
    while(self.js.running):
      serverInstance.handle_request()
    
    print("Stopping Image Server.")
    #serverInstance.serve_forever()
    
    