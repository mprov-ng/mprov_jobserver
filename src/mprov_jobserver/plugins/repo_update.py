from urllib import response
from urllib.parse import urlparse
from .plugin import JobServerPlugin
import sys
import os
class repo_update(JobServerPlugin):
  jobModule = 'repo-update'
  repoDir = ""
  repoList = None
  pass


  def load_config(self):
    # if 'repo-server' not in self.js.jobmodules:
    #   print("Configuration Error: you MUST run repo-server on repo-upate/repo-delete nodes!")
    #   print("                   : repo-update/delete nodes are SOURCE nodes for repos!")
    #   print("                   : and need a way to serve repos! Job Module Halted!!!")
    #   sys.exit(1)
    return super().load_config()

  def handle_jobs(self):
    #print("repo-update")
    # TODO: Get the job from 
    if not self.js.update_job_status(self.jobModule, 2):
      return # no jobs
    # grab the list of repos to update.
    self.repoList = []
    response = self.js.session.get( f"{self.js.mprovURL}jobs/?jobserver={str(self.js.id)}&module={self.jobModule}&status=2")
    for job in response.json():
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
        self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + self(self.js.id) + "&status=2")
    
    for repo in self.repoList:
      print(f"Processing repo update for {repo}")
      query = f"repos/{repo}/"
      response = self.js.session.get( self.js.mprovURL + query)
      if response.status_code == 200:
        repoDetails = response.json()
        print(repoDetails['repo_package_url'])
        
        # let's do some extraction
        parsed_uri = urlparse(repoDetails['repo_package_url'])
        pathDepth = parsed_uri.path.count("/") - 1

        # try to make the directory for this repo.
        os.makedirs(f"{self.repoDir}/{repo}", exist_ok=True)

        os.chdir(f"{self.repoDir}/")
        baseURL = repoDetails['repo_package_url']
        print("Grabbing repository mirror: " + baseURL)
        if os.system(f'wget --mirror -nH --cut-dirs={pathDepth} -np -P {repo}/ {baseURL}' ):
          print("Error: unable to get repo: " + baseURL)
          self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          return
      else: 
        print(f'Error: bad http request: {response.status_code}')
        print(self.js.mprovURL + query)
        self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
        return
    return 