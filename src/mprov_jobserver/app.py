
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
import ipaddress
import psutil
from urllib.parse import urlparse
from requests.adapters import HTTPAdapter
from mprov_jobserver.log import get_logger, configure as log_configure
from mprov_jobserver.config import merge_config
from mprov_jobserver.version import install as version_install
from mprov_jobserver import identity as ident

log = get_logger(__name__)

# interfaces whose addresses aren't ones clients reach this job server on:
# loopback, and container and VM bridges (#114)
SKIPPED_INTERFACES = ("lo", "docker", "veth", "br-", "virbr", "podman", "cni")
# seconds to wait for an address's heartbeat: a network that drops packets
# mustn't hold up the job server's main loop (#114)
ADDRESS_HEARTBEAT_TIMEOUT = 5
# how often to check whether this job server's certificate is due for
# renewal (mprov_control_center#381)
RENEW_CHECK_EVERY = 3600


def interface_addresses():
  """
  The addresses on this host's interfaces that are up, for the mPCC to
  send clients on each network to the one on theirs (#114): IPv4 and
  global-scope IPv6, without loopback, link-local or container bridges.
  """
  stats = psutil.net_if_stats()
  result = []
  for name, addrs in psutil.net_if_addrs().items():
    if name.startswith(SKIPPED_INTERFACES) or name not in stats or not stats[name].isup:
      continue
    for addr in addrs:
      if addr.family not in (socket.AF_INET, socket.AF_INET6):
        continue
      try:
        ip = ipaddress.ip_address(addr.address.split('%', 1)[0])
      except ValueError:
        continue
      if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        continue
      if str(ip) not in result:
        result.append(str(ip))
  return result


def address_family(address):
  """ The socket family of an address, or None if it isn't one. """
  try:
    version = ipaddress.ip_address(address.split('%', 1)[0]).version
  except ValueError:
    return None
  return socket.AF_INET6 if version == 6 else socket.AF_INET


def url_families(url):
  """
  The address families the mPCC at url can be reached over (#292).  None
  if that can't be told, e.g. its name doesn't resolve: an address is then
  given the benefit of the doubt rather than dropped on a passing DNS
  failure.
  """
  host = urlparse(url).hostname
  if not host:
    return None
  family = address_family(host)
  if family is not None:
    return {family}
  try:
    return {info[0] for info in socket.getaddrinfo(host, None)}
  except socket.gaierror:
    return None


class SourceAddressAdapter(HTTPAdapter):
  """ Sends requests from one local address, so a per-address heartbeat
  goes out on that address's network (#114). """

  def __init__(self, source, **kwargs):
    self.source = source
    super().__init__(**kwargs)

  def init_poolmanager(self, *args, **kwargs):
    kwargs['source_address'] = (self.source, 0)
    super().init_poolmanager(*args, **kwargs)


def job_subject(job):
  """ What a job is about, from its params: an image or repo id, or "". """
  params = job.get('params') if isinstance(job, dict) else None
  if isinstance(params, dict):
    for key in ('imageId', 'repo_id'):
      if params.get(key) not in (None, ""):
        return f" {params[key]}"
  return ""


