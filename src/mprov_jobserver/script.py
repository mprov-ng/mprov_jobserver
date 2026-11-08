
import requests
import socket
from mprov_jobserver.tls import ca_bundle
from mprov_jobserver import identity as ident
import yaml, os, sys, time
from mprov_jobserver.log import get_logger
from mprov_jobserver.config import merge_config
from mprov_jobserver.version import VERSION_HEADER

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
  # CA bundle for HTTPS to the mPCC; unset, the OS bundle is used (#102),
  # or the mPCC's CA next to the node's certificate
  caBundle = ""
  # where this node's own key and certificate are, which the mPCC's boot
  # scripts enrolled it for (mprov_control_center#381).  Unset,
  # /etc/mprov/tls.  Without one, the shared API key is used.
  nodeTlsDir = ""
  heartbeatInterval = 10
  runonce = True
  sessionOk = False
  disklayout = {}
  session = requests.Session()
  ip_address = None
  configfile = "/etc/mprov/script-runner.yaml"
  # A subclass sets this to its own version to report it to the mPCC in the
  # mProv-Version header (#452).  Left unset, no version is sent.
  script_version = None
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

    # flatten the config space, the same way as the job server (#301)
    self.config_data = merge_config(self.config_data)

    # map the global config on to our object
    for config_entry in self.config_data['global'].keys():
      try:
        getattr(self, config_entry)
        setattr(self, config_entry, self.config_data['global'][config_entry])
      except AttributeError:
        # ignore unused keys
        pass
    pass

  def startSession(self):
    caBundleSet = bool(self.caBundle)
    self.session.headers.update({'Content-Type': 'application/json'})
    # this node's own certificate, over HTTPS to the same host, as the
    # mPCC's boot scripts use it; else the shared API key, for an image built
    # before 0.7.0 (mprov_control_center#381)
    node = ident.node_credential(self.nodeTlsDir)
    if node is not None:
      self.mprovURL = ident.https_url(self.mprovURL)
      self.session.cert = (node.cert_path, node.key_path)
      self.session.headers.pop('Authorization', None)
    else:
      self.session.headers['Authorization'] = 'Api-Key ' + self.apikey
    # A script reports its own version for the mPCC's logs and exemption
    # list, and doesn't check the mPCC's in return: scripts are baked into
    # images that can't be rebuilt with every mPCC release, so an mPCC on a
    # newer version has to be able to answer them (#452).
    if self.script_version:
      self.session.headers.update({VERSION_HEADER: self.script_version})

    # connect to the mPCC
    try:
      self.caBundle = ca_bundle(self.caBundle)
    except FileNotFoundError as err:
      log.error(f"{err}.")
      return False
    if node is not None and not caBundleSet:
      self.session.verify = node.ca_path
    elif self.caBundle:
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
