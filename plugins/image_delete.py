from .plugin import JobServerPlugin
import os
import shutil

class image_delete(JobServerPlugin):
  jobModule = 'image-delete'
  imageList = None
  imageDir = ""
  def handle_jobs(self):
    # See if we have any image-delete jobs, and take 'em if we do, else just exit
    jobquery = "jobserver=" + str(self.js.id) + "&module=image-delete"
    if not self.js.update_job_status(self.jobModule, 2, jobquery=jobquery + "&status=1"):
      return # no jobs.
    
    # we have some jobs, let's do this.
    # get the jobs
    response = self.js.session.get( self.js.mprovURL + 'jobs/?' + jobquery + '&status=2')
    for job in response.json():
      # grab the params.
      try:
          params = job['params']
      except: 
        print("Error: Image Delete Job with no imageID present, cannot parse params")
        self.js.update_job_status(self.jobModule, 3, jobid=job['id'])
        return
      if self.imageList is None or params['imageId'] in self.imageList:
        # This appears to be one of ours.
        if os.path.exists(self.imageDir):
          if os.path.exists(self.imageDir + '/' + params['imageId']):
            # the path seems there.  Kill it.
            shutil.rmtree(self.imageDir + '/' + params['imageId'])
            #print("rm -rf " + self.imageDir + '/' + params['imageId'])
            self.js.update_job_status(self.jobModule, 4, jobid=job['id'])
          else:
            print("Error: image has no directory:" + self.imageDir + '/' + params['imageId'])
            self.js.update_job_status(self.jobModule, 3, jobid=job['id'])
            
        else:
          print("Error: imageDir doesn't exist: " + self.imageDir)
          self.js.update_job_status(self.jobModule, 3, jobid=job['id'])
          
      else:
        print("Error: Recieved image-delete job for image we don't host. HuH?")
        self.js.update_job_status(self.jobModule, 3, jobid=job['id'])

  