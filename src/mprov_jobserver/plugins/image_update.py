import importlib
from .plugin import JobServerPlugin
import os
import stat
import subprocess
import sys
import json
import time
import urllib.parse

import yaml
from jinja2 import Environment, PackageLoader, select_autoescape

from os.path import dirname, basename, isfile, join
import glob

jenv = Environment(
    loader=PackageLoader("mprov_jobserver"),
    autoescape=select_autoescape()
)


def redacted_image_details(imageDetails):
  """Copy of the image details with the root password masked, for logging (#168)."""
  distro = imageDetails.get('osdistro')
  if isinstance(distro, dict) and 'rootpw' in distro:
    return {**imageDetails, 'osdistro': {**distro, 'rootpw': '******'}}
  return imageDetails

class image_update(JobServerPlugin):
  jobModule = 'image-update'
  imageDir = ""
  imageList = None
  depMods = [ 'repo-sync' ]

  # override default load config because we have to check that mprov-webserver is also enabled.
  def load_config(self):
    if 'mprov-webserver' not in self.js.jobmodules:
      print("Configuration Error: you MUST run mprov-webserver on image-upate/image-delete nodes!")
      print("                   : image-update/delete nodes are SOURCE nodes for images!")
      print("                   : and need a way to serve images! Job Module Halted!!!")
      sys.exit(1)
    # run load config on each sub_module
    modules = glob.glob(join(dirname(__file__) + "/image_update_mod", "*.py"))
    mods = [ basename(f)[:-3] for f in modules if isfile(f) and not f.endswith('__init__.py')]
    for name in mods:
      try:
        mod = importlib.import_module(f".image_update_mod.{name}", "mprov_jobserver.plugins")
        update_klass = getattr(mod, 'UpdateImage')
      except BaseException as err:
        print(f"Error: Unable to import Image Update Module for {name}.")
        print(f"Exception: {err=}, {type(err)=}")
        continue

      try:
        image_update = update_klass(self.js)
      except:
        print(f"Error: Unable to instantiate UpdateImage class on {name}")
        continue
      image_update.load_config()
    return super().load_config()

  def updateImageJobservers(self):

    # if we don't have an ~image path, make one.  If we can't, there's
    # nothing to host; handle_jobs() fails the claimed jobs (#144).
    try:
      os.makedirs(self.imageDir, exist_ok=True)
    except OSError as err:
      print(f"Error: Unable to make imageDir: {self.imageDir}: {err}")
      return

    # see if we have a image-versions file
    if not os.path.exists(self.imageDir + '/image-versions'):
      # no versions file.  Create a blank one right quick
      with open(self.imageDir + '/image-versions', "w") as vfile:
        vfile.write(json.dumps({}))
    imgVersions = {}
    with open(self.imageDir + '/image-versions', "r") as vfile:
      # grab the version info into a var
      try:
        imgVersions = json.loads(vfile.read())
      except:
        pass
        

    # cycle through the images we should be serving and 
    # check our version(s) against the mPCC.
    imageList = []
    if self.imageList is None:
      imageList = []
      # no config for image list, so grab all of them from the mPCC
      response = self.js.session.get(f"{self.js.mprovURL}images/")
      #if we don't get a good reply, error out of this function only. We'll try again
      # on the next iteration
      if response.status_code <=199 or response.status_code >=300:
        print("Error: Unable to get a list of images from the mPCC.")
        return
      try: 
        for image in response.json():
          imageList.append(image['slug'])
      except:
        print("Error: Unable to get a list of images from the mPCC")

        return

    # here we should have some sort of an image list.  So let's begin.
    for image in imageList:
      response = self.js.session.get(f"{self.js.mprovURL}systemimages/{image}/")
      if response.status_code <= 199 or response.status_code >= 300:
        print(f"Error: Unable to retrieve data for image {image}")
        continue
      try:
        imageData = response.json()
      except:
        imageData = None
        print(response.text)
      if imageData is None:
        print(f"Error: Error trying to retrieve data for image {image}")
        continue
      # print(imageData)
      # print(self.js.id)
      if self.js.id in imageData['jobservers']:
        # we are already in here.  
        continue
      #print(imgVersions)
      if imageData['customIPXE'] not in ("", None):
        continue

      # skip if rebuild requested.
      if imageData['needs_rebuild']:
        continue
      if image not in imgVersions:
        # we have no record of this image version, shim with 0
        imgVersions[image] = 0
      if imgVersions[image] == imageData['version']:
        # our image version matches, tell the mPCC about us.
        jobservers = []
        for jobserver in imageData['jobservers']:
          jobservers.append(jobserver)
        # now append our id
        jobservers.append(self.js.id)
        data = {
          'slug': image,
          'needs_rebuild': False,
          'jobservers': jobservers,
        }
        response = self.js.session.patch(self.js.mprovURL + 'systemimages/' + str(data['slug']) + '/?addjs', data=json.dumps(data))
        if response.status_code <= 199 or response.status_code >= 300:
          print(f"Error: mPCC wouldn't let us add ourselves, status code: {response.status_code}")
        continue
      # check our version against the mPCC version
      if imgVersions[image] != imageData['version']:
        if len(imageData['jobservers']) > 0:
          # if our version doesn't match, and there are job servers available, pull the image from someone else.
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
            with self.js.session.get(self.js.mprovURL + 'images/' + image + '.vmlinuz', stream=True) as remoteImage:
              remoteImage.raise_for_status()
              os.makedirs(self.imageDir + '/' + image,exist_ok=True)
              with open(self.imageDir + '/' + image + '/' + image + '.vmlinuz', 'wb') as localFile:
                for chunk in remoteImage.iter_content(chunk_size=8192):
                  localFile.write(chunk)
            with open(self.imageDir + '/image-versions', "w") as vfile:
              vfile.write(json.dumps(imgVersions))
        # else:
        #   # if our version doesn't match, and there are no jobservers, submit an image-update job to the mPCC
        #   data = {
        #     "params": { "imageId" : imageData['slug'] },
        #     "name": "Image Update",
        #     "module": "image-update",
        #     "status": 1,
        #     "jobserver": self.js.id,
        #   }
        #   #print(json.dumps(data))
        #   response = self.js.session.post(f"{self.js.mprovURL}jobs/",data=json.dumps(data))
        #   if response.status_code <= 199 or response.status_code >= 300:
        #     print(f"Error: Could not submit update job to mPCC, status code: {response.status_code}, image: {imageData['slug']}")

  def handle_jobs(self):
    # try:
    self.updateImageJobservers()
    # except Exception as e:
    #   print("Error: There was an error updating Jobservers for Images.")
    #   print(f"{e=}")

    # check to see if we have any dependancy jobs running
    response = self.js.session.get( self.js.mprovURL + 'jobs/?&module=repo-update&status=1')
    if response.status_code != 200:
      self.threadOk = False
      print(f"Error: Unable to check on dependent jobs.")
      return
    jobs = {}
    try:
      jobs = response.json()
      if len(jobs):
        self.threadOk = False
        print(f"image_update.py: Dependant Jobs Present, waiting for them to finish.")
        return
    except:
      print("WARN: Unable to parse reply, trying to continue anyway.")
    
    response = self.js.session.get( self.js.mprovURL + 'jobs/?&module=repo-update&status=2')
    if response.status_code != 200:
      self.threadOk = False
      print(f"Error: Unable to check on dependent jobs.")
      return
    jobs = {}
    try:
      jobs = response.json()
      if len(jobs):
        self.threadOk = False
        print(f"image_update.py: Dependant Jobs Present, waiting for them to finish.")
        return
    except:
      print("WARN: Unable to parse reply, trying to continue anyway.")
    

    if self.imageList is None:
      # grab all the image-update jobs.
      if not self.js.update_job_status(self.jobModule, 2):
        return # no jobs.
      # populate self.imageList with the list of all pending images.
      self.imageList = {}
      response = self.js.session.get( self.js.mprovURL + 'jobs/?jobserver=' + str(self.js.id) + '&module=' + self.jobModule + '&status=2')
      for job in response.json():
        try:
          params = job['params']
        except: 
          print("Error: Image Update Job with no imageID present, cannot parse params")
          self.js.update_job_status(self.jobModule, 3, jobid=job['id'])
          return
        try:
          self.imageList[params['imageId']] = job['id']
        except:
          print("Error: Image Update Job failed, corrupted params.")
          self.js.update_job_status(self.jobModule, 3, jobid=job['id'])
          return
    else:
      # imageList is not none, so let's grab just the ones we care about.
      # The mPCC matches ?params= key by key; pending jobs have no
      # jobserver yet, so the claim can't filter on it (#172).  Build the
      # same image -> job id dict as above (#144).
      configured = self.imageList
      self.imageList = {}
      for image in configured:
        params = urllib.parse.quote(json.dumps({"imageId": image}))
        query = 'module=' + self.jobModule + '&params=' + params
        if not self.js.update_job_status(self.jobModule, 2, jobquery=query + '&status=1'):
          continue # this image wasn't requested to be updated.
        response = self.js.session.get(self.js.mprovURL + 'jobs/?jobserver=' + str(self.js.id) + '&' + query + '&status=2')
        try:
          for job in response.json():
            self.imageList[image] = job['id']
        except:
          print(f"Error: Unable to read the claimed job for image {image}.")
      if len(self.imageList) == 0:
        return # no updates found.
        

    # check if the imageDir exists.
    if not os.path.exists(self.imageDir):
      # if not, attempt to make it, recursively.
      try:
        os.makedirs(self.imageDir, exist_ok=True)
      except:
        print("Error: Unable to make imageDir: " + self.imageDir)
        # no image is being built yet, so fail every job we claimed (#144).
        for jid in self.imageList.values():
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
        return
    
    # now we iterate over our imageList, and build the image(s).  An error
    # fails that image's job and moves on: returning would leave the other
    # claimed jobs running (#220).
    for image,jid in self.imageList.items():
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
          if imageDetails['osdistro'] is None:
            print(f"Error: No OS Distrubtion set. Not generating image.")
            continue
          print(f"..{redacted_image_details(imageDetails)}")
          print(f".image_update_mod.{imageDetails['osdistro']['baserepo']['ostype']['slug']}")
          mod = importlib.import_module(f".image_update_mod.{imageDetails['osdistro']['baserepo']['ostype']['slug']}", "mprov_jobserver.plugins")
          update_klass = getattr(mod, 'UpdateImage')
        except BaseException as err:
          exc_type, exc_obj, exc_tb = sys.exc_info()
          fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
          print("Error: Unable to import Image Update Module.")
          print(f"Exception: {err=}, {type(err)=}, File={fname}, Line={exc_tb.tb_lineno}")
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          continue

        try:
          image_update = update_klass(self.js)
        except:
          print(f"Error: Unable to instantiate UpdateImage class on {imageDetails['osdistro']['baserepo']['ostype']['slug']}")
          continue
        image_update.imageDetails = imageDetails
        image_update.imageDir = self.imageDir
        image_update.jobid = jid
        image_update.start()

        image_update.join()
        if image_update.threadOk != True:
          continue
        # run image-gen scripts.
    
        
        imgDir = self.imageDir + '/' + imageDetails['slug']
        # copy in the system resolv.conf
        os.system(f"/bin/cp -f /etc/resolv.conf {imgDir}/etc/resolv.conf")
        if os.system("chroot " + imgDir + " pip3 --no-cache-dir install --upgrade mprov_jobserver"):
          print("Error: Unable to install mprov_jobserver python module.")
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          continue

        # the jobserver.yaml we write under the image holds the fleet API
        # key, so it must be reachable only by us (#169).
        mprovDir = imgDir + '/tmp/mprov'
        jobserverYaml = mprovDir + '/jobserver.yaml'
        os.makedirs(mprovDir, mode=0o700, exist_ok=True)
        # the image tree is under our control, but the image we unpacked
        # may have left a symlink or a directory owned by another uid at
        # this path; refuse to write through it (#169).
        st = os.lstat(mprovDir)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
          print(f"Error: {mprovDir} is not a directory owned by this user, failing job.")
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          continue
        os.chmod(mprovDir, 0o700)
        os.makedirs(mprovDir + '/plugins', mode=0o700, exist_ok=True)
        os.chmod(mprovDir + '/plugins', 0o700)

        # create a config file to /tmp for our script-runner instance.
        localConfig = [dict()]
        localConfig[0]['global'] = self.js.config_data['global']
        print(localConfig[0]['global']['jobmodules'])
        localConfig[0]['global']['jobmodules'] = ['script-runner']
        localConfig[0]['global']['runonce'] = True
        try:
          fd = os.open(jobserverYaml, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        except OSError as err:
          print(f"Error: Unable to create {jobserverYaml}, failing job: {err}")
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          continue
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as confFile:
          yaml.dump(localConfig, confFile)
          confFile.write("\n- !include plugins/*.yaml")

        try:
          data = {
            'imgDir': imgDir,
            'image': imageDetails['slug'],
          }

          # copy in our root keys from the distro
          if 'rootsshkeys' in imageDetails['osdistro']:
            if imageDetails['osdistro']['rootsshkeys'] is not None:
              # the key material must land in the image tree, not on the build host (#175)
              sshDir = imgDir + '/root/.ssh'
              authKeys = sshDir + '/authorized_keys'
              try:
                os.makedirs(sshDir, mode=0o700, exist_ok=True)
                os.chmod(sshDir, 0o700)
                with open(authKeys, mode="w") as authkeys:
                  authkeys.write(imageDetails['osdistro']['rootsshkeys'] )
                os.chmod(authKeys, 0o600)
              except OSError as err:
                print(f"Error: Unable to write root authorized_keys into the image, failing job: {err}")
                self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
                continue
          
          # set the root pw to the password in the distro
          if 'rootpw' in imageDetails['osdistro']:
            if imageDetails['osdistro']['rootpw'] is not None and imageDetails['osdistro']['rootpw'] != "" :
              print("Setting root password...")
              # feed the password to the chrooted passwd over its stdin pipe,
              # which is private to this (root) process; an `echo ... | passwd`
              # shell command would expose it in the sh -c argv (#168).
              subprocess.run(["chroot", imgDir, "passwd", "root"],
                             input=f"{imageDetails['osdistro']['rootpw']}\n" * 2,
                             text=True)

          # now let's run our script-runner shell script.
          with open(os.open(imgDir + '/tmp/mprov/script-runner.sh',os.O_CREAT | os.O_WRONLY, 0o755) , 'w') as conf:
              conf.write(jenv.get_template('image-update/script-runner.sh').render(data))
          # now let's run our script-runner shell script.
          with open(os.open(imgDir + '/tmp/mprov/plugins/script-runner.yaml',os.O_CREAT | os.O_WRONLY, 0o755) , 'w') as conf:
              conf.write(jenv.get_template('image-update/script-runner.yaml.j2').render(data))

          if os.system(imgDir + '/tmp/mprov/script-runner.sh'):
            print("Error while running image-gen scripts via job server..")
        finally:
          try:
            os.unlink(jobserverYaml)
          except FileNotFoundError:
            pass

        # grab a copy of busybox into a root-only directory under the image
        # store.  A fixed /tmp path is world-writable: another user could
        # plant a binary there for us to run as root (#170).
        busyboxDir = self.imageDir + '/.busybox'
        os.makedirs(busyboxDir, mode=0o700, exist_ok=True)
        os.chmod(busyboxDir, 0o700)
        busybox = busyboxDir + '/busybox'
        if os.system(f"wget -q -O {busybox} https://busybox.net/downloads/binaries/1.35.0-x86_64-linux-musl/busybox"):
          print("Error: unable to download busybox")
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          continue
        # don't run the download unless it is the ELF binary we asked for;
        # wget -O leaves error pages and partial files in place (#170).
        try:
          with open(busybox, "rb") as f:
            magic = f.read(4)
        except OSError:
          magic = b""
        if magic != b"\x7fELF":
          print("Error: downloaded busybox is not a valid ELF binary, refusing to run it")
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          continue
        os.chmod(busybox, 0o755)

        # attempt to call updateBootFiles()
        # Check if 'updateBootFiles' exists and is callable
        if hasattr(image_update, 'updateBootFiles') and callable(getattr(image_update, 'updateBootFiles', None)):
          image_update.updateBootFiles() # You can call it
          # it fails the job itself; don't pack and register the image (#144).
          if image_update.threadOk != True:
            continue
        else:
          print(f"Notice: {imageDetails['osdistro']['baserepo']['ostype']['slug']} does not have updateBootFiles.")


        # package the filesystem into an initramfs
        print("Building " +  imgDir + '/' + imageDetails['slug'] + '.img')
        startTime=time.time()
        os.system('rm -f ' + imgDir + '/' + imageDetails['slug'] + '.img')
        if os.system(busybox + ' tar c -C' + imgDir + ' -f - ./ | gzip -1 -c > /tmp/' + imageDetails['slug'] + '.img'):
          print("Error: unable to create initramfs")
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          continue
        endTime = time.time()
        lapsed = endTime - startTime
        print("Image generated in " + str(lapsed) + " seconds.")
        print("Image saved to "+  imgDir + '/' + imageDetails['slug'] + '.img')
        os.system('mv /tmp/' + imageDetails['slug'] + '.img ' + imgDir + '/' )


        # update the 'jobservers' field to be us, so that the 
        data = {
          'slug': imageDetails['slug'],
          'needs_rebuild': False,
          'jobservers':[
            self.js.id,
          ]
        }
        response = self.js.session.patch(self.js.mprovURL + 'systemimages/' + str(data['slug']) + '/?addjs', data=json.dumps(data))
        with open(self.imageDir + '/image-versions', "r") as vfile:
          # grab the version info into a var
          try:
            imgVersions = json.loads(vfile.read())
          except:
            # if we have an error loading the version data, 
            # init it with no version data, we'll rebuild it 
            # in a moment.
            imgVersions = {}
        with open(self.imageDir + '/image-versions', "w") as vfile:
          imgVersions[imageDetails['slug']] = imageDetails['version']
          vfile.write(json.dumps(imgVersions))

        # Update our jobs with success or failure
        self.js.update_job_status(self.jobModule, 4, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      
      else: 
          print(f"Error handling job for {image} server returned {response.status_code} for request {self.js.mprovURL + query}")
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')