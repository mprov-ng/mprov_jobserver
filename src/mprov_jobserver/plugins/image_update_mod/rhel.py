# This sub-module of the image-update job module
import os
from urllib.parse import urlparse
from slugify import slugify
import json
import distro
import shutil

from mprov_jobserver.plugins.plugin import JobServerPlugin

class UpdateImage(JobServerPlugin):
  imageDetails = None
  imageDir = ""
  error = True # true by default, set to False if no errors in the run
  def load_config(self):
    # Do a GET to see if our ostype exists, if it does, patch it, if it doesn't, post it.
    # if 'repo-server' not in self.js.jobmodules:
    #   print("Configuration Error: you MUST run repo-server on repo-upate/repo-delete nodes!")
    #   print("                   : repo-update/delete nodes are SOURCE nodes for repos!")
    #   print("                   : and need a way to serve repos! Job Module Halted!!!")
    #   sys.exit(1)
    #res = super().load_config()
    # Register our OS Type with the mPCC. ? Or should this be a fixture?
    ostypeurl = f"{self.js.mprovURL}/ostypes/"
    data = { "slug": "rhel", "name": "Yum/DNF Based Linux" }
    response = self.js.session.post(ostypeurl, data=json.dumps(data))
    if response.status_code != 200:
      print("Error: Error updating OS Type for module.")
    return True
    
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
#     print()
#     if imageDetails['osdistro']['baserepo']['managed']:
#       # locally managed repo, let's just generate a repo file and point it.
#       repofileContents=f"""
# [baseos]
# name= {imageDetails['osdistro']['name']}- mProv
# baseurl={self.js.mprovURL}/osrepos/{imageDetails['osdistro']['baserepo']['id']}/
# gpgcheck=0
# enabled=1


#       """

#       os.makedirs(f"{imgDir}/etc/yum.repos.d/", exist_ok=True)
#       nameSlug = slugify(imageDetails['osdistro']['name'])
#       with open(f"{imgDir}/etc/yum.repos.d/{nameSlug}.repo", "w") as repofile:
#         repofile.write(repofileContents)
#     else:
#       print("Grabbing os repo package: " + baseURL)
#       if os.system('wget -O ' + file + ' ' + baseURL ):
#         print("Error: unable to get repo package: " + baseURL)
#         self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
#         self.threadOk = False
#         return

#       # force the RPM to unpack to our image dir.
#       print("Unpacking RPM to " + imgDir)
      
