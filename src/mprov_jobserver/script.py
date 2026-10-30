
import requests
import socket
from mprov_jobserver.tls import ca_bundle
import yaml, os, sys, time
from mprov_jobserver.log import get_logger

log = get_logger(__name__)
class MProvScript():
  '''
  This class is a template that can be used by scripts run
  by the 'script-runner' to have access to the jobserver 
  config to run against the API.
  '''
  config_data = {}
  mprovURL = "http://127.0.0.1:8080/"
  apikey = ""
  # CA bundle for HTTPS to the mPCC; unset, the OS bundle is used (#102)
  caBundle = ""
  heartbeatInterval = 10
  runonce = True
  sessionOk = False
  disklayout = {}
  session = requests.Session()
  ip_address = None
  configfile = "/etc/mprov/script-runner.yaml"
  def __init__(self, **kwargs):
    # load our config
    self.load_config()

    # start a session
    if not self.startSession():
      log.error("Unable to communicate with the mPCC.")
      sys.exit(1)

  def yaml_include(self, loader, node):
    # disable includes, no need.
    return {}

  def load_config(self):
    # load the config yaml
    # print(self.configfile)
    yaml.add_constructor("!include", self.yaml_include)
    
    if not(os.path.isfile(self.configfile) and os.access(self.configfile, os.R_OK)):
      log.error("Unable to find a working config file.")
      sys.exit(1)

    with open(self.configfile, "r") as yamlfile:
      self.config_data = yaml.load(yamlfile, Loader=yaml.FullLoader)

    # flatten the config space
    result = {}
    for entry in self.config_data:
      result.update(entry)
    self.config_data = result

    # map the global config on to our object
    for config_entry in self.config_data['global'].keys():
      try:
        getattr(self, config_entry)
        setattr(self, config_entry, self.config_data['global'][config_entry])
      except:
        # ignore unused keys
        pass
    pass

  def startSession(self):
    
    self.session.headers.update({
      'Authorization': 'Api-Key ' + self.apikey,
      'Content-Type': 'application/json'
      })

    # connect to the mPCC
    try:
      self.caBundle = ca_bundle(self.caBundle)
    except FileNotFoundError as err:
      log.error(f"{err}.")
      return False
    if self.caBundle:
      self.session.verify = self.caBundle
    try:
      response = self.session.get(self.mprovURL, stream=True)
    except Exception as err:
      # say why: "Communication error" alone hid certificate problems (#102)
      log.error(f"unable to reach the mPCC at {self.mprovURL}: {err}")
      log.error("Communication error to the server.  Retrying.")
      self.sessionOk = False
      time.sleep(self.heartbeatInterval)
      return False
    self.sessionOk = True
    # our address on the connection to the mPCC: the first item of the
    # socket's (address, port, ...), not the whole tuple (#160).  A socket
    # on a copy of the connection's fd, so it takes the connection's own
    # family (IPv4 or IPv6), and is closed again straight away.
    with socket.socket(fileno=os.dup(response.raw.fileno())) as s:
      self.ip_address = s.getsockname()[0]
      
    # if we get a response.status_code == 200, we're ok.  If not,
    # our auth failed.
    return response.status_code == 200

  # OVERRIDE THIS!
  def run(self):
    pass

  def main(self):
    self.startSession()
    self.run()
