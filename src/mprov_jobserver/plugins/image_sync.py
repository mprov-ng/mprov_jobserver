import shutil
from threading import local
from .plugin import JobServerPlugin, path_is_under, mark_mprov_dir, is_mprov_marked_dir
import os
import json
import sys

class image_sync(JobServerPlugin):
  jobModule = 'image-sync'
  imageDir = ""
  imageList = None
  # The declared mprov data root; imageDir must sit inside it so the
  # end-of-sync sweep can only ever touch mprov-owned space (#171).
  dataRoot = None

  # override default load config because we have to check that mprov-webserver is also enabled.
  def load_config(self):
    if 'mprov-webserver' not in self.js.jobmodules:
      print("Configuration Error: you MUST run mprov-webserver on image-sync nodes!")
      print("                   : image-sync nodes are SOURCE nodes for images!")
      print("                   : and need a way to serve images! Job Module Halted!!!")
      sys.exit(1)
    if not super().load_config():
      return False
    # A configured imageList is compared with directory names, so make it
    # strings: YAML reads `imageList: [4, 5]` as ints, and the sweep below then
    # deleted (and the next pass re-fetched) every image on the list (#235).
    if self.imageList is not None:
      if not isinstance(self.imageList, list):
        self.imageList = [self.imageList]
      self.imageList = [str(entry) for entry in self.imageList]
    # The sweep at the end of handle_jobs deletes directories under
    # imageDir, so refuse to start unless imageDir sits inside a data
    # root the operator declared (#171).
    if not self.dataRoot or not path_is_under(self.imageDir, self.dataRoot):
      print("Configuration Error: image-sync imageDir '" + str(self.imageDir) + "' must be inside dataRoot '" + str(self.dataRoot) + "'.")
      print("                   : Refusing to run: the end-of-sync sweep deletes directories under imageDir.")
      sys.exit(1)
    return True

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
    print(self.imageList)
    # over a copy: a deleted image is removed from the list as we go, which
    # skipped the next one (#141)
    for image in list(self.imageList):
      # grab the MPCC version for this image.
      # if MPCC 404's the image was deleted.  Remove from our imageList
      # print an error, and move on.  File deletion happens below.
      print(f"Syncing images for {image}")
      response = self.js.session.get( self.js.mprovURL + 'systemimages/' + image + '/')
      # an int: comparing with the string "404" never matched, so a deleted
      # image's null reply crashed the whole pass (#141)
      if response.status_code == 404:
        # image not found in the MPCC, must have been removed, delete it locally.
        self.imageList.remove(image)
        print("Error: Image '" + image + "' was not found on the MPCC, removing locally." )
        print("Error: Consider removing it from your config.")
        continue
      if response.status_code != 200:
        # anything else may be passing; try this image again next pass.
        print(f"Error: the mPCC answered {response.status_code} for image {image}, skipping it this pass.")
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
          # mark it as mprov-owned so the sweep below may clean it up (#171)
          mark_mprov_dir(self.imageDir + '/' + image)
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
          with self.js.session.get(self.js.mprovURL + 'images/' + image + '.vmlinuz', stream=True) as remoteImage:
            remoteImage.raise_for_status()
            os.makedirs(self.imageDir + '/' + image,exist_ok=True)
            with open(self.imageDir + '/' + image + '/' + image + '.vmlinuz', 'wb') as localFile:
              for chunk in remoteImage.iter_content(chunk_size=8192):
                localFile.write(chunk)
        print(f"Image {image} download complete. Updating versions")        
        # file download complete.  Update our version
        imgVersions[image] = mpccVersion
        ourVersion = mpccVersion
        with open(self.imageDir + '/image-versions', "w") as vfile:
          vfile.write(json.dumps(imgVersions))
      if self.js.id not in currJobServers:
        # tell the MPCC we can host this file
        print(f"Adding ourself to mPCC for image {image}")
        # Only our id: the mPCC merges it into the image's current hosts.
        # Sending the list from our GET put back every host a rebuild had
        # cleared since (#248, mprov_control_center#396).
        # no needs_rebuild: sending False cleared any rebuild asked for on
        # the image whenever we added ourselves as a host (#226).
        data = {
          'slug': image,
          'jobservers': [self.js.id],
        }
        response = self.js.session.patch(self.js.mprovURL + 'systemimages/' + str(data['slug']) + '/?addjs', data=json.dumps(data))
        print(f"Image {image} updated.")
      

    # Clean up any directories that are not in our imageList.
    print("Scanning for haning images... " + self.imageDir)
    for entry in os.listdir(self.imageDir):
      # print(entry)
      entryPath = self.imageDir + '/' + entry
      if os.path.isdir(entryPath):
        # see if this entry is in our image list
        if not os.path.basename(entry) in self.imageList:
          # Nope doesn't seem to be there, doesn't need to exist on disk then.
          # But only if we own it: without our marker this directory is
          # somebody else's data and we must never delete it (#171).
          if not is_mprov_marked_dir(entryPath):
            print("Warn: leaving unowned directory alone (no mprov marker): " + entryPath)
            continue
          #print("rm -rf " + entryPath)
          print("Warn: Removed unknown image directory: " + entryPath)
          shutil.rmtree(entryPath)

        
