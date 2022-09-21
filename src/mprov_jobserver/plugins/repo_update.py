import importlib
from urllib import response
from urllib.parse import urlparse
from .plugin import JobServerPlugin
import sys
import os


from os.path import dirname, basename, isfile, join
import glob

class repo_update(JobServerPlugin):
  jobModule = 'repo-update'
  repoDir = ""
  repoList = None
  pass


  def load_config(self):
    # if 'mprov-webserver' not in self.js.jobmodules:
    #   print("Configuration Error: you MUST run mprov-webserver on repo-upate/repo-delete nodes!")
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
        print(f"Error: Unable to import Repo Update Module for {name}.")
        print(f"Exception: {err=}, {type(err)=}")
        continue

      try:
        repo_update = update_klass(self.js)
      except:
        print(f"Error: Unable to instantiate UpdateRepo class on {name}")
        continue
      repo_update.load_config()

    return super().load_config()

  def handle_jobs(self):
    #print("repo-update~")
    # Get the job from 
    if not self.js.update_job_status(self.jobModule, 2):
      return # no jobs
    # grab the list of repos to update.
    self.repoList = []
    response = self.js.session.get( f"{self.js.mprovURL}jobs/?jobserver={str(self.js.id)}&module={self.jobModule}&status=2")
    for job in response.json():
      # get the ostype from the repo.
      
      try:
        params = job['params']
      except:
        print("Error: Repo Update with no repo_id, cannot parse params.")
        self.js.update_job_status(self.module, 3, jobid=job['id'])
        return
      try:
        self.repoList.append(params['repo_id'])
      except:
        print("Error: Repo Update Job Failed, corrupted params")
        self.js.update_job_status(self.module, 3, jobid=job['id'])
        return
    if not os.path.exists(self.repoDir):
      try:
        os.makedirs(self.repoDir, exist_ok=True)
      except:
        print("Error: unable to make repodir:" + self.repoDir)
        self.js.update_job_status(self.module, 3, jobquery='jobserver=' + self(self.js.id) + "&status=2")
    
    for repo in self.repoList:
      # grab the repo from the mPCC
        
      
      print(f"Processing repo update for {repo}")
      query = f"repos/{repo}/"
      response = self.js.session.get( self.js.mprovURL + query)
      if response.status_code == 200:
        # instantiate the sub module and pass it the repo.
        try:
          repo = response.json()
        except:
          print("Error: Unable to parse response.")
          continue
        
        try:
          print(f".repo_update_mod.{repo['ostype']}")
          mod = importlib.import_module(f".repo_update_mod.{repo['ostype']}", "mprov_jobserver.plugins")
          update_klass = getattr(mod, 'UpdateRepo')
        except BaseException as err:
          print("Error: Unable to import OS Repo Module.")
          print(f"Exception: {err=}, {type(err)=}")
          continue
        
        try:
          repo_update = update_klass(self.js)
        except:
          print(f"Error: Unable to instantiate UpdateRepo class on {repo['ostype']}")
          continue
        repo_update.repo = repo
        repo_update.repoDir = self.repoDir
        repo_update.start()
      else: 
        print(f'Error: bad http request: {response.status_code}')
        print(self.js.mprovURL + query)
        self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
        continue