#       if os.system('rpm2cpio < ' + file + ' | cpio -D ' + imgDir + ' -id'):
#         print("Error: unable to extract repo package: " + file + ' into ' + imgDir)
#         self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
#         self.threadOk = False
#         return
    ########
    
    # install the extra repository packages on the system image
    print("Installing extra repos...")
    repostr = ""
    repos = []

    # pull in the repos from the image.
    if len(imageDetails['osrepos']) > 0:
      for repo in imageDetails['osrepos']:
        if type(repo) is dict:
          repos.append(repo)

    # pull in the OS repos from the distro
    if len(imageDetails['osdistro']['osrepos']) > 0:
      for repo in imageDetails['osdistro']['osrepos']:
        if type(repo) is dict:
          repos.append(repo)

    # pull in the BaseOS repo from the distro
    if type(imageDetails['osdistro']['baserepo']) is dict:
      repos.append(imageDetails['osdistro']['baserepo']) 
    
    # pull in extra repos from the distro.
    if "extrarepos" in imageDetails['osdistro']:
      for repo in imageDetails['osdistro']['extrarepos']:
        if type(repo) is dict:
          repos.append(repo)



    # now let's loop through the repos, if it's not managed, it should be a repo URL
    # if it is managed, create the yum.conf for it in /etc/yum.repos.d/
    for repo in repos:
      if type(repo) is not dict:
        continue
      print(repo)
      print("\nx")
      print(repo['name'])
      if repo['managed'] :
        # we are managed, so point the repo to the mPCC
        repourl = f"{self.js.mprovURL}/osrepos/{repo['id']}/"
      else:
        # otherwise, just set the url to the url in the repo.
        repourl = repo['repo_package_url']
      
      # now make the repo file
      repoid = slugify(repo['name'])
      os.makedirs(f"{imgDir}/etc/yum.repos.d", exist_ok=True)
      with open(os.open(f"{imgDir}/etc/yum.repos.d/{repoid}-mprov.repo", os.O_CREAT | os.O_WRONLY, 0o755) , 'w') as repofile:
          repofile.write(f"[{repoid}-mprov]\n")
          repofile.write(f"name={repo['name']} - mProv\n")
          repofile.write(f"baseurl=\"{repourl}\"\n")
          repofile.write(f"enabled=1\n")
          repofile.write(f"gpgcheck=0\n")
      
    # here we are going to disable all the default repos, we'll do this again after the main install.
    disable_repos = ['appstream', 'baseos', 'epel', 'extras', 'powertools', 'crb']
    for drepo in disable_repos:
      try:
        os.system(f"dnf -y --installroot {imgDir} config-manager --disable {drepo}")
      except:
        pass

    ######

    ver= str(imageDetails['osdistro']['version'])
    # run a clean on yum
    if os.system('dnf -y clean all'):
      print("Error: unable to clear all dnf metadata.")
      self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return
    if os.system('dnf --installroot=' + imgDir + ' -y clean all'):
      print("Error: unable to clear all dnf metadata.")
      self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return
    # install gpg keys for the distro into the installroot
    print('dnf -y --installroot=' + imgDir + ' --releasever=' + str(imageDetails['osdistro']['version'])  + ' --nogpgcheck install "*-gpg-keys"')
    if os.system('dnf -y --installroot=' + imgDir + ' --releasever=' + str(imageDetails['osdistro']['version'])  + ' --nogpgcheck install "*-gpg-keys"'):
      print("Error: unable to genergate image filesystem.")
      self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return 
    # print(f'chroot {imgDir} rpm --import "/etc/pki/rpm-gpg/*"')
    # if os.system(f'chroot {imgDir} rpm --import "/etc/pki/rpm-gpg/*"'):
    #   print("Error: unable to genergate image filesystem.")
    #   self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
    #   self.threadOk = False
    #   return 
      
    # build the filesystem.
    print('dnf -y --installroot=' + imgDir + ' --releasever=' + str(imageDetails['osdistro']['version'])  + ' --nogpgcheck groupinstall \'Minimal Install\'')
    if os.system('dnf -y --installroot=' + imgDir + ' --releasever=' + str(imageDetails['osdistro']['version'])  + ' --nogpgcheck groupinstall \'Minimal Install\''):
      print("Error: unable to genergate image filesystem.")
      self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return
    
    # here we are going to disable all the default repos again just to make sure.
    disable_repos = ['appstream', 'baseos', 'epel', 'extras', 'powertools', 'crb']
    for drepo in disable_repos:
      try:
        os.system(f"chroot {imgDir} dnf -y --disable config-manager {drepo}")
      except:
        pass

    # install and copy the kernel image to the image root
    if float(imageDetails['osdistro']['version']) >= 9 :
      pythonpkgs = " python3 python3-devel python3-pyyaml python3-devel python3-requests python3-jinja2.noarch"
      
    else:
      pythonpkgs = " python38 python38-pip python38-devel python38-pyyaml python38-devel python38-requests python38-jinja2.noarch"
      

    print('dnf -y --installroot=' + imgDir + ' --releasever=' + str(imageDetails['osdistro']['version'])  + f'  install kernel wget jq parted-devel gcc grub2 mdadm rsync grub2-efi-x64 grub2-efi-x64-modules dosfstools ipmitool python3-dnf-plugin-versionlock.noarch' + pythonpkgs)
    if os.system('dnf -y --installroot=' + imgDir + ' --releasever=' + str(imageDetails['osdistro']['version'])  + f'  install kernel wget jq parted-devel gcc grub2 mdadm rsync grub2-efi-x64 grub2-efi-x64-modules dosfstools ipmitool python3-dnf-plugin-versionlock.noarch' + pythonpkgs):
      print("Error unable to install required packages into image filesystem")
      self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return

    # set up a version lock on the kernel
    os.system(f'chroot {imgDir} dnf -y versionlock kernel*')

    shutil.copyfile("/etc/resolv.conf", f"{imgDir}/etc/resolv.conf")
    
    if float(imageDetails['osdistro']['version']) < 9 :
      os.system(f'chroot {imgDir} alternatives --set python3 /usr/bin/python3.8')
      os.system(f'chroot {imgDir} alternatives --set python /usr/bin/python3.8')
      os.system(f'chroot {imgDir} alternatives --set pip3 /usr/bin/pip3.8')

    # pip install some stuff
    if os.system(f"chroot {imgDir} pip3 install sh pyparted==3.11.7 requests"):
      print("Error uanble to install pip packages into image filesystem")
      self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return

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

    if os.system('ls -1 ' + imgDir + '/boot/vmlinuz-* | grep -v rescue | sort -r | head -n1 | xargs -I{} ln -sf {} ' + imgDir + '/' + imageDetails['slug'] + '.vmlinuz'): 
      print("Error: unable to copy kernel image.")
      self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return

    if os.system('ls -1 ' + imgDir + '/boot/initramfs-* | grep -v rescue | sort -r | head -n1 | xargs -I{} ln -sf {} ' + imgDir + '/' + imageDetails['slug'] + '.initramfs'): 
      print("Error: unable to copy initramfs.")
      self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return

    if os.system('rm -f ' + imgDir + '/dev/ram0; mknod -m 600 ' + imgDir + '/dev/ram0 b 1 0'):
      print('Error: trying to make /dev/ram0')
      self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return
    
    if os.system('rm -f ' + imgDir + '/dev/initrd; mknod -m 400 ' + imgDir + '/dev/initrd b 1 250'):
      print('Error: trying to make /dev/ram0')
      self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return
      
    print(f"Regenerating initial ramdisk... ")
    cmd=f"chroot {imgDir} dracut --regenerate-all -f --mdadmconf --force-add mdraid --add-drivers \"{imageDetails['osdistro']['initial_mods'].replace(',',' ')}\""
    print(cmd)
    if os.system(cmd):
      print("Error: Unable to dracut a new initramfs.")
      self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return