from time import sleep
import threading
import sys

   
class JobServerPlugin(threading.Thread):
  jobModule = ""
  js = None


  def set_job_running(self):
    # This function can be overridden, especially if you are processing specific jobID's.
    return self.js.update_job_status(self.jobModule, 2) # RUNNING = 2
  
  def set_job_success(self):
    # This function can be overridden, especially if you are processing specific jobID's.
    return self.js.update_job_status(self.jobModule, 4) # SUCCESS = 4

  def set_job_failure(self):
    # This function can be overridden, especially if you are processing specific jobID's.
    return self.js.update_job_status(self.jobModule, 3) # FAILURE = 3

  def __init__(self, js):
    threading.Thread.__init__(self)
    self.name = self.__class__
    self.js = js
    pass

  def run(self):
    if self.load_config():
      self.handle_jobs()
    

  def load_config(self):
    # NOTE: Override with your config loading routine if needed. 
    # Access to js.config_data to parse the config file.
    # for information on how to parse the config_data structure, 
    # see https://pyyaml.org/wiki/PyYAMLDocumentation 
    # 
    # This data structure is loaded by the JobServer class at 
    # startup

    # This template config loader should work for all modules.  Make sure 
    # you put your module config in the plugins/ dir in the same location
    # as the main jobserver.yaml file.  
    #print(self.js.config_data)
    if self.js.config_data is None:
          print("Conf is empty? ")
          return False
    if self.jobModule not in self.js.config_data:
      # This is not necessarily an error, maybe one day print a warning?
      print("Warn: No config found for " + self.jobModule + " hope that's ok..")
      return True
    if self.js.config_data[self.jobModule] is None:
          print("Found empty module config?")
          return False
    for config_entry in self.js.config_data[self.jobModule].keys():
        try:
          getattr(self, config_entry)
          setattr(self, config_entry, self.js.config_data[self.jobModule][config_entry])
        except:
          print("Error: Unknown config entry " + config_entry + " in " + self.jobModule)
    return True

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


