from .plugin import JobServerPlugin
from time import sleep
import os
import sys
import json
from urllib.parse import urlparse


class image_update(JobServerPlugin):
  jobModule = 'image-update'
  imageDir = ""
  imageList = None


# rpm2cpio < rocky-repos-8.5-3.el8.noarch.rpm | cpio -D /mnt/tmproot/ -id
# dnf -y --installroot=/mnt/tmproot/ --releasever=8 groupinstall 'Minimal Install'

  # override default load config because we have to check that image-server is also enabled.
  def load_config(self):
    if 'image-server' not in self.js.jobmodules:
      print("Configuration Error: you MUST run image-server on image-upate/image-delete nodes!")
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
      query = 'images/' + image + '/details'
      response = self.js.session.get( self.js.mprovURL + query)
      if response.status_code == 200:
        imageDetails = response.json()
        baseURL=imageDetails['osdistro']['baserepo']['repo_package_url']
        print(baseURL)

        imgDir = self.imageDir + '/' + imageDetails['slug']
        # create this image's dir.  Use the image['slug']
        os.makedirs(imgDir, exist_ok=True)
        os.chdir(imgDir)
        # grab the repo rpm
        url = urlparse(baseURL)
        file = os.path.basename(url.path)
        print("Grabbing os repo package: " + baseURL)
        if os.system('wget -O ' + file + ' ' + baseURL ):
          print("Error: unable to get repo package: " + baseURL)
          self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          return

        # force the RPM to unpack to our image dir.
        print("Unpacking RPM to " + imgDir)
        
        if os.system('rpm2cpio < ' + file + ' | cpio -D ' + imgDir + ' -id'):
          print("Error: unable to extract repo package: " + file + ' into ' + imgDir)
          self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          return


        # build the filesystem.
        if os.system('dnf -y --installroot=' + imgDir + ' --releasever=' + str(imageDetails['osdistro']['version'])  + ' groupinstall \'Minimal Install\''):
          print("Error: unable to genergate image filesystem.")
          self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          return

        # package the filesystem into an initramfs
        # print("Building " + os.getcwd() + "/" + imageDetails['slug'] + '.img')
        # if os.system('find .  -depth -print| cpio -oD ' + imgDir + ' | gzip -9 > ' + imageDetails['slug'] + '.img'):
        #   print("Error: unable to create initramfs")
        #   self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
        #   return
        print("Image saved to "+ os.getcwd() + "/" + imageDetails['slug'] + '.img')
        
        # update the 'jobservers' field to be us, so that the 
        data = {
          'slug': imageDetails['slug'],
          'needs_rebuild': False,
          'jobservers':[
            self.js.id,
          ]
        }
        response = self.js.session.patch(self.js.mprovURL + 'images/' + str(data['slug']) + '/update', data=json.dumps(data))

    # Update our jobs with success or failure
    self.set_job_success()