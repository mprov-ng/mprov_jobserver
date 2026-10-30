# This sub-module of the repo_update job module, will sync and 
# build repos from RHEL based (yum/dnf) systems.
import os
import shlex
from urllib.parse import urlparse
from mprov_jobserver.plugins.plugin import JobServerPlugin
import json


class UpdateRepo(JobServerPlugin):
  repo = None
  repoDir = ""
  jobid = None
  def load_config(self):
    # Do a GET to see if our ostype exists, if it does, patch it, if it doesn't, post it.
    # if 'repo-server' not in self.js.jobmodules:
    #   print("Configuration Error: you MUST run repo-server on repo-upate/repo-delete nodes!")
    #   print("                   : repo-update/delete nodes are SOURCE nodes for repos!")
    #   print("                   : and need a way to serve repos! Job Module Halted!!!")
    #   sys.exit(1)
    #res = super().load_config()
    # Register our OS Type with the mPCC. ? Or should this be a fixture?
    ostypeurl = f"{self.js.mprovURL}ostypes/"
    data = { "slug": "rhel", "name": "Yum/DNF Based Linux" }
    response = self.js.session.post(ostypeurl, data=json.dumps(data))
    if response.status_code != 200:
      print("Error: Error updating OS Type for module.")
    return True


  def handle_jobs(self):
    if self.repo == None:
      print("Error: repo was empty.")
      return
    print(f"Running rhel repo update for repo {self.repo['id']}")
    repoDetails = self.repo
    print(repoDetails['repo_package_url'])
    
    # let's do some extraction
    parsed_uri = urlparse(repoDetails['repo_package_url'])
    pathDepth = parsed_uri.path.count("/") - 1

    # try to make the directory for this repo.
    os.makedirs(f"{self.repoDir}/{self.repo['id']}", exist_ok=True)

    # Don't os.chdir() into repoDir: plugins are threads of one process, so
    # that would move every other plugin's working directory too, and theirs
    # could move this reposync (#130).  reposync writes to destdir/<repo id>.
    baseURL = repoDetails['repo_package_url']
    print("Grabbing repository mirror: " + baseURL)
    #if os.system(f"wget --mirror -nH --cut-dirs={pathDepth} -np -P {self.repo['id']}/ {baseURL}" ):
    # the --installroot option here allows us to override dnf's stupid download locking and allows
    # us to parallelize reposync properly-ish.
    # the URL is a free-text field in the mPCC: quoted, it can't change the
    # command, whatever it holds (#184).  The repo id is the integer pk.
    repoFromPath = shlex.quote(f"{self.repo['id']},{self.repo['repo_package_url']}")
    if os.system(f"dnf reposync --repofrompath {repoFromPath} --repo {self.repo['id']} --destdir={self.repoDir}/ --download-metadata --installroot={self.repoDir}/{self.repo['id']}"):
      print("Error: unable to get repo: " + baseURL)
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      return
    
    # update the 'jobservers' field to be us, so that the 
    data = {
      'id': self.repo['id'],
      'hosted_by':[
        self.js.id,
      ],
    }
    print(json.dumps(data))
    response = self.js.session.patch(self.js.mprovURL + 'repos/' + str(data['id']) + '/', data=json.dumps(data))
    self.js.update_job_status(self.jobModule, 4, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
    return 