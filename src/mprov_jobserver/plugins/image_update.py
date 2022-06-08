import time
from .plugin import JobServerPlugin
from time import sleep
import os
import sys
import json
from urllib.parse import urlparse
import yaml
from jinja2 import Environment, PackageLoader, select_autoescape

jenv = Environment(
    loader=PackageLoader("mprov_jobserver"),
    autoescape=select_autoescape()
)



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

        # install and copy the kernel image to the image root
        if os.system('dnf -y --installroot=' + imgDir + ' --releasever=' + str(imageDetails['osdistro']['version'])  + ' install kernel python38 python38-pyyaml python38-requests python38-jinja2.noarch jq'):
          print("Error uanble to install kernel into image filesystem")
          self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          return

        if os.system('ls -t ' + imgDir + '/boot/vmlinuz-* | grep -v rescue | head -n1 | xargs -I{} cp {} ' + imgDir + '/' + image + '.vmlinuz'): 
          print("Error: unable to copy kernel image.")
          self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          return

        if os.system('ls -t ' + imgDir + '/boot/initramfs-* | grep -v rescue | head -n1 | xargs -I{} cp {} ' + imgDir + '/' + image + '.initramfs'): 
          print("Error: unable to copy initramfs.")
          self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          return

        if os.system('rm -f ' + imgDir + '/dev/ram0; mknod -m 600 ' + imgDir + '/dev/ram0 b 1 0'):
          print('Error: trying to make /dev/ram0')
          self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          return
        
        if os.system('rm -f ' + imgDir + '/dev/initrd; mknod -m 400 ' + imgDir + '/dev/initrd b 1 250'):
          print('Error: trying to make /dev/ram0')
          self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          return
          
        
        # run image-gen scripts.
        
        # grab a copy of the jobserver wheel from the main mprov server
        # TODO: Change this to a pip install mprov_jobserver command after publication

        if os.system("chroot " + imgDir + " pip3 install mprov_jobserver --force-reinstall"):
          print("Error: Unable to install mprov_jobserver python module.")
          return

        # check if the imagDir + /tmp/mprov exists and create it if not
        os.makedirs(imgDir + '/tmp/mprov/plugins/', exist_ok=True)
        
        # if os.system('rsync -ar ' + str(scriptDir) + '/../../mprov_jobserver ' + imgDir + '/root/mprov/'):
        #   print("Error: Unable to copy a jobserver to the image.")
        #   self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
        #   return
        
        # create a config file to /tmp for our script-runner instance.
        localConfig = [dict()]
        localConfig[0]['global'] = self.js.config_data['global']
        print(localConfig[0]['global']['jobmodules'])
        localConfig[0]['global']['jobmodules'] = ['script-runner']
        localConfig[0]['global']['runonce'] = True
        # print(imgDir + '/tmp/mprov/jobserver.yaml')
        with open(imgDir + '/tmp/mprov/jobserver.yaml',"w") as confFile:
          yaml.dump(localConfig, confFile)
          confFile.write("\n- !include plugins/*.yaml")

        data = {
          'imgDir': imgDir,
          'image': image,
        }
        # now let's run our script-runner shell script.
        with open(os.open(imgDir + '/tmp/mprov/script-runner.sh',os.O_CREAT | os.O_WRONLY, 0o755) , 'w') as conf:
            conf.write(jenv.get_template('image-update/script-runner.sh').render(data))
        # now let's run our script-runner shell script.
        with open(os.open(imgDir + '/tmp/mprov/plugins/script-runner.yaml',os.O_CREAT | os.O_WRONLY, 0o755) , 'w') as conf:
            conf.write(jenv.get_template('image-update/script-runner.yaml.j2').render(data))

        if os.system(imgDir + '/tmp/mprov/script-runner.sh'):
          print("Error while running image-gen scripts via job server..")
        

        # package the filesystem into an initramfs
        print("Building " + os.getcwd() + "/" + imageDetails['slug'] + '.img')
        startTime=time.time()
        os.system('rm -f ' + imgDir + '/' + imageDetails['slug'] + '.img')
        if os.system('find .  -depth -print| cpio -H newc --quiet -oD ' + imgDir + '  | gzip -1 -c > /tmp/' + imageDetails['slug'] + '.img'):
          print("Error: unable to create initramfs")
          self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          return
        endTime = time.time()
        lapsed = endTime - startTime
        print("Image generated in " + str(lapsed) + " seconds.")
        print("Image saved to "+ os.getcwd() + "/" + imageDetails['slug'] + '.img')
        os.system('mv /tmp/' + imageDetails['slug'] + '.img ' + imgDir + '/' )


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
    self.js.update_job_status(self.jobModule, 4, jobquery='jobserver=' + str(self.js.id) + '&status=2')