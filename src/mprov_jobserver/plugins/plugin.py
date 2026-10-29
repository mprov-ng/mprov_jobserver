from time import sleep
import threading
import sys
import os

# Marker file mprov writes into every directory it creates inside a
# configured sync directory.  The end-of-sync sweep may only delete
# directories carrying this marker, so a mis-set sync directory can never
# wipe operator data it does not own (#171).
MPROV_MARKER = '.mprov-managed'


def path_is_under(path, root):
  # realpath both sides so a symlink or '..' cannot smuggle a path
  # outside the data root the operator declared.
  path = os.path.realpath(path)
  root = os.path.realpath(root)
  if root == os.sep:
    return path.startswith(os.sep)
  return path == root or path.startswith(root + os.sep)


def mark_mprov_dir(dirpath):
  with open(os.path.join(dirpath, MPROV_MARKER), 'w') as marker:
    marker.write('mprov owns this directory\n')


def is_mprov_marked_dir(dirpath):
  return os.path.isfile(os.path.join(dirpath, MPROV_MARKER))

   
class JobServerPlugin(threading.Thread):
  jobModule = ""
  js = None
  threadOk = True
  # set True by a module that ran and failed, so runonce can exit nonzero (#174)
  failed = False


  def set_job_running(self):
    # This function can be overridden, especially if you are processing specific jobID's.
    return self.js.update_job_status(self.jobModule, 2) # RUNNING = 2
  
  def set_job_success(self):
    # This function can be overridden, especially if you are processing specific jobID's.
    return self.js.update_job_status(self.jobModule, 4) # SUCCESS = 4

  def set_job_failure(self):
    self.threadOk = False
    # This function can be overridden, especially if you are processing specific jobID's.
    return self.js.update_job_status(self.jobModule, 3) # FAILURE = 3

  def __init__(self, js):
    threading.Thread.__init__(self)
    self.name = self.__class__
    self.js = js
    pass
  def checkHTTPStatus(self, status):
    # return True if  199 < status < 300
    if 199 < status < 300: 
      return True
    return False

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
          return False
    if self.jobModule not in self.js.config_data:
      # This is not necessarily an error, maybe one day print a warning?
      return True
    if self.js.config_data[self.jobModule] is None:
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

    # Update all pending jobs of our module to running
    if(not self.set_job_running()):
      # no jobs to run, just return
      return

    
    print(self.jobModule + ": Sleeping 15 seconds to simulate work.  Override 'handle_jobs()' to do something useful.")
    sleep(15)
    
    # Update our jobs with success or failure
    self.set_job_success()


