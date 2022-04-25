from .plugin import JobServerPlugin
from time import sleep
class image_update(JobServerPlugin):
  jobModule = 'image-update'
  imageDir = ""
  imageList = None

# rpm2cpio < rocky-repos-8.5-3.el8.noarch.rpm | cpio -D /mnt/tmproot/ -id
# dnf -y --installroot=/mnt/tmproot/ --releasever=8 groupinstall 'Minimal Install'

  def handle_jobs(self):
    if(not self.set_job_running()):
      # no jobs to run, just return
      return

    
    print(self.jobModule + ": Sleeping 15 seconds to simulate work.  Override 'handle_jobs()' to do something useful.")
    sleep(15)
    
    # Update our jobs with success or failure
    self.set_job_success()