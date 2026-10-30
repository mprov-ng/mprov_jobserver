import shutil
from .plugin import JobServerPlugin, path_is_under, mark_mprov_dir, is_mprov_marked_dir
import os
import json
import sys
import sh
from mprov_jobserver.log import get_logger

log = get_logger(__name__)

class repo_sync(JobServerPlugin):
  jobModule = 'repo-sync'
  repoDir = ""
  repoList = None
  # The declared mprov data root; repoDir must sit inside it so the
  # end-of-sync sweep can only ever touch mprov-owned space (#171).
  dataRoot = None

  # override default load config because we have to check that mprov-webserver is also enabled.
  def load_config(self):
    if 'mprov-webserver' not in self.js.jobmodules:
      log.error("Configuration error: you MUST run mprov-webserver on repo-sync nodes!")
      log.error("repo-sync nodes are SOURCE nodes for repos!")
      log.error("and need a way to serve repos! Job Module Halted!!!")
      sys.exit(1)
    if not super().load_config():
      return False
    # A configured repoList is compared with directory names, so make it
    # strings: YAML reads `repoList: [4, 5]` as ints, and the sweep below then
    # deleted (and the next pass re-fetched) every repo on the list (#235).
    if self.repoList is not None:
      if not isinstance(self.repoList, list):
        self.repoList = [self.repoList]
      self.repoList = [str(entry) for entry in self.repoList]
    # The sweep at the end of handle_jobs deletes directories under
    # repoDir, so refuse to start unless repoDir sits inside a data
    # root the operator declared (#171).
    if not self.dataRoot or not path_is_under(self.repoDir, self.dataRoot):
      log.error("Configuration error: repo-sync repoDir '" + str(self.repoDir) + "' must be inside dataRoot '" + str(self.dataRoot) + "'.")
      log.error("Refusing to run: the end-of-sync sweep deletes directories under repoDir.")
      sys.exit(1)
    return True

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
    # over a copy: a deleted repo is removed from the list as we go, which
    # skipped the next one (#141)
    for repo in list(self.repoList):
      # grab the MPCC version for this repo.
      # if MPCC 404's the repo was deleted.  Remove from our repoList
      # print an error, and move on.  File deletion happens below.
      log.info(f"Syncing repo {repo}")
      response = self.js.session.get( self.js.mprovURL + 'repos/' + str(repo) + '/')
      # an int: comparing with the string "404" never matched, so a deleted
      # repo's null reply crashed the whole pass (#141)
      if response.status_code == 404:
        # repo not found in the MPCC, must have been removed, delete it locally.
        self.repoList.remove(repo)
        log.error(f"repo '{repo}' was not found on the MPCC, removing locally.")
        log.error("Consider removing it from your config.")
        continue
      if response.status_code != 200:
        # anything else may be passing; try this repo again next pass.
        log.error(f"the mPCC answered {response.status_code} for repo {repo}, skipping it this pass.")
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
        # mark it as mprov-owned so the sweep below may clean it up (#171)
        mark_mprov_dir(self.repoDir + '/' + str(repo))
        # Mirror into repoDir with -P instead of os.chdir() into it: plugins
        # are threads of one process, so a chdir would move every other
        # plugin's working directory too, and theirs could move this mirror
        # (#131).  -nH and --cut-dirs=1 drop the host and osrepos/, so the
        # files land in repoDir/<repo>/.
        wgetArgs = [
          '--mirror', 
          '--retry-on-http-error=404',
          '--tries=0',
          '--waitretry=1',
          '-nH', 
          f'--cut-dirs=1', 
          '-np', 
          '-P', self.repoDir,
          f"{self.js.mprovURL}osrepos/{repo}/"
        ]
        # the CA the job server trusts for the mPCC (#102)
        if self.js.caBundle:
          wgetArgs.insert(0, f"--ca-certificate={self.js.caBundle}")
        log.debug(" ".join(wgetArgs))
        sh.wget(wgetArgs)
         
        log.info(f"repo {repo} download complete. Updating versions")        
        # file download complete.  Update our version
        repoVersions[repo] = mpccVersion
        ourVersion = mpccVersion
        with open(self.repoDir + '/repo-versions', "w") as vfile:
          vfile.write(json.dumps(repoVersions))
      if self.js.id not in currJobServers:
        # tell the MPCC we can host this file
        log.info(f"Adding ourself to mPCC for repo {repo}")
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
        log.info(f"repo {repo} updated.")
      

    # Clean up any directories that are not in our repoList.
    log.info("Scanning for hanging repos... " + self.repoDir)
    # print(self.repoList)
    for entry in os.listdir(self.repoDir):
      # print(entry)
      entryPath = self.repoDir + '/' + entry
      if os.path.isdir(entryPath):
        # see if this entry is in our repo list
        if not os.path.basename(entry) in self.repoList:
          # Nope doesn't seem to be there, doesn't need to exist on disk then.
          # But only if we own it: without our marker this directory is
          # somebody else's data and we must never delete it (#171).
          if not is_mprov_marked_dir(entryPath):
            log.warning("leaving unowned directory alone (no mprov marker): " + entryPath)
            continue
          #print("rm -rf " + entryPath)
          log.warning("Removed unknown repo directory: " + entryPath)
          shutil.rmtree(entryPath)

        
