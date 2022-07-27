import shutil
from threading import local
from .plugin import JobServerPlugin
import os
import json
import sys

class image_sync(JobServerPlugin):
  jobModule = 'image-sync'
  imageDir = ""
  imageList = None

  # override default load config because we have to check that mprov-webserver is also enabled.
  def load_config(self):
    if 'mprov-webserver' not in self.js.jobmodules:
      print("Configuration Error: you MUST run mprov-webserver on image-sync nodes!")
      print("                   : image-sync nodes are SOURCE nodes for images!")
      print("                   : and need a way to serve images! Job Module Halted!!!")
      sys.exit(1)
    return super().load_config()

  def handle_jobs(self):
    
    # image-sync is not job based.  We will connect to the MPCC, compare the version we know
    # about for our image, and update it if it doesn't match.

    # images we care about: self.imageList, if populated.  All images in MPCC if None.
    # image versions tacked via version file in self.imageDir.

    if not os.path.exists(self.imageDir):
      # if we don't have an image path, make one.
      os.makedirs(self.imageDir)
    
    # see if we have a image-versions file
    if not os.path.exists(self.imageDir + '/image-versions'):
      # no versions file.  Create a blank one right quick
      with open(self.imageDir + '/image-versions', "w") as vfile:
        vfile.write(json.dumps({}))
    
    # now we should have a dir and a version file, lets get our list of images we care about
    if self.imageList is None:
      self.imageList = []
      # no imageList in config, grab it from MPCC
      response = self.js.session.get( self.js.mprovURL + 'images/')
      for image in response.json():
        self.imageList.append(image['slug'])
    
    # so here we should have either a hard coded config list of images, 
    # or a list of all images grabbed from the server.
    for image in self.imageList: 
      # grab the MPCC version for this image.
      # if MPCC 404's the image was deleted.  Remove from our imageList
      # print an error, and move on.  File deletion happens below.
      response = self.js.session.get( self.js.mprovURL + 'systemimages/' + image + '/')
      if(response.status_code == "404" ):
        # image not found in the MPCC, must have been removed, delete it locally.
        self.imageList.remove(image)
        print("Error: Image '" + image + "' was not found on the MPCC, removing locally." )
        print("Error: Consider removing it from your config.")
        continue
      if(response.json()['jobservers'] == []):
        self.imageList.remove(image)
        print("Warn: No jobservers available yet for image: " + image)
        print("Warn: Not syncing image: " + image)
        continue
      mpccVersion = response.json()['version']
      currJobServers = response.json()['jobservers']

      with open(self.imageDir + '/image-versions', "r") as vfile:
        # grab the version info into a var
        try:
          imgVersions = json.loads(vfile.read())
        except:
          # if we have an error loading the version data, 
          # init it with no version data, we'll rebuild it 
          # in a moment.
          imgVersions = {}
    
      # see if our image is in imgVersions
      ourVersion = 0
      if image in imgVersions:
        ourVersion = imgVersions[image]
      
      # compare our version to the one the MPCC has for us.
      if ourVersion != mpccVersion:
        # Versions don't match, ask the MPCC to grab a new copy.
        with self.js.session.get(self.js.mprovURL + 'images/' + image, stream=True) as remoteImage:
          remoteImage.raise_for_status()
          os.makedirs(self.imageDir + '/' + image,exist_ok=True)
          with open(self.imageDir + '/' + image + '/' + image + '.img', 'wb') as localFile:
            for chunk in remoteImage.iter_content(chunk_size=8192):
              localFile.write(chunk)

          # grab the initramfs
          with self.js.session.get(self.js.mprovURL + 'images/' + image + '.initramfs', stream=True) as remoteImage:
            remoteImage.raise_for_status()
            os.makedirs(self.imageDir + '/' + image,exist_ok=True)
            with open(self.imageDir + '/' + image + '/' + image + '.initramfs', 'wb') as localFile:
              for chunk in remoteImage.iter_content(chunk_size=8192):
                localFile.write(chunk)
        
          # grab the kernel
          with self.js.session.get(self.js.mprovURL + 'kernels/' + image + '.vmlinuz', stream=True) as remoteImage:
            remoteImage.raise_for_status()
            os.makedirs(self.imageDir + '/' + image,exist_ok=True)
            with open(self.imageDir + '/' + image + '/' + image + '.vmlinuz', 'wb') as localFile:
              for chunk in remoteImage.iter_content(chunk_size=8192):
                localFile.write(chunk)
                
        # file download complete.  Update our version
        imgVersions[image] = mpccVersion
        ourVersion = mpccVersion
        with open(self.imageDir + '/image-versions', "w") as vfile:
          vfile.write(json.dumps(imgVersions))

        # tell the MPCC we can host this file
        jobservers = []
        for jobserver in currJobServers:
          jobservers.append(jobserver['id'])
        # now append our id
        jobservers.append(self.js.id)
        data = {
          'slug': image,
          'needs_rebuild': False,
          'jobservers': jobservers,
        }
        response = self.js.session.patch(self.js.mprovURL + 'systemimages/' + str(data['slug']) + '/', data=json.dumps(data))

    # Clean up any directories that are not in our imageList.
    # print("Scanning for haning images... " + self.imageDir)
    for entry in os.listdir(self.imageDir):
      # print(entry)
      if os.path.isdir(self.imageDir + '/' + entry):
        # see if this entry is in our image list
        if not os.path.basename(entry) in self.imageList:
          # Nope doesn't seem to be there, doesn't need to exist on disk then.
          #print("rm -rf " + self.imageDir + '/' + entry)
          print("Warn: Removed unknown image directory: " + self.imageDir + '/' + entry)
          shutil.rmtree(self.imageDir + '/' + entry)

        
