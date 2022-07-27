import importlib
from .plugin import JobServerPlugin
import os
import sys



class image_update(JobServerPlugin):
  jobModule = 'image-update'
  imageDir = ""
  imageList = None

  # override default load config because we have to check that mprov-webserver is also enabled.
  def load_config(self):
    if 'mprov-webserver' not in self.js.jobmodules:
      print("Configuration Error: you MUST run mprov-webserver on image-upate/image-delete nodes!")
      print("                   : image-update/delete nodes are SOURCE nodes for images!")
      print("                   : and need a way to serve images! Job Module Halted!!!")
      sys.exit(1)
    return super().load_config()

  def handle_jobs(self):
    if self.imageList is None:
      # grab all the image-update jobs.
      if not self.js.update_job_status(self.jobModule, 2):
        return # no jobs.
      # populate self.imageList with the list of all pending images.
      self.imageList = []
      response = self.js.session.get( self.js.mprovURL + 'jobs/?jobserver=' + str(self.js.id) + '&module=' + self.jobModule + '&status=2')
      for job in response.json():
        try:
          params = job['params']
        except: 
          print("Error: Image Update Job with no imageID present, cannot parse params")
          self.js.update_job_status(self.jobModule, 3, jobid=job['id'])
          return
        try:
          self.imageList.append(params['imageId'])
        except:
          print("Error: Image Update Job failed, corrupted params.")
          self.js.update_job_status(self.jobModule, 3, jobid=job['id'])
          return
    else:
      # imageList is not none, so let's grab just the ones we care about.
      updateFound = False
      for image in self.imageList:
        # json to pass to the job search.
        paramstr="{\"imageId\":\"" + image + "\"}"
        if self.js.update_job_status(self.jobModule, 2, jobquery=paramstr):
          updateFound=True
        else: 
          # this image wasn't requested to be updated, remove it from the list.
          self.imageList.remove(image)
      if (not updateFound) or len(self.imageList) == 0:
        return # no updates found.
        

    # check if the imageDir exists.
    if not os.path.exists(self.imageDir):
      # if not, attempt to make it, recursively.
      try:
        os.makedirs(self.imageDir, exist_ok=True)
      except:
        print("Error: Unable to make imageDir: " + self.imageDir)
        self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
        return
    
    # now we iterate over our imageList, and build the image(s)
    for image in self.imageList:
      print(f"Processing update for image {image}")
      query = 'systemimages/' + image + '/'
      response = self.js.session.get( self.js.mprovURL + query)
      if response.status_code == 200:

                # instantiate the sub module and pass it the repo.
        try:
          imageDetails = response.json()
        except:
          print("Error: Unable to parse response.")
          continue
        
        try:
          print(f".image_update_mod.{imageDetails['osdistro']['baserepo']['ostype']['slug']}")
          mod = importlib.import_module(f".image_update_mod.{imageDetails['osdistro']['baserepo']['ostype']['slug']}", "mprov_jobserver.plugins")
          update_klass = getattr(mod, 'UpdateImage')
        except BaseException as err:
          print("Error: Unable to import Image Update Module.")
          print(f"Exception: {err=}, {type(err)=}")
          continue

        try:
          image_update = update_klass(self.js)
        except:
          print(f"Error: Unable to instantiate UpdateImage class on {imageDetails['osdistro']['baserepo']['ostype']['slug']}")
          continue
        image_update.imageDetails = imageDetails
        image_update.imageDir = self.imageDir
        image_update.start()

      else: 
          print(f"Error handling job for {image} server returned {response.status_code} for request {self.js.mprovURL + query}")
          self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')