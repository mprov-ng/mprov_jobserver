
from datetime import datetime
import importlib
import yaml
import time
import requests
import sys
import socket
import platform
import json
import os
from inspect import isclass
from .plugins.plugin import JobServerPlugin
import mprov.mprov_jobserver.plugins


class JobServer ():
  mprovURL = "http://127.0.0.1:8080/"
  running = True
  session = requests.Session()
  job_module_plugins = {}
  heartbeatInterval = 10
  configfile="/etc/mprov/jobserver.conf"
  ip_address = ""
  plugin_dir = os.path.dirname(os.path.realpath(__file__)) + '/plugins/'
  jobmodules = []
  running_threads = {}
  config_data = {}
  apikey = ""
  


  def __init__(self, **kwargs):
    print("mProv Job Server Starting.")

    # if we get passed in a config file, let's use that
    if 'configfile' in kwargs:
      self.configfile = kwargs['configfile']

    # load our config
    self.load_config()
    
    # Load Plugins. (plugins register job modules.)
    self.load_plugins()

    # Authenticat to the Control Center and start a session
    print("mProv Job Server authenticating.")
    if not self.startSession():
        print("Error: Unable to log into mProv Control Center.",file=sys.stderr)
        return None

    # register the server.
    self.register_server()

    pass

  def load_config(self):
    # load the config yaml
    if os.path.isfile(self.configfile) and os.access(self.configfile, os.R_OK):
      with open(self.configfile, "r") as yamlfile:
        self.data = yaml.load(yamlfile, Loader=yaml.FullLoader)
    else:
      with open(os.getcwd() + "/jobserver.conf", "r") as yamlefile:
        self.data = yaml.load(yamlefile, Loader=yaml.FullLoader)
    # map the global config on to our object
    for config_entry in self.data[0]['global'].keys():
      try:
        getattr(self, config_entry)
        setattr(self, config_entry, self.data[0]['global'][config_entry])
      except:
        print("Error: " + config_entry + " is not a valid config entry in 'global'.", file=sys.stderr)
        sys.exit(1)
    pass

  def load_plugins(self):
    # Load plugins set in the config file.
    for mod in self.jobmodules:
      # self.job_module_plugins[mod] = importlib.import_module('.' + mod, 'mprov.mprov_jobserver.plugins')
      attribute = getattr(mprov.mprov_jobserver.plugins, mod.replace('-', '_'))
        # attribute = getattr(attribute, attribute_name.replace('-', '_'))
      if isclass(attribute) and issubclass(attribute, JobServerPlugin):
        globals()[mod.replace('-', '_')] = attribute
            
    pass

  def stop(self):
    self.running = False

  def start(self):
    # Start processing jobs.
    # this strange looking loop allows us to die quickly on SIGTERM
    counter=self.heartbeatInterval
    while(self.running):
      if(counter == self.heartbeatInterval):
        for mod in self.jobmodules:
          # first check if the thread is done running.
          if mod in self.running_threads: 
            # check if this thread is still running
            if not self.running_threads[mod].isAlive() :
              #print ("Thread " + mod + " ended.")
              # if it's done running remove it.
              self.running_threads[mod].handled = True
              del self.running_threads[mod]

          # if a thread of this plugin is not running, start one.
          if mod not in self.running_threads:
            #print ("Starting mod... " + mod)
            mod_cls = getattr(mprov.mprov_jobserver.plugins, mod.replace('-', '_'))
            mod_cls = getattr(mod_cls, mod.replace('-', '_'))
            self.running_threads[mod] = mod_cls(self)
            self.running_threads[mod].start()
            
        #print(".", sep=None)
        self.register_server()
        counter=0
      counter+=1
      time.sleep(1)
    return 0


  
  def update_job_status(self, job_module, status):
    data = {
      'pk': job_module,
      'status': status,
    }
    if(status == 2) :
      # setting the job to running
      data['start_time'] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
      data['end_time'] = None
    elif(status == 3) or (status == 4) :
      # job failed
      data['end_time'] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    response = self.session.get(self.mprovURL + 'jobs/?search=' + job_module, )
    if response.status_code == 400:
        print("Error: Server returned error 400. Make sure your specified jobmodules exist.",file=sys.stderr)
        exit(1)
    if (response.json() == [] ):
      return False
    
    # no need to update if our status hasn't changed.
    if(response.json()[0]['status'] == status ):
      return False

    # if we are in an end state, don't update.
    if(response.json()[0]['status'] == 3) or (response.json()[0]['status'] == 4):
      return False

    data['id'] = response.json()[0]['id']
    #print(data)
    response = self.session.patch(self.mprovURL + 'jobs/' + str(data['id']) + '/', data=json.dumps(data))

    if response.status_code == 400:
      print("Oops")
      print(response.json())
      return False
    return True

    #print(job_module + " " + str(status))
    pass

  def register_server(self):
    # get my hostname from platform
    myHostname = platform.node()

    # setup or server info register payload
    data = {
        'name': myHostname,
        'address': self.ip_address,
        'jobmodules': self.jobmodules,

    }
    #print(data)
    # post the response (maybe should be put?) to the server.
    response = self.session.post(self.mprovURL + 'jobservers/', data=json.dumps(data), )
    if response.status_code == 400:
        print("Error: Server returned error 400. Make sure your specified jobmodules exist.",file=sys.stderr)
        exit(1)
    #print(response)
    print("Heartbeat - " + datetime.now().strftime("%d/%m/%Y %H:%M:%S"))


  def startSession(self):
    
    self.session.headers.update({
      'Authorization': 'Api-Key ' + self.apikey,
      'Content-Type': 'application/json'
      })

    # test connectivity
    response = self.session.get(self.mprovURL, stream=True)
    # get the sock from the session
    s = socket.fromfd(response.raw.fileno(), socket.AF_INET, socket.SOCK_STREAM)
    # get the address from the socket
    (address, myport) = s.getsockname()
    self.ip_address=address
    # if we get a response.status_code == 200, we're ok.  If not,
    # our auth failed.
    return response.status_code == 200

 