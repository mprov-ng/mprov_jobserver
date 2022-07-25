# This sub-module of the image-update job module
import json
import os
import time
from urllib.parse import urlparse

from jinja2 import Environment, PackageLoader, select_autoescape

jenv = Environment(
    loader=PackageLoader("mprov_jobserver"),
    autoescape=select_autoescape()
)


import yaml
from mprov_jobserver.plugins.plugin import JobServerPlugin

class UpdateImage(JobServerPlugin):
  imageDetails = None
  imageDir = ""
  def load_config(self):
    # Do a GET to see if our ostype exists, if it does, patch it, if it doesn't, post it.
    # if 'repo-server' not in self.js.jobmodules:
    #   print("Configuration Error: you MUST run repo-server on repo-upate/repo-delete nodes!")
    #   print("                   : repo-update/delete nodes are SOURCE nodes for repos!")
    #   print("                   : and need a way to serve repos! Job Module Halted!!!")
    #   sys.exit(1)
    return super().load_config()

  def handle_jobs(self):

    imageDetails = self.imageDetails

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

    ver= str(imageDetails['osdistro']['version'])
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

    # install the extra repository packages on the system image
    print("Installing extra repos...")
    repostr = ""
    if len(imageDetails['osrepos']) > 0:
      for repo in imageDetails['osrepos']:
        repostr += repo['repo_package_url']
    if len(imageDetails['osdistro']['osrepos']) > 0:
      for repo in imageDetails['osdistro']['osrepos']:
        repostr += repo['repo_package_url']
      
    if os.system(f'dnf -y --installroot={imgDir} --releasever={ver} install {repostr}'):
        print("Warn error installing extra repos")

    if os.path.exists(imgDir + '/' + imageDetails['slug'] + '.vmlinuz'):
      try:
        os.remove(imgDir + '/' + imageDetails['slug'] + '.vmlinuz')
      except:
        pass
    
    if os.path.exists(imgDir + '/' + imageDetails['slug'] + '.initramfs'):
      try:
        os.remove(imgDir + '/' + imageDetails['slug'] + '.initramfs')
      except:
        pass

    if os.system('ls -t ' + imgDir + '/boot/vmlinuz-* | grep -v rescue | head -n1 | xargs -I{} ln -sf {} ' + imgDir + '/' + imageDetails['slug'] + '.vmlinuz'): 
      print("Error: unable to copy kernel image.")
      self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      return

    if os.system('ls -t ' + imgDir + '/boot/initramfs-* | grep -v rescue | head -n1 | xargs -I{} ln -sf {} ' + imgDir + '/' + imageDetails['slug'] + '.initramfs'): 
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

    if os.system("chroot " + imgDir + " pip3 --no-cache-dir install mprov_jobserver --force-reinstall"):
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
      'image': imageDetails['slug'],
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
    response = self.js.session.patch(self.js.mprovURL + 'systemimages/' + str(data['slug']) + '/', data=json.dumps(data))

    # Update our jobs with success or failure
    self.js.update_job_status(self.jobModule, 4, jobquery='jobserver=' + str(self.js.id) + '&status=2')
  
