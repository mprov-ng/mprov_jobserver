
from datetime import datetime, timezone
import importlib
import yaml
import time
import requests
from mprov_jobserver.tls import ca_bundle
import sys
import socket
import platform
import json
import os
from inspect import isclass
from .plugins.plugin import JobServerPlugin
import mprov_jobserver.plugins
import glob
import multiprocessing
import uuid
from mprov_jobserver.log import get_logger, configure as log_configure

log = get_logger(__name__)


def job_subject(job):
  """ What a job is about, from its params: an image or repo id, or "". """
  params = job.get('params') if isinstance(job, dict) else None
  if isinstance(params, dict):
    for key in ('imageId', 'repo_id'):
      if params.get(key) not in (None, ""):
        return f" {params[key]}"
  return ""


def log_job(job_module, job, status):
  """
  One line per job transition, in a fixed form a log search (Loki, Grafana)
  can match: "job 42 image-update compute started", then "succeeded" or
  "failed" with how long it ran, from the job's start_time (#103).
  """
  module = job.get('module') or job_module
  what = f"job {job['id']} {module}{job_subject(job)}"
  if status == 2:
    log.info(f"{what} started")
    return
  took = ""
  try:
    started = datetime.fromisoformat(str(job.get('start_time')).replace('Z', '+00:00'))
    if started.tzinfo is None:
      started = started.replace(tzinfo=timezone.utc)
    took = f" ({int((datetime.now(timezone.utc) - started).total_seconds())}s)"
  except ValueError:
    pass
  if status == 4:
    log.info(f"{what} succeeded{took}")
  elif status == 3:
    log.error(f"{what} failed{took}")
  else:
    log.info(f"{what} set to status {status}")