def log_job(job_module, job, status, orphaned=False):
  """
  One line per job transition, in a fixed form a log search (Loki, Grafana)
  can match: "job 42 image-update compute started", then "succeeded" or
  "failed" with how long it ran, from the job's start_time (#103).

  An orphan is a job nobody watched finish, so there is no run time to
  report: the gap from its start_time to now covers however long this job
  server was down or hung as well.  Those say "last seen RUNNING 25927s
  ago" instead, so a reader -- or a dashboard built on this format -- can
  tell the two apart rather than reading downtime as a 7-hour build (#305).
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
    since = int((datetime.now(timezone.utc) - started).total_seconds())
    took = f" (last seen RUNNING {since}s ago)" if orphaned else f" ({since}s)"
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
  # the shared API key: the fallback for a job server with no certificate
  # and no enrollment token, until 0.8.0 (mprov_control_center#381)
  apikey = ""
  # CA bundle for HTTPS to the mPCC; unset, the OS bundle is used (#102),
  # or, once enrolled, the CA the mPCC sent with the certificate
  caBundle = ""
  # this job server's own key and certificate (mprov_control_center#381),
  # in jobserver.pem and the mPCC's CA in ca.crt.  Unset: MPROV_TLS_DIR,
  # else /var/lib/mprov/tls.
  tlsDir = ""
  # the one-time token to enroll with: unset, MPROV_ENROLL_TOKEN, else the
  # first line of enrollTokenFile (unset, enroll_token next to the config
  # file, where the mPCC in the same compose stack writes one)
  enrollToken = ""
  enrollTokenFile = ""
  # the SHA-256 fingerprint of the mPCC's CA, to check it by before the
  # token is sent; unset, MPROV_CA_FINGERPRINT.  Not needed with caBundle,
  # or with the mPCC's ca.crt next to the config file.
  caFingerprint = ""
  # where a booted node's own key and certificate are (the mPCC's boot
  # scripts enroll it): what a script runner on a node authenticates with.
  # Unset, /etc/mprov/tls.
  nodeTlsDir = ""
  # how this job server authenticates: 'cert', 'apikey' or None (not yet)
  auth = None
  _caBundleSet = False
  _certFingerprint = None
  # the certificate the mPCC refused (revoked, expired, its job server
  # deleted) and the token it refused, so neither is tried again
  _refusedCert = None
  _refusedToken = None
  _identityProblem = None
  # the certificate, key and CA files authenticating with, for the scripts
  # the script runner runs (entity.json's tls)
  tlsPaths = None
  _sharedKeyRefused = False
  _nextRenewCheck = 0.0
  # this host's stable id for the mPCC; unset, a random one is kept in
  # host-id next to the config file (#113)
  hostId = ""
  _hostIdValue = None
  # the addresses this job server serves clients on, one per network, each
  # heartbeated to the mPCC on its own (#114).  Unset, they're found from
  # the interfaces that are up; [] registers none.
  addresses = None
  addressEntries = None
  # per-address sessions, bound to that address, and the addresses whose
  # last heartbeat failed, so a failure is logged once
  _addressSessions = None
  _failingAddresses = None
  # addresses left unregistered because they can't reach their mPCC, so
  # that's said once instead of on every heartbeat (#292)
  _unreachableAddresses = None
  # job modules whose last call to the mPCC couldn't reach it, so a module
  # says so once while the mPCC is down, not on every run (#337)
  _unreachableModules = None
  # why the last attempt to start a session failed, so a job server left
  # pointing at an mPCC it can't use says so once instead of every retry
  _sessionProblem = None
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

    # Authenticate to the Control Center and start a session
    log.info("mProv Job Server authenticating.")
    loggedIn = self.startSession()
    if not loggedIn:
        log.error("Unable to log into mProv Control Center.")


    # Load Plugins. (plugins register job modules.)
    self.load_plugins()

    # register the server, once we can actually talk to the mPCC.
    # Registering over a session that didn't start leaves a job server row
    # on the mPCC that this one never uses: it either never sees the reply
    # or can't read an id out of it.  The main loop registers as soon as a
    # session does start (#293).
    if loggedIn:
      self.register_server()

    pass

  def yaml_include(self, loader, node):
      # Get the path out of the yaml file
    file_name = os.path.join(os.path.dirname(loader.name), node.value)
    
    # we have a glob, so we will iterate.  sorted() so that, when two files
    # set the same global setting, which one wins doesn't depend on the
    # directory order.
    documents = []
    for file in sorted(glob.glob(file_name)):
      with open(file) as inputfile:
        documents.append(yaml.load(inputfile, Loader=yaml.FullLoader)[0])
    return merge_config(documents)



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

    # flatten the config space; `global:` is merged across documents, so a
    # plugin file can add global settings (#301)
    self.config_data = merge_config(self.config_data)

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
      except AttributeError:
        log.error("" + config_entry + " is not a valid config entry in 'global'.")
        sys.exit(1)
      except Exception as err:
        log.error(f"invalid value for {config_entry} in 'global': {err}")
        sys.exit(1)
    try:
      self.addressEntries = self.parse_addresses(self.addresses)
    except ValueError as err:
      log.error(f"invalid addresses in 'global': {err}")
      sys.exit(1)
    self.tlsDir = self.tlsDir or os.environ.get('MPROV_TLS_DIR') or ident.DEFAULT_TLS_DIR
    self.enrollToken = self.enrollToken or os.environ.get('MPROV_ENROLL_TOKEN') or ""
    self.enrollTokenFile = self.enrollTokenFile or self.beside_config('enroll_token')
    self.caFingerprint = self.caFingerprint or os.environ.get('MPROV_CA_FINGERPRINT') or ""
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
          self.maybe_renew()
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


  
  def update_job_status(self, job_module, status, jobid=None,jobquery="",orphaned=False):
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
    try:
      response = self.session.get(queryURL)
    except requests.RequestException as err:
      # e.g. the mPCC restarting: no jobs this run, rather than a traceback
      # that ends the module's thread (#337)
      self.mpcc_unreachable(job_module, err)
      return False
    self.mpcc_reachable(job_module)
    if response.status_code <= 199 or response.status_code >=300:

        log.error(f"Server returned error {response.status_code}. ")
        return False
    try:
      if (response.json() == [] ):
        return False
    except ValueError:
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

      if status == 2 and 'job-claim' in getattr(self, 'features', ()):
        # claim it atomically: of job servers claiming the same pending job,
        # the mPCC lets exactly one win (mprov_control_center#296).  The PATCH
        # below was last-writer-wins, so two job servers could both run it
        # (#112).
        if self.claim_job(job_module, job):
          updateCount += 1
        continue

      data['id'] = job['id']
      data['jobserver'] = self.id
      # print(data)
      try:
        response = self.session.patch(self.mprovURL + 'jobs/' + str(data['id']) + '/', data=json.dumps(data))
      except requests.RequestException as err:
        self.mpcc_unreachable(job_module, err)
        continue
      updateCount += 1

      if response.status_code == 400:
        log.error("the mPCC refused the update of job " + str(data['id']) + ":")
        log.error(response.json())
        continue
      log_job(job_module, job, status, orphaned=orphaned)

    return updateCount
    # return True

    #print(job_module + " " + str(status))
    pass

  def mpcc_unreachable(self, job_module, err):
    '''
    A job module couldn't reach the mPCC (#337).  Said once per module
    while it's down: the module runs again on every heartbeat, and the main
    loop already says when the mPCC itself is unreachable.
    '''
    if self._unreachableModules is None:
      self._unreachableModules = set()
    if job_module in self._unreachableModules:
      log.debug(f"{job_module}: still unable to reach the mPCC: {err}")
      return
    self._unreachableModules.add(job_module)
    log.error(f"{job_module}: unable to reach the mPCC: {err}; trying again on its next run.")

  def mpcc_reachable(self, job_module):
    ''' A job module reached the mPCC again. '''
    if self._unreachableModules and job_module in self._unreachableModules:
      self._unreachableModules.discard(job_module)
      log.info(f"{job_module}: reached the mPCC again.")

  def claim_job(self, job_module, job):
    '''
    Claim a pending job for this job server with POST jobs/<id>/claim/.
    The mPCC sets it RUNNING with our id and its start time in the same
    step.  Returns True if we got it, False if another job server did, it
    isn't pending any more, or the claim failed.
    '''
    try:
      response = self.session.post(self.mprovURL + 'jobs/' + str(job['id']) + '/claim/', data=json.dumps({'jobserver': self.id}))
    except requests.RequestException as err:
      self.mpcc_unreachable(job_module, err)
      return False
    if response.status_code == 200:
      log_job(job_module, job, 2)
      return True
    if response.status_code == 409:
      # someone else has it, or it moved on: nothing for us to do
      log.debug(f"job {job['id']} {job_module} was claimed elsewhere or isn't pending; skipping it.")
      return False
    try:
      detail = response.json()
    except ValueError:
      detail = response.text
    log.error(f"the mPCC refused our claim of job {job['id']} {job_module} (HTTP {response.status_code}): {detail}")
    return False

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
      self.update_job_status(job.get('module', ''), 3, jobid=job['id'], orphaned=True)
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
    # setup or server info register payload
    data = {
        'name': self.hostname(),
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
    # an mPCC only lets a certificate register its own host id, so it's
    # always sent then (mprov_control_center#381)
    if 'host-id' in self.features or self.auth == 'cert':
      hostId = self.host_id()
      if hostId:
        data['host_id'] = hostId
    # our address on each network we serve, if the mPCC can take them and
    # keep each one fresh by its own heartbeat (#114)
    entries = None
    if {'jobserver-addresses', 'jobserver-address-heartbeat'} <= self.features:
      entries = self.address_entries()
      data['addresses'] = [{k: entry[k] for k in ('address', 'network') if k in entry} for entry in entries]
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
      
    except Exception as err:
      log.error(f"heartbeat to the mPCC failed: {err}")
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
    if entries and 'jobserver-address-heartbeat' in self.features:
      self.heartbeat_addresses(entries)

  def parse_addresses(self, value):
    '''
    The global addresses list (#114) as [{"address", "network"?, "mprovURL"}],
    or None if it isn't set.  An entry is an address, or a mapping with
    "address", optionally the mPCC "network" id it's on (else the mPCC finds
    it from the subnets) and the "mprovURL" that reaches the mPCC from that
    network (else the global mprovURL).  Raises ValueError if it's malformed.
    '''
    if value is None:
      return None
    if not isinstance(value, list):
      raise ValueError("must be a list of addresses")
    entries = []
    for item in value:
      if isinstance(item, str):
        item = {'address': item}
      if not isinstance(item, dict) or not isinstance(item.get('address'), str) or not item['address'].strip():
        raise ValueError(f"each entry needs an address, not {item!r}")
      unknown = set(item) - {'address', 'network', 'mprovURL'}
      if unknown:
        raise ValueError(f"unknown key(s) {', '.join(sorted(unknown))} for {item['address']}")
      entry = {'address': item['address'].strip()}
      if item.get('network') is not None:
        entry['network'] = item['network']
      url = item.get('mprovURL') or self.mprovURL
      if not isinstance(url, str):
        raise ValueError(f"mprovURL for {entry['address']} must be a URL")
      entry['mprovURL'] = url if url.endswith('/') else url + '/'
      if entry['address'] in (e['address'] for e in entries):
        raise ValueError(f"{entry['address']} is listed twice")
      entries.append(entry)
    return entries

  def address_entries(self):
    ''' The addresses to register (#114): the configured ones, else those
    on the interfaces that are up, found again on each heartbeat.  Only
    the ones that can reach the mPCC they'd register with (#292). '''
    if self.addressEntries is not None:
      entries = self.addressEntries
    else:
      entries = [{'address': address, 'mprovURL': self.mprovURL} for address in interface_addresses()]
    return [entry for entry in entries if self.reaches_mpcc(entry)]

  def reaches_mpcc(self, entry):
    '''
    Whether a heartbeat sent from this address could arrive at the mPCC it
    would go to.  An address of a family that mPCC's URL doesn't have
    can't: binding to it gives "Address family for hostname not supported"
    every time (#292).  Registering it anyway would have the mPCC send
    clients to an address this job server has never been heard from on,
    which is what the per-address heartbeat exists to prevent (#114), so
    it's left out instead.
    '''
    if self._unreachableAddresses is None:
      self._unreachableAddresses = set()
    address = entry['address']
    url = entry.get('mprovURL') or self.mprovURL
    family = address_family(address)
    families = url_families(url)
    # a host name is never bound to a source address anyway (address_session
    # falls back to the main session), and an mPCC we can't resolve tells us
    # nothing, so neither is a reason to drop the address
    reachable = family is None or families is None or family in families
    if reachable:
      if address in self._unreachableAddresses:
        self._unreachableAddresses.discard(address)
        log.info(f"{address} can reach the mPCC at {url} again; registering it.")
    elif address not in self._unreachableAddresses:
      self._unreachableAddresses.add(address)
      log.warning(
        f"not registering {address}: the mPCC at {url} has no "
        f"{'IPv6' if family == socket.AF_INET6 else 'IPv4'} address, so a heartbeat "
        f"from it can't reach the mPCC and clients couldn't be sent to it."
      )
    return reachable

  def address_session(self, address):
    '''
    A session that sends from address, so its heartbeat goes out on that
    address's network (#114), sharing the main session's headers, client
    certificate and certificate checks.  A host name can't be bound: the main
    session is used.
    '''
    try:
      ipaddress.ip_address(address)
    except ValueError:
      return self.session
    if self._addressSessions is None:
      self._addressSessions = {}
    if address not in self._addressSessions:
      session = requests.Session()
      session.headers = self.session.headers
      session.verify = self.session.verify
      # this job server's certificate goes along too (mprov_control_center#381)
      session.cert = getattr(self.session, 'cert', None)
      session.hooks['response'].append(self.auth_refused)
      # hooks aren't copied with the headers, so the version check has to
      # be put on this session too (#287)
      version_install(session, enforce=self.register)
      adapter = SourceAddressAdapter(address)
      session.mount('http://', adapter)
      session.mount('https://', adapter)
      self._addressSessions[address] = session
    return self._addressSessions[address]

  def heartbeat_addresses(self, entries):
    '''
    Heartbeat each registered address on its own, from that address and to
    its mprovURL (#114), so the mPCC stops sending clients to an address
    whose network can't reach us.  A failure is logged once, until that
    address heartbeats again.
    '''
    if self._failingAddresses is None:
      self._failingAddresses = set()
    for entry in entries:
      address = entry['address']
      url = entry['mprovURL'] + 'jobservers/' + str(self.id) + '/heartbeat/'
      problem = None
      try:
        response = self.address_session(address).post(url, data=json.dumps({'address': address}), timeout=ADDRESS_HEARTBEAT_TIMEOUT)
        if response.status_code != 200:
          problem = f"the mPCC answered {response.status_code}: {response.text[:200]}"
      except Exception as err:
        problem = f"unable to reach the mPCC at {entry['mprovURL']} from it: {err}"
      if problem is None:
        if address in self._failingAddresses:
          self._failingAddresses.discard(address)
          log.info(f"heartbeat for {address} is back.")
      elif address not in self._failingAddresses:
        self._failingAddresses.add(address)
        log.warning(f"heartbeat for {address} failed, so the mPCC will stop sending clients to it: {problem}")


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

  def hostname(self):
    ''' This host's name without its domain, as the mPCC knows it. '''
    return platform.node().split(".", 1)[0]

  def beside_config(self, name):
    ''' The path of name in the config file's directory (/etc/mprov). '''
    return os.path.join(os.path.dirname(os.path.abspath(self.configfile)), name)

  def identity_problem(self, problem):
    ''' Log why this job server can't authenticate, once, not on every retry. '''
    if problem != self._identityProblem:
      self._identityProblem = problem
      log.error(problem)

  def use_identity(self):
    '''
    Set the session up to authenticate (mprov_control_center#381), with,
    in order: this job server's certificate; a new one, enrolled with a
    token; the shared API key (deprecated, until 0.8.0).  False if there's
    none of them yet, which is said once.  A job server that doesn't
    register (-d, -m) never enrolls, but uses a certificate there already
    is.
    '''
    identity = ident.Identity(self.tlsDir)
    cert = identity.usable()
    if cert is not None and ident.fingerprint(cert) == self._refusedCert:
      cert = None
    if cert is not None and not self.mprovURL.startswith('https://'):
      self.identity_problem(f"this job server's certificate can only be used over HTTPS; "
                            f"mprovURL is {self.mprovURL}.  Set MPROV_URL to https://.")
      cert = None
    elif cert is None and self.register:
      cert = self.enroll(identity)
    if cert is not None:
      self.use_certificate(identity, cert)
      return True
    if not self.register:
      node = ident.node_credential(self.nodeTlsDir)
      if node is not None and ident.fingerprint(node.cert) != self._refusedCert:
        self.use_node_certificate(node)
        return True
    if self.apikey or not self.register:
      self.use_apikey()
      return True
    # with a token, enroll() has said why it didn't work
    if ident.token_from(self.enrollToken, self.enrollTokenFile) is None:
      self.identity_problem(
        "this job server has no certificate and nothing to enroll with: set MPROV_ENROLL_TOKEN (or global "
        f"enrollToken) to an enrollment token from the mPCC, or put one in {self.enrollTokenFile}.  "
        "Will keep trying.")
    return False

  def use_apikey(self):
    ''' Authenticate with the shared API key: the fallback until 0.8.0. '''
    if self.auth != 'apikey' and self.register:
      log.warning("authenticating with the shared API key, which is deprecated: an mPCC with "
                  "MPROV_SHARED_KEY_MODE=off refuses it, and 0.8.0 drops it.  Enroll this job server "
                  "with a token from the mPCC instead (MPROV_ENROLL_TOKEN).")
    self.auth = 'apikey'
    self.tlsPaths = None
    self.session.cert = None
    # a script runner may have none (the nads image): then it sends no
    # Authorization at all, for the endpoints that don't need one
    if self.apikey:
      self.session.headers['Authorization'] = 'Api-Key ' + self.apikey
    else:
      self.session.headers.pop('Authorization', None)
    self._addressSessions = None

  def use_certificate(self, identity, cert):
    ''' Authenticate with cert, the certificate in identity. '''
    fp = ident.fingerprint(cert)
    if self.auth != 'cert' or self._certFingerprint != fp:
      log.info(f"authenticating with this job server's certificate, valid until "
               f"{cert.not_valid_after_utc:%Y-%m-%d} UTC.")
    self.auth = 'cert'
    self._certFingerprint = fp
    self._identityProblem = None
    self.session.cert = identity.credential_path
    self.tlsPaths = {'cert': identity.credential_path, 'key': identity.credential_path, 'ca': identity.ca_path}
    if not self._caBundleSet and os.path.isfile(identity.ca_path):
      self.session.verify = identity.ca_path
    self.session.headers.pop('Authorization', None)
    # rebuilt with the new certificate when next used
    self._addressSessions = None

  def use_node_certificate(self, node):
    '''
    Authenticate with this node's own certificate, over HTTPS to the same
    host, as the mPCC's boot scripts do: a node's configuration has the
    plain-HTTP URL it boots from (mprov_control_center#381).
    '''
    fp = ident.fingerprint(node.cert)
    if self.auth != 'cert' or self._certFingerprint != fp:
      log.info(f"authenticating with this node's certificate, valid until {node.cert.not_valid_after_utc:%Y-%m-%d} UTC.")
    self.auth = 'cert'
    self._certFingerprint = fp
    self._identityProblem = None
    self.tlsPaths = {'cert': node.cert_path, 'key': node.key_path, 'ca': node.ca_path}
    self.mprovURL = ident.https_url(self.mprovURL)
    self.session.cert = (node.cert_path, node.key_path)
    if not self._caBundleSet:
      self.session.verify = node.ca_path
    self.session.headers.pop('Authorization', None)
    self._addressSessions = None

  def enrollment_trust(self, identity):
    '''
    What to check the mPCC's certificate against before sending it the
    token: caBundle, the CA whose fingerprint is configured, or the mPCC's
    ca.crt next to the config file.  Never nothing: the token is a secret.
    Raises ident.EnrollError if there's no way to check.
    '''
    if self._caBundleSet:
      return self.caBundle
    if self.caFingerprint:
      identity.save_ca(ident.fetch_ca(self.mprovURL, self.caFingerprint))
      return identity.ca_path
    beside = self.beside_config(ident.CA_CERT)
    if os.path.isfile(beside):
      return beside
    raise ident.EnrollError(
      "can't check the mPCC's certificate, so the enrollment token isn't sent: set MPROV_CA_FINGERPRINT "
      "(or global caFingerprint) to the CA fingerprint the mPCC shows with the token, set caBundle, or "
      f"put the mPCC's ca.crt in {beside}.", retry=True)

  def enroll(self, identity):
    ''' A new certificate, enrolled with the token; None if there's no
    token, or enrolling failed (which is said once). '''
    token = ident.token_from(self.enrollToken, self.enrollTokenFile)
    if token is None or ident.token_id(token) == self._refusedToken:
      return None
    if not self.mprovURL.startswith('https://'):
      self.identity_problem(f"enrolling needs HTTPS, and mprovURL is {self.mprovURL}.  Set MPROV_URL to https://.")
      return None
    hostId = self.host_id()
    if not hostId:
      self.identity_problem("this job server can't enroll without a host id (see above).")
      return None
    try:
      verify = self.enrollment_trust(identity)
      key_pem, cert_pem, ca_pem = ident.enroll(self.mprovURL, verify, token, hostId, self.hostname())
      identity.save(key_pem, cert_pem, ca_pem)
    except ident.EnrollError as err:
      if not err.retry:
        self._refusedToken = ident.token_id(token)
      self.identity_problem(f"unable to enroll with the mPCC: {err}")
      return None
    except OSError as err:
      self.identity_problem(f"unable to save this job server's certificate in {identity.tls_dir}: {err}")
      return None
    cert = identity.usable()
    if cert is not None:
      self._refusedCert = None
      log.info(f"enrolled with the mPCC at {self.mprovURL} as {self.hostname()} ({hostId}).")
    return cert

  def auth_refused(self, response, *args, **kwargs):
    '''
    A session hook for the mPCC refusing how this job server authenticates
    (mprov_control_center#381):
    - its certificate (401, revoked, expired, or its job server deleted):
      stop using it, and look for a token to enroll again with when the
      session restarts;
    - the shared API key (403, an mPCC with MPROV_SHARED_KEY_MODE=off): try
      to enroll at the next heartbeat instead of within the hour.
    '''
    if (response.status_code == 401 and self.auth == 'cert'
        and 'TLS-Client-Certificate' in response.headers.get('WWW-Authenticate', '')):
      if self._refusedCert != self._certFingerprint:
        log.error("the mPCC refused this job server's certificate: it was revoked or has expired, or the job "
                  "server was deleted.  It needs a new enrollment token (MPROV_ENROLL_TOKEN).")
      self._refusedCert = self._certFingerprint
      self.auth = None
      self.sessionOk = False
    elif (response.status_code == 403 and self.auth == 'apikey'
          and "shared API key isn't accepted" in (response.text or '')):
      if not self._sharedKeyRefused:
        self._sharedKeyRefused = True
        log.error("the mPCC no longer accepts the shared API key: this job server needs an enrollment token "
                  "(MPROV_ENROLL_TOKEN, or the mPCC's enroll_token).")
      self._nextRenewCheck = 0.0
    return response

  def maybe_renew(self):
    '''
    Checked at most once an hour: renew this job server's certificate in
    its renewal window, or, for one still on the shared API key, enroll if
    it now can (a token, or the mPCC's CA to check it against, turned up).
    '''
    if self.auth not in ('cert', 'apikey'):
      return
    now = time.monotonic()
    if now < self._nextRenewCheck:
      return
    self._nextRenewCheck = now + RENEW_CHECK_EVERY
    identity = ident.Identity(self.tlsDir)
    if self.auth == 'apikey':
      cert = self.enroll(identity) if self.register else None
      if cert is not None:
        self.use_certificate(identity, cert)
      return
    cert = identity.usable()
    if cert is None or not identity.due(cert):
      return
    try:
      issued = ident.renew(self.session, self.mprovURL, self.hostname())
      if issued is None:
        log.debug("the mPCC says this job server's certificate isn't due for renewal yet.")
        return
      identity.save(*issued)
    except (ident.EnrollError, OSError) as err:
      log.warning(f"unable to renew this job server's certificate (valid until "
                  f"{cert.not_valid_after_utc:%Y-%m-%d} UTC): {err}.  Will try again in an hour.")
      return
    renewed = identity.usable()
    if renewed is not None:
      log.info("renewed this job server's certificate.")
      self.use_certificate(identity, renewed)

  def use_ca_bundle(self):
    # verify the mPCC's certificate against the configured caBundle or the
    # OS bundle (#102).  An admin's REQUESTS_CA_BUNDLE still wins, as it
    # does for any requests program.
    self._caBundleSet = bool(self.caBundle)
    try:
      self.caBundle = ca_bundle(self.caBundle)
    except FileNotFoundError as err:
      log.error(f"{err}; set global caBundle to the mPCC's CA certificate file.")
      sys.exit(1)
    if self.caBundle:
      self.session.verify = self.caBundle

  def startSession(self):
    if not self.use_identity():
      self.sessionOk = False
      time.sleep(10)
      return False
    self.session.headers.update({'Content-Type': 'application/json'})
    if self.auth_refused not in self.session.hooks['response']:
      self.session.hooks['response'].append(self.auth_refused)
    # A script runner (-d, and -m) is a short-lived client of whatever mPCC
    # the site has, not a job server registering with it, so a version
    # mismatch is a warning there rather than a refusal: refusing hangs it
    # instead of failing it (#303).
    version_install(self.session, enforce=self.register)

    # test connectivity
    try:
      response = self.session.get(self.mprovURL, stream=True)
    except Exception as err:
      # say why: "Communication error" alone hid certificate problems (#102)
      problem = f"unable to reach the mPCC at {self.mprovURL}: {err}"
      # the retry is every few seconds and the cause rarely changes, so say
      # it once rather than filling the log with it (#293).  A different
      # failure is still reported: a certificate error that becomes a
      # version mismatch is news.
      if problem != self._sessionProblem:
        self._sessionProblem = problem
        log.error(problem)
        if isinstance(err, requests.exceptions.SSLError):
          log.error("if the mPCC's certificate is from a site or self-signed CA, set global caBundle to that CA's certificate file.")
        log.error("Communication error to the server.  Retrying.")
      self.sessionOk = False
      time.sleep(10)
      return
    if self._sessionProblem is not None:
      self._sessionProblem = None
      log.info(f"the mPCC at {self.mprovURL} is reachable again.")
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

 