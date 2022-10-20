import shutil
from threading import local
from .plugin import JobServerPlugin
import os
import json
import sys
import sh

class repo_sync(JobServerPlugin):
  jobModule = 'repo-sync'
  repoDir = ""
  repoList = None

  # override default load config because we have to check that mprov-webserver is also enabled.
  def load_config(self):
    if 'mprov-webserver' not in self.js.jobmodules:
      print("Configuration Error: you MUST run mprov-webserver on repo-sync nodes!")
      print("                   : repo-sync nodes are SOURCE nodes for repos!")
      print("                   : and need a way to serve repos! Job Module Halted!!!")
      sys.exit(1)
    return super().load_config()

  def handle_jobs(self):
    
    # repo-sync is not job based.  We will connect to the MPCC, compare the version we know
    # about for our repo, and update it if it doesn't match.

    # repos we care about: self.repoList, if populated.  All repos in MPCC if None.
    # repo versions tracked via version file in self.repoDir.

    if not os.path.exists(self.repoDir):
      # if we don't have an repo path, make one.
      os.makedirs(self.repoDir, exist_ok=True)
    
    # see if we have a repo-versions file
    if not os.path.exists(self.repoDir + '/repo-versions'):
      # no versions file.  Create a blank one right quick
      with open(self.repoDir + '/repo-versions', "w") as vfile:
        vfile.write(json.dumps({}))
    
    # now we should have a dir and a version file, lets get our list of repos we care about
    if self.repoList is None:
      self.repoList = []
      # no repoList in config, grab it from MPCC
      response = self.js.session.get( self.js.mprovURL + 'repos/')
      for repo in response.json():
        self.repoList.append(str(repo['id']))
    
    # so here we should have either a hard coded config list of repos, 
    # or a list of all repos grabbed from the server.
    # print(self.repoList)
    for repo in self.repoList: 
      # grab the MPCC version for this repo.
      # if MPCC 404's the repo was deleted.  Remove from our repoList
      # print an error, and move on.  File deletion happens below.
      print(f"Syncing repo {repo}")
      response = self.js.session.get( self.js.mprovURL + 'repos/' + str(repo) + '/')
      if(response.status_code == "404" ):
        # repo not found in the MPCC, must have been removed, delete it locally.
        self.repoList.remove(repo)
        print("Error: repo '" + repo + "' was not found on the MPCC, removing locally." )
        print("Error: Consider removing it from your config.")
        continue

      mpccVersion = response.json()['version']
      currJobServers = response.json()['hosted_by']

      with open(self.repoDir + '/repo-versions', "r") as vfile:
        # grab the version info into a var
        try:
          repoVersions = json.loads(vfile.read())
        except:
          # if we have an error loading the version data, 
          # init it with no version data, we'll rebuild it 
          # in a moment.
          repoVersions = {}
    
      # see if our repo is in repoVersions
      ourVersion = 0
      if repo in repoVersions:
        ourVersion = repoVersions[repo]
      
      # compare our version to the one the MPCC has for us.
      if ourVersion != mpccVersion:
        # Versions don't match, ask the MPCC to grab a new copy.
        os.makedirs(self.repoDir + '/' + str(repo), exist_ok=True)
        os.chdir(self.repoDir)
        # TODO: Do the wget.
        print(" ".join([
          '--mirror', 
          '--retry-on-http-error=404',
          '--tries=0',
          '--waitretry=1',
          '-nH', 
          f'--cut-dirs=1', 
          '-np', 
          f"{self.js.mprovURL}osrepos/{repo}/"
        ]))
        sh.wget([
          '--mirror', 
          '--retry-on-http-error=404',
          '--tries=0',
          '--waitretry=1',
          '-nH', 
          f'--cut-dirs=1', 
          '-np', 
          f"{self.js.mprovURL}osrepos/{repo}/"
        ])
         
        print(f"repo {repo} download complete. Updating versions")        
        # file download complete.  Update our version
        repoVersions[repo] = mpccVersion
        ourVersion = mpccVersion
        with open(self.repoDir + '/repo-versions', "w") as vfile:
          vfile.write(json.dumps(repoVersions))
      if self.js.id not in currJobServers:
        # tell the MPCC we can host this file
        print(f"Adding ourself to mPCC for repo {repo}")
        jobservers = []
        for jobserver in currJobServers:
          jobservers.append(jobserver)
        # now append our id
        jobservers.append(self.js.id)
        data = {
          'id': repo,
          'hosted_by': jobservers,
        }
        response = self.js.session.patch(self.js.mprovURL + 'repos/' + str(data['id']) + '/?addjs', data=json.dumps(data))
        print(f"repo {repo} updated.")
      

    # Clean up any directories that are not in our repoList.
    print("Scanning for hanging repos... " + self.repoDir)
    # print(self.repoList)
    for entry in os.listdir(self.repoDir):
      # print(entry)
      if os.path.isdir(self.repoDir + '/' + entry):
        # see if this entry is in our repo list
        if not os.path.basename(entry) in self.repoList:
          # Nope doesn't seem to be there, doesn't need to exist on disk then.
          #print("rm -rf " + self.repoDir + '/' + entry)
          print("Warn: Removed unknown repo directory: " + self.repoDir + '/' + entry)
          shutil.rmtree(self.repoDir + '/' + entry)

        
