from time import sleep


   
class JobServerPlugin():
  jobModule = ""
  js = None

  def set_job_running(self):
    return self.js.update_job_status(self.jobModule, 2) # RUNNING = 2
  
  def set_job_success(self):
    return self.js.update_job_status(self.jobModule, 4) # SUCCESS = 4

  def set_job_failure(self):
    return self.js.update_job_status(self.jobModule, 3) # FAILURE = 3

  def __init__(self, js):
    self.js = js
    pass

  def handle_jobs(self):
    # NOTE:
    # 
    # Default structure: 
    #
    # self.set_job_running()
    #
    # ... do something ...
    # 
    # if something :
    #   set_job_success()
    # else
    #   set_job_failure()
    #
    # By default, this function will only test the work flow with a 15 second delay as the 'work'

    # Update all pending jobs of oir module to running
    if(not self.set_job_running()):
      # no jobs to run, just return
      return

    
    print(self.jobModule + ": Sleeping 15 seconds to simulate work.  Override 'handle_jobs()' to do something useful.")
    sleep(15)
    
    # Update our jobs with success or failure
    self.set_job_success()

    pass


# jobserver class gets passed to handle_jobs.   Provides methods to update jobs to RUNNING SUCCESS or FAILURE
