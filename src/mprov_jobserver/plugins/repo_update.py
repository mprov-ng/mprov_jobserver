import importlib
from .plugin import JobServerPlugin
import os


from os.path import dirname, basename, isfile, join
import glob
from mprov_jobserver.log import get_logger

log = get_logger(__name__)

class repo_update(JobServerPlugin):
  jobModule = 'repo-update'
  repoDir = ""
  repoList = None
  # mirror only the newest version of each package (#95).  A repo's own
  # newest_only (mprov_control_center#156) wins; this is the setting for an
  # mPCC that doesn't send one.
  newestOnly = False


  def load_config(self):
    # if 'mprov-webserver' not in self.js.jobmodules:
    #   print("Configuration Error: you MUST run mprov-webserver on repo-update/repo-delete nodes!")
    #   print("                   : repo-update/delete nodes are SOURCE nodes for repos!")
    #   print("                   : and need a way to serve repos! Job Module Halted!!!")
    #   sys.exit(1)
        # run load config on each sub_module
    modules = glob.glob(join(dirname(__file__) + "/repo_update_mod", "*.py"))
    mods = [ basename(f)[:-3] for f in modules if isfile(f) and not f.endswith('__init__.py')]
    for name in mods:
      try:
        mod = importlib.import_module(f".repo_update_mod.{name}", "mprov_jobserver.plugins")
        update_klass = getattr(mod, 'UpdateRepo')
      except BaseException as err:
        log.error(f"Unable to import Repo Update Module for {name}.")
        log.error(f"Exception: {err=}, {type(err)=}")
        continue

      try:
        repo_update = update_klass(self.js)
      except Exception as err:
        log.error(f"Unable to instantiate UpdateRepo class on {name}: {err}")
        continue
      repo_update.load_config()

    return super().load_config()

  def handle_jobs(self):
    #print("repo-update~")
    # Get the job from 
    if not self.js.update_job_status(self.jobModule, 2):
      return # no jobs
    # grab the list of repos to update.
    self.repoList = {}
    response = self.js.session.get( f"{self.js.mprovURL}jobs/?jobserver={str(self.js.id)}&module={self.jobModule}&status=2")
    # A job that fails here or below fails by its id, and the others carry
    # on: returning would leave them running (#145).
    for job in response.json():
      # get the ostype from the repo.
      
      try:
        params = job['params']
      except (KeyError, TypeError):
        log.error("Repo Update with no repo_id, cannot parse params.")
        self.js.update_job_status(self.jobModule, 3, jobid=job['id'])
        continue
      try:
        self.repoList[params['repo_id']] = job['id']
      except (KeyError, TypeError):
        log.error("Repo Update Job Failed, corrupted params")
        self.js.update_job_status(self.jobModule, 3, jobid=job['id'])
        continue
    if not os.path.exists(self.repoDir):
      try:
        os.makedirs(self.repoDir, exist_ok=True)
      except OSError:
        log.error("unable to make repodir:" + self.repoDir)
        # no repo is being synced yet, so fail every job we claimed.
        for jid in self.repoList.values():
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
        return
    # create an empty robots.txt file.
    with open(f'{self.repoDir}/robots.txt', 'w'):
      pass
    workers = []
    for repo,jid in self.repoList.items():
      # grab the repo from the mPCC
        
      
      log.info(f"Processing repo update for {repo}")
      query = f"repos/{repo}/"
      response = self.js.session.get( self.js.mprovURL + query)
      if response.status_code == 200:
        # instantiate the sub module and pass it the repo.
        try:
          repo = response.json()
        except ValueError:
          log.error("Unable to parse response.")
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          continue
        if repo['ostype'] is None:
          log.error(f"Repo {repo['id']} doesn't have an OS type set.  Exiting.")
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          continue
        try:
          log.debug(f".repo_update_mod.{repo['ostype']}")
          mod = importlib.import_module(f".repo_update_mod.{repo['ostype']}", "mprov_jobserver.plugins")
          update_klass = getattr(mod, 'UpdateRepo')
        except BaseException as err:
          log.error("Unable to import OS Repo Module.")
          log.error(f"Exception: {err=}, {type(err)=}")
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          continue
        
        try:
          repo_update = update_klass(self.js)
        except Exception as err:
          log.error(f"Unable to instantiate UpdateRepo class on {repo['ostype']}: {err}")
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          continue
        repo_update.repo = repo
        repo_update.repoDir = self.repoDir
        newest = repo.get('newest_only')
        repo_update.newestOnly = bool(self.newestOnly if newest is None else newest)
        repo_update.jobid = jid
        repo_update.start()
        workers.append(repo_update)
      else: 
        log.error(f'bad http request: {response.status_code}')
        log.debug(self.js.mprovURL + query)
        self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
        continue

    # The repos sync in parallel, but this run waits for all of them.  It
    # used to return straight away, so the next heartbeat started a new run,
    # and if that claimed a new job it listed every RUNNING repo-update job
    # on this server, the ones still syncing too, and started a second
    # reposync of each (#157).  Each worker sets its own job's status.
    for worker in workers:
      worker.join()
