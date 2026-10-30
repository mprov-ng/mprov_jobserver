from .plugin import JobServerPlugin
import os
import re
import shutil
from mprov_jobserver.log import get_logger

log = get_logger(__name__)

# repo-update mirrors each repo into <repoDir>/<repo id>
REPO_ID = re.compile(r'[0-9]+')


class repo_delete(JobServerPlugin):
  '''
  Removes the mirrors of deleted repos.  The mPCC queues one "Delete Repos"
  job, with no params, whenever a repo is deleted, so the job doesn't say
  which: on each job this compares repoDir with the repos the mPCC still
  has and removes the mirror directories of the ones it doesn't (#161).
  '''
  jobModule = 'repo-delete'
  repoDir = ""

  def load_config(self):
    if not super().load_config():
      return False
    # the mirrors are repo-update's; use its repoDir unless ours is set
    if not self.repoDir:
      update = (self.js.config_data or {}).get('repo-update') or {}
      self.repoDir = update.get('repoDir', "")
    return True

  def stale_repos(self, known):
    '''
    The mirror directories in repoDir whose repo the mPCC no longer has.
    Only real directories named by a repo id: anything else in repoDir
    (repo-versions, robots.txt, an operator's files, a symlink) isn't a
    mirror, and the rmtree runs as root.
    '''
    stale = []
    for name in sorted(os.listdir(self.repoDir)):
      path = os.path.join(self.repoDir, name)
      if not REPO_ID.fullmatch(name) or name in known:
        continue
      if os.path.islink(path) or not os.path.isdir(path):
        continue
      stale.append(path)
    return stale

  def handle_jobs(self):
    jobquery = "module=" + self.jobModule
    if not self.js.update_job_status(self.jobModule, 2, jobquery=jobquery + "&status=1"):
      return # no jobs.
    response = self.js.session.get(self.js.mprovURL + 'jobs/?jobserver=' + str(self.js.id) + '&' + jobquery + '&status=2')
    jobs = response.json() if self.checkHTTPStatus(response.status_code) else []
    if not isinstance(jobs, list):
      jobs = []

    ok = self.delete_stale()
    for job in jobs:
      self.js.update_job_status(self.jobModule, 4 if ok else 3, jobid=job['id'])
    if not ok:
      self.failed = True

  def delete_stale(self):
    ''' Remove the deleted repos' mirrors.  False if that didn't work out. '''
    if not self.repoDir:
      log.error("repo-delete has no repoDir (set it, or repo-update's), not deleting anything.")
      return False
    if not os.path.isdir(self.repoDir):
      # nothing was ever mirrored here, so nothing to delete
      log.info(f"{self.repoDir} doesn't exist, nothing to delete.")
      return True
    # every repo, managed or not: only a repo the mPCC no longer has at all
    # loses its mirror.  Without a real list, delete nothing.
    response = self.js.session.get(self.js.mprovURL + 'repos/')
    repos = response.json() if response.status_code == 200 else None
    if not isinstance(repos, list):
      log.error(f"the mPCC answered {response.status_code} for its repo list, not deleting anything.")
      return False
    known = {str(repo['id']) for repo in repos if isinstance(repo, dict) and 'id' in repo}
    ok = True
    for path in self.stale_repos(known):
      try:
        shutil.rmtree(path)
        log.info(f"Removed {path}: its repo was deleted from the mPCC.")
      except OSError as err:
        log.error(f"unable to delete {path}: {err}")
        ok = False
    return ok
