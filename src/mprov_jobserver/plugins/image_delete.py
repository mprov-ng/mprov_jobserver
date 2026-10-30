from .plugin import JobServerPlugin, mounts_under
import os
import shutil
from mprov_jobserver.log import get_logger

log = get_logger(__name__)

class image_delete(JobServerPlugin):
  jobModule = 'image-delete'
  imageList = None
  imageDir = ""

  def image_path(self, params):
    '''
    The image directory a job's params name, or None if they don't name one.
    The path is built from the job's imageId and handed to rmtree as root,
    so it must be exactly one entry of imageDir (#158): an empty imageId
    (or ".") meant imageDir itself, the whole image store, and ".." or a
    "/" anything above or below it.
    '''
    imageId = params.get('imageId') if isinstance(params, dict) else None
    if not isinstance(imageId, str) or imageId in ("", ".", "..") or "/" in imageId or "\0" in imageId:
      return None
    store = os.path.realpath(self.imageDir)
    path = os.path.join(store, imageId)
    # a symlink in the store must not lead the rmtree elsewhere either
    if os.path.dirname(os.path.realpath(path)) != store:
      return None
    return path

  def handle_jobs(self):
    # See if we have any image-delete jobs, and take 'em if we do, else just exit
    jobquery = "jobserver=" + str(self.js.id) + "&module=image-delete"
    if not self.js.update_job_status(self.jobModule, 2, jobquery=jobquery + "&status=1"):
      return # no jobs.

    # we have some jobs, let's do this.
    # get the jobs
    response = self.js.session.get( self.js.mprovURL + 'jobs/?' + jobquery + '&status=2')
    for job in response.json():
      # a bad job fails by itself; the ones after it still run (#158)
      path = self.image_path(job.get('params'))
      if path is None:
        log.error(f"image-delete job {job['id']} doesn't name an image (params: {job.get('params')!r}), not deleting anything.")
        self.js.update_job_status(self.jobModule, 3, jobid=job['id'])
        continue
      imageId = os.path.basename(path)
      if self.imageList is None or imageId in self.imageList:
        # This appears to be one of ours.
        if os.path.exists(self.imageDir):
          if os.path.exists(path):
            # a build that died left the host's /dev etc. bound in the image:
            # rmtree would delete through them (#183)
            mounted = mounts_under(path)
            if mounted:
              log.error(f"not deleting {path}, something is still mounted in it: {', '.join(mounted)}")
              self.js.update_job_status(self.jobModule, 3, jobid=job['id'])
              continue
            # the path seems there.  Kill it.
            try:
              shutil.rmtree(path)
            except OSError as err:
              log.error(f"unable to delete {path}: {err}")
              self.js.update_job_status(self.jobModule, 3, jobid=job['id'])
              continue
            self.js.update_job_status(self.jobModule, 4, jobid=job['id'])
          else:
            log.error("image has no directory:" + path)
            self.js.update_job_status(self.jobModule, 3, jobid=job['id'])

        else:
          log.error("imageDir doesn't exist: " + self.imageDir)
          self.js.update_job_status(self.jobModule, 3, jobid=job['id'])

      else:
        log.error("Received image-delete job for image we don't host. HuH?")
        self.js.update_job_status(self.jobModule, 3, jobid=job['id'])