class JobServer ():
  mprovURL = "http://127.0.0.1:8080/"
  running = True
  session = requests.Session()
  job_module_plugins = {}
  heartbeatInterval = 10
  configfile="/etc/mprov/jobserver.yaml"
  ip_address = ""
  myaddress = ""
  loadmon=True
  plugin_dir = os.path.dirname(os.path.realpath(__file__)) + '/plugins/'
  jobmodules = []
  running_threads = {}
  config_data = {}
  apikey = ""
  # CA bundle for HTTPS to the mPCC; unset, the OS bundle is used (#102)
  caBundle = ""
  # this host's stable id for the mPCC; unset, a random one is kept in
  # host-id next to the config file (#113)
  hostId = ""
  _hostIdValue = None
  # how much the log says (error, warning, info, debug), and whether lines
  # carry a timestamp; journald and docker stamp lines themselves (#104)
  logLevel = "info"
  logTimestamps = False
  sessionOk = False
  id = -1
  runonce = False
  register = True
  highLoad = False
  firstrun = True
  # jobs left RUNNING by an earlier run of this job server are failed once,
  # before any module starts (#94)
  orphansChecked = False
  # what the mPCC said it supports in its last registration reply (#248)
  features = frozenset()
  # jobmodules that ran and failed in runonce mode, checked before exiting (#174)
  failed_runonce_modules = []

  def __init__(self, **kwargs):
    log.info("mProv Job Server Starting.")


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
    log.info("mProv Job Server authenticating.")
    if not self.startSession():
        log.error("Unable to log into mProv Control Center.")


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
      log.error("Unable to find a working config file.")
      log.error("Try passing one in with the '-c' option.")
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
        if config_entry == "mprovURL":
          # add a trailing slash if one doesn't exist.
          if self.config_data['global'][config_entry][-1] != "/":
            self.config_data['global'][config_entry] += "/"
        setattr(self, config_entry, self.config_data['global'][config_entry])
      except:
        log.error("" + config_entry + " is not a valid config entry in 'global'.")
        sys.exit(1)
    self.use_ca_bundle()
    if not log_configure(self.logLevel, self.logTimestamps):
      log.error(f"logLevel {self.logLevel!r} isn't one of error, warning, info or debug.")
      sys.exit(1)

  def load_plugins(self):
    if self.jobmodules is None:
      log.error("You must specify at least 1 jobmodule!")
      sys.exit(1)
    # Load plugins set in the config file.
    for mod in self.jobmodules:
      # self.job_module_plugins[mod] = importlib.import_module('.' + mod, 'mprov.mprov_jobserver.plugins')
      attribute = getattr(mprov_jobserver.plugins, mod.replace('-', '_'))
        # attribute = getattr(attribute, attribute_name.replace('-', '_'))
      if isclass(attribute) and issubclass(attribute, JobServerPlugin):
        globals()[mod.replace('-', '_')] = attribute
            
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
          # Not registered yet (the first registration failed, e.g. an mPCC
          # 500): without an id the plugins would claim jobs for job server
          # -1, so register first and start them once we have one (#159).
          retried = False
          if self.register and not self.runonce and self.id == -1:
            self.register_server()
            retried = True
          waiting = self.register and not self.runonce and self.id == -1
          if waiting:
            log.error("not registered with the mPCC yet, job modules wait until we are.")
          elif not self.orphansChecked:
            # nothing is running yet, so a job the mPCC has as RUNNING here
            # was cut off when this job server last stopped (#94)
            self.orphansChecked = True
            self.fail_orphaned_jobs(None, "it was running when this job server stopped")
          for mod in ([] if waiting else self.jobmodules):
            # first check if the thread is done running.
            if mod in self.running_threads: 
              # check if this thread is still running
              if not self.running_threads[mod].is_alive() :
                # print ("Thread " + mod + " ended.")
                # if it's done running remove it.
                ended = self.running_threads[mod]
                ended.handled = True
                del self.running_threads[mod]
                # a module waits for its own workers before it returns, so
                # nothing is working on a job it left RUNNING: it crashed or
                # forgot it.  Fail it before the next run claims more (#180).
                self.fail_orphaned_jobs(ended.claimedModules(), f"the {mod} module ended without finishing it")

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
          if not self.runonce and not retried:
            self.register_server()
          counter=0
        counter+=1
        time.sleep(1)
        self.firstrun = False
        if self.runonce:
          log.info("Job Server in 'runonce' mode.")
          self.running = False
    log.info("\nJob Server Exiting...")
    # wait for any jobmodules to complete.
    for mod in self.jobmodules:
        # first check if the thread is done running.
        if mod in self.running_threads: 
          # check if this thread is still running
          # print("\tWaiting on thread " + mod)
          self.running_threads[mod].join()

    if self.runonce:
      # a module that failed its run means this process exits nonzero (#174)
      self.failed_runonce_modules = [
        mod for mod in self.jobmodules
        if mod in self.running_threads and getattr(self.running_threads[mod], "failed", False)]
      if self.failed_runonce_modules:
        log.error("Jobmodule(s) failed: " + ", ".join(self.failed_runonce_modules))
        return 1

    return 0


  
  def update_job_status(self, job_module, status, jobid=None,jobquery=""):
    data = {
      'pk': job_module,
      'status': status,
    }
    # UTC with an explicit offset: the local clock without one was read in
    # the mPCC's TIME_ZONE, so the times were off on any job server whose
    # zone differed (#186).  e.g. 2026-10-30T17:42:18+00:00
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if(status == 2) :
      # setting the job to running
      data['start_time'] = now
      data['end_time'] = None
    elif(status == 3) or (status == 4) :
      # job finished
      data['end_time'] = now
    if jobid is not None:
      # get the specific job status
      queryURL = self.mprovURL + 'jobs/' + str(jobid) + '/'
    elif jobquery != "":
      queryURL = self.mprovURL + 'jobs/?' + jobquery
    elif status == 2:
      # claiming: only this module's pending jobs.  search= matched any
      # status and any job server (a substring search, at that), so a module
      # took over jobs other job servers were running, and the poll fetched
      # the module's whole history (#112).
      queryURL = self.mprovURL + 'jobs/?module=' + job_module + '&status=1'
    else:
      # finishing: only the jobs of this module we're running (#112)
      queryURL = self.mprovURL + 'jobs/?jobserver=' + str(self.id) + '&module=' + job_module + '&status=2'
    response = self.session.get( queryURL )
    if response.status_code <= 199 or response.status_code >=300:

        log.error(f"Server returned error {response.status_code}. ")
        return False
    try:
      if (response.json() == [] ):
        return False
    except:
      log.error(f"There was an error parsing output from the mPCC.  Output: ")
      log.error(f"{response.text}")
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
        log.error("the mPCC refused the update of job " + str(data['id']) + ":")
        log.error(response.json())
        continue
      log_job(job_module, job, status)

    return updateCount
    # return True

    #print(job_module + " " + str(status))
    pass

  def fail_orphaned_jobs(self, modules, reason):
    """ Fail the jobs the mPCC has as RUNNING on this job server, for the given
    job modules (all of them if None), that nothing here is working on any
    more (#94, #180).  Only for a registered, long-running job server: a
    runonce run has no id of its own.  Returns how many were failed. """
    if not self.register or self.runonce or self.id == -1 or modules == []:
      return 0
    query = 'jobs/?jobserver=' + str(self.id) + '&status=2'
    if modules is not None:
      # one module by name, several as a JSON list, as the mPCC takes them
      query += '&module=' + (modules[0] if len(modules) == 1 else json.dumps(modules, separators=(',', ':')))
    try:
      response = self.session.get(self.mprovURL + query)
      jobs = response.json() if 199 < response.status_code < 300 else []
    except Exception as err:
      log.error(f"unable to look for orphaned jobs: {err}")
      return 0
    if not isinstance(jobs, list):
      return 0
    failed = 0
    for job in jobs:
      if not isinstance(job, dict) or 'id' not in job:
        continue
      log.error(f"failing job {job['id']} ({job.get('module')}): {reason}.")
      self.update_job_status(job.get('module', ''), 3, jobid=job['id'])
      failed += 1
    return failed

  def reply_object(self, response):
    """ The mPCC's POST /jobservers/ reply as a dict, or None if it isn't one.
    The mPCC has double-encoded this reply (a JSON string holding the
    object), and will send the object itself (mprov_control_center#268);
    accept both. """
    try:
      result = response.json()
      if isinstance(result, str):
        result = json.loads(result)
    except Exception:
      return None
    return result if isinstance(result, dict) else None

  def register_server(self):
    if not self.register:
      return
    # get my hostname from platform
    myHostname = platform.node()
    if "." in myHostname:
      # remove the domain if one exists
      myHostname, _ = myHostname.split(".", 1)

    # setup or server info register payload
    data = {
        'name': myHostname,
        'address': self.ip_address,
        'jobmodules': self.jobmodules,
        'one_minute_load': os.getloadavg()[0]
    }
    # Keep heartbeating under high load: the mPCC deletes a job server that
    # goes quiet for a minute, and it already steers image downloads to the
    # least loaded one (#113).  loadmon is a global setting, so load_config()
    # put it on self (#147).  Only log the change, not every heartbeat.
    highLoad = data['one_minute_load'] >= multiprocessing.cpu_count() and self.loadmon
    if highLoad != self.highLoad:
      if highLoad:
        log.warning(f"high load ({data['one_minute_load']}), still heartbeating with the load reported.")
      else:
        log.info("load back to normal.")
      self.highLoad = highLoad
    # the mPCC knows us by this id, if it says it can, instead of by our
    # name, so a rename or another host with our name doesn't take over or
    # split our registration (#113)
    if 'host-id' in self.features:
      hostId = self.host_id()
      if hostId:
        data['host_id'] = hostId
    # print(self.config_data)
    if 'mprov-webserver' in self.config_data:
    # if  self.config_data['mprov-webserver']:
      if 'serverPort' in self.config_data['mprov-webserver']:
        # print(self.config_data['mprov-webserver']['serverPort'])
        data['port'] = self.config_data['mprov-webserver']['serverPort']
      else: 
        data['port'] = 8080
    
    #print(data)
    # post the response (maybe should be put?) to the server.
    try: 
      response = self.session.post(self.mprovURL + 'jobservers/', data=json.dumps(data), )
      
    except:
      self.sessionOk = False
      self.startSession()
      return
    
    if response.status_code == 400:
        log.error("Server returned error 400. Make sure your specified jobmodules exist.")
        self.sessionOk = False
        return
    if response.status_code == 409:
        # our name is registered by another job server (another host id)
        result = self.reply_object(response) or {}
        log.error(f"the mPCC refused our registration: {result.get('detail', response.text)}")
        log.error("give this host a unique name (or check hostId); will try again on the next heartbeat.")
        return
    if response.status_code == 500:
        log.error("The mPCC had an internal server error.")
        result = self.reply_object(response)
        if result is None or 'msg' not in result:
          log.error("And we were unable to load json from the response.")
          log.error(f"so here is the raw response: {response.text}")
          log.error("will try again on the next heartbeat.")
          return
        log.error(f"The mPCC said: \"{result['msg']}\"")
        # The one 500 the mPCC sends on purpose: a job module in our config
        # it doesn't know.  That won't fix itself, so stop loudly.  Any other
        # 500 may pass, and exiting made a restart loop for as long as the
        # mPCC was failing; try again on the next heartbeat (#159).
        if str(result['msg']).startswith("Error locating jobmodule"):
          log.error("check the jobmodules in your config against the mPCC's job modules.")
          sys.exit(1)
        log.error("will try again on the next heartbeat.")
        return

    result = self.reply_object(response)
    if result is None or 'pk' not in result:
      log.error("mPCC gave us an invalid reply.")
      log.error(response.text)
      return

    # grab our id from the MPCC
    self.id = result['pk']
    # and what it supports, read on every heartbeat so a downgraded mPCC
    # stops getting what it no longer understands (#248).  No key: none.
    features = result.get('features')
    self.features = frozenset(f for f in features if isinstance(f, str)) if isinstance(features, list) else frozenset()

    log.info("Heartbeat - " + datetime.now().strftime("%d/%m/%Y %H:%M:%S"))


  def host_id(self):
    '''
    This host's stable id for the mPCC (#113): the global hostId if set,
    else the one in host-id next to the config file (/etc/mprov/host-id),
    made on first use from a random uuid4.  Not /etc/machine-id: in the
    container image it's baked in at build time, so every container from
    one image has the same one.  None if there's no id and none can be
    saved; the job server then registers by its name, as before.
    '''
    if self._hostIdValue is not None:
      return self._hostIdValue or None
    value = str(self.hostId or "").strip()
    if not value:
      path = os.path.join(os.path.dirname(os.path.abspath(self.configfile)), 'host-id')
      try:
        with open(path) as idfile:
          value = idfile.read().strip()
      except FileNotFoundError:
        pass
      except OSError as err:
        log.warning(f"unable to read the host id in {path}: {err}")
      if not value:
        value = str(uuid.uuid4())
        try:
          with open(path, 'w') as idfile:
            idfile.write(value + "\n")
          os.chmod(path, 0o644)
          log.info(f"this host's id for the mPCC is {value}, saved in {path}.")
        except OSError as err:
          log.warning(f"unable to save a host id in {path}: {err}; registering by name.")
          value = ""
    if len(value) > 64:
      log.error(f"the host id {value!r} is longer than 64 characters; registering by name.")
      value = ""
    self._hostIdValue = value
    return value or None

  def use_ca_bundle(self):
    # verify the mPCC's certificate against the configured caBundle or the
    # OS bundle (#102).  An admin's REQUESTS_CA_BUNDLE still wins, as it
    # does for any requests program.
    try:
      self.caBundle = ca_bundle(self.caBundle)
    except FileNotFoundError as err:
      log.error(f"{err}; set global caBundle to the mPCC's CA certificate file.")
      sys.exit(1)
    if self.caBundle:
      self.session.verify = self.caBundle

  def startSession(self):
    
    self.session.headers.update({
      'Authorization': 'Api-Key ' + self.apikey,
      'Content-Type': 'application/json'
      })

    # test connectivity
    try:
      response = self.session.get(self.mprovURL, stream=True)
    except Exception as err:
      # say why: "Communication error" alone hid certificate problems (#102)
      log.error(f"unable to reach the mPCC at {self.mprovURL}: {err}")
      if isinstance(err, requests.exceptions.SSLError):
        log.error("if the mPCC's certificate is from a site or self-signed CA, set global caBundle to that CA's certificate file.")
      log.error("Communication error to the server.  Retrying.")
      self.sessionOk = False
      time.sleep(10)
      return
    self.sessionOk = True
    if self.myaddress is not None:
      if self.myaddress != '':
        self.ip_address = self.myaddress
      else:
        log.warning("No address set in config, attempting autodetection.  This may not work right...")
        # get the sock from the session
        s = socket.fromfd(response.raw.fileno(), socket.AF_INET, socket.SOCK_STREAM)
        # get the address from the socket
        sockinfo = s.getsockname()
        self.ip_address=sockinfo[0]
      
    # if we get a response.status_code == 200, we're ok.  If not,
    # our auth failed.
    return response.status_code == 200

 