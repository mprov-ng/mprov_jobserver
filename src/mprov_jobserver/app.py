
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
import mprov_jobserver.plugins
import glob



class JobServer ():
  mprovURL = "http://127.0.0.1:8080/"
  running = True
  session = requests.Session()
  job_module_plugins = {}
  heartbeatInterval = 10
  configfile="/etc/mprov/jobserver.yaml"
  ip_address = ""
  myaddress = ""
  plugin_dir = os.path.dirname(os.path.realpath(__file__)) + '/plugins/'
  jobmodules = []
  running_threads = {}
  config_data = {}
  apikey = ""
  sessionOk = False
  id = -1
  runonce = False
  register = True


  def __init__(self, **kwargs):
    print("mProv Job Server Starting.")


    # if we get passed in a config file, let's use that
    if 'configfile' in kwargs:
      self.configfile = kwargs['configfile']
    
    # load our config
    self.load_config()
    
    # set runonce if someone sent it to us.
    if 'runonce' in kwargs:
      self.runonce = kwargs['runonce']
      
      
    if '-d' in set(sys.argv):
      self.register = False


    if '-m' in set(sys.argv):
      # -m overrides whatever is in the config.
      # so we will re-write the self.jobmodules list
      self.jobmodules= sys.argv[sys.argv.index('-m')+1].split(',')
      # we are also going to specify runonce
      self.runonce=True
      # we also don't want to register with mPCC
      self.register = False

    # Authenticat to the Control Center and start a session
    print("mProv Job Server authenticating.")
    if not self.startSession():
        print("Error: Unable to log into mProv Control Center.",file=sys.stderr)


    # Load Plugins. (plugins register job modules.)
    self.load_plugins()

    # register the server.
    self.register_server()

    pass

  def yaml_include(self, loader, node):
      # Get the path out of the yaml file
    file_name = os.path.join(os.path.dirname(loader.name), node.value)
    
    # we have a glob, so we will iterate.
    result = {}
    for file in glob.glob(file_name):
      with open(file) as inputfile:
        result.update(yaml.load(inputfile, Loader=yaml.FullLoader)[0])
    return result



  def load_config(self):
    # load the config yaml
    # print(self.configfile)
    yaml.add_constructor("!include", self.yaml_include)
    if not(os.path.isfile(self.configfile) and os.access(self.configfile, os.R_OK)):
      self.configfile = os.getcwd() + "/jobserver.yaml"
    # print(self.configfile)
    if not(os.path.isfile(self.configfile) and os.access(self.configfile, os.R_OK)):
      print("Error: Unable to find a working config file.")
      print("Try passing one in with the '-c' option.")
      sys.exit(1)


    with open(self.configfile, "r") as yamlfile:
      self.config_data = yaml.load(yamlfile, Loader=yaml.FullLoader)

    # flatten the config space
    result = {}
    for entry in self.config_data:
      result.update(entry)
    self.config_data = result
  
    # pp = pprint.PrettyPrinter(indent=2,width=100,)
    # pp.pprint(self.config_data)
    # map the global config on to our object
    for config_entry in self.config_data['global'].keys():
      try:
        getattr(self, config_entry)
        setattr(self, config_entry, self.config_data['global'][config_entry])
      except:
        print("Error: " + config_entry + " is not a valid config entry in 'global'.", file=sys.stderr)
        sys.exit(1)
    pass

  def load_plugins(self):
    if self.jobmodules is None:
      print("Error: You must specify at least 1 jobmodule!")
      sys.exit(1)
    # Load plugins set in the config file.
    for mod in self.jobmodules:
      # self.job_module_plugins[mod] = importlib.import_module('.' + mod, 'mprov.mprov_jobserver.plugins')
      attribute = getattr(mprov_jobserver.plugins, mod.replace('-', '_'))
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
      if self.sessionOk is False:
        self.startSession()
      else:
        if(counter == self.heartbeatInterval):
          for mod in self.jobmodules:
            # first check if the thread is done running.
            if mod in self.running_threads: 
              # check if this thread is still running
              if not self.running_threads[mod].isAlive() :
                # print ("Thread " + mod + " ended.")
                # if it's done running remove it.
                self.running_threads[mod].handled = True
                del self.running_threads[mod]

            # if a thread of this plugin is not running, start one.
            if mod not in self.running_threads:
              # print ("Starting mod... " + mod)
              # attempt to reload the module, just in case
              #print(f"Starting {mod.replace('-', '_')}")
              if f"mprov_jobserver.plugins.{mod.replace('-', '_')}" in sys.modules:
                #reload
                #print(f"Reload mprov_jobserver.plugins.{mod.replace('-', '_')}")
                importlib.reload(sys.modules[f"mprov_jobserver.plugins.{mod.replace('-', '_')}"])

              mod_cls = getattr(mprov_jobserver.plugins, mod.replace('-', '_'))
              mod_cls = getattr(mod_cls, mod.replace('-', '_'))
              self.running_threads[mod] = mod_cls(self)
              self.running_threads[mod].start()
              
          #print(".", sep=None)
          if not self.runonce:
            self.register_server()
          counter=0
        counter+=1
        time.sleep(1)
        if self.runonce:
          print("Job Server in 'runonce' mode.")
          self.running = False
    print("\nJob Server Exiting...")
    # wait for any jobmodules to complete.
    for mod in self.jobmodules:
        # first check if the thread is done running.
        if mod in self.running_threads: 
          # check if this thread is still running
          # print("\tWaiting on thread " + mod)
          self.running_threads[mod].join()
          
    
    return 0


  
  def update_job_status(self, job_module, status, jobid=None,jobquery=""):
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
    if jobid is not None:
      # get the specific job status
      queryURL = self.mprovURL + 'jobs/' + str(jobid) + '/'
    elif jobquery != "":
      queryURL = self.mprovURL + 'jobs/?' + jobquery
    else:
      queryURL = self.mprovURL + 'jobs/?search=' + job_module
    response = self.session.get( queryURL )
    if response.status_code == 400:
        print("Error: Server returned error 400. Make sure your specified jobmodules exist.",file=sys.stderr)
        exit(1)
    if (response.json() == [] ):
      return False
    # print(job_module)
    # print(response.json())
    updateCount = 0
    jobs = response.json()
    # single objects still need to be in a list.
    if type(jobs) != list:
      jobs = [jobs]
    for job in jobs:

      # no need to update if our status hasn't changed.
      # print("\t" + str(job['id']))
      if(job['status'] == status ):
        continue

      # if we are in an end state, don't update.
      if(job['status'] == 3) or (job['status'] == 4):
        continue

      data['id'] = job['id']
      data['jobserver'] = self.id
      # print(data)
      response = self.session.patch(self.mprovURL + 'jobs/' + str(data['id']) + '/', data=json.dumps(data))
      updateCount += 1

      if response.status_code == 400:
        print("Oops!, JobID: " + str(data['id']))
        print(response.json())
        continue

    return updateCount
    # return True

    #print(job_module + " " + str(status))
    pass

  def register_server(self):
    if not self.register:
      return
    # get my hostname from platform
    myHostname = platform.node()

    # setup or server info register payload
    data = {
        'name': myHostname,
        'address': self.ip_address,
        'jobmodules': self.jobmodules,
        'one_minute_load': os.getloadavg()[0]
    }
    # print(self.config_data)
    if 'image-server' in self.config_data:
    # if  self.config_data['image-server']:
      if 'serverPort' in self.config_data['image-server']:
        # print(self.config_data['image-server']['serverPort'])
        data['port'] = self.config_data['image-server']['serverPort']
    
    #print(data)
    # post the response (maybe should be put?) to the server.
    try: 
      response = self.session.post(self.mprovURL + 'jobservers/', data=json.dumps(data), )
      
    except:
      self.sessionOk = False
      self.startSession()
      return
    
    if response.status_code == 400:
        print("Error: Server returned error 400. Make sure your specified jobmodules exist.",file=sys.stderr)
        exit(1)
    if response.status_code == 500:
        print("Error: The mPCC had an internal server error.")
        exit(1)
    # pp = pprint.PrettyPrinter(indent=2,width=100,)
    # pp.pprint(vars(response))
    # # print(response.text)
    if type(response.json()) is dict:
      print("Error: Invalid response from mPCC")
      print(response.json())
      sys.exit(1)
    # print(response.json())
    result = json.loads(response.json())
    
    # grab our id from the MPCC
    self.id = result['pk']

    print("Heartbeat - " + datetime.now().strftime("%d/%m/%Y %H:%M:%S"))


  def startSession(self):
    
    self.session.headers.update({
      'Authorization': 'Api-Key ' + self.apikey,
      'Content-Type': 'application/json'
      })

    # test connectivity
    try:
      response = self.session.get(self.mprovURL, stream=True)
    except:
      print("Error: Communication error to the server.  Retrying.", file=sys.stderr)
      self.sessionOk = False
      time.sleep(10)
      return
    self.sessionOk = True
    if self.myaddress is not None:
      if self.myaddress != '':
        self.ip_address = self.myaddress
      else:
        print("Warning: No address set in config, attempting autodetection.  This may not work right...")
        # get the sock from the session
        s = socket.fromfd(response.raw.fileno(), socket.AF_INET, socket.SOCK_STREAM)
        # get the address from the socket
        address = s.getsockname()
        self.ip_address=address
      
    # if we get a response.status_code == 200, we're ok.  If not,
    # our auth failed.
    return response.status_code == 200

 