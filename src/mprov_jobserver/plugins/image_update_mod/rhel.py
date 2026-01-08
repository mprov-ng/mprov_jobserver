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
  jobid = None
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

    baseURL=imageDetails['osdistro']['baseurl'] 

    imgDir = self.imageDir + '/' + imageDetails['slug']
    # create this image's dir.  Use the image['slug']
    os.makedirs(imgDir, exist_ok=True)
    os.chdir(imgDir)

    url = urlparse(baseURL)
    
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

    # let's see what we have.
    
    # check if the last character is a slash, if so, it's a repo URL
    if url.path[-1] == '/':
      print("Base URL is a repo URL")
      self.do_url_repo_install(imageDetails, imgDir, baseURL)
      
    elif url.path.endswith('.qcow2'):
      print("Base URL is a qcow2 image")
      self.do_url_image_install(imageDetails, imgDir, baseURL, 'qcow2')
      
    elif url.path.endswith('.img'):
      print("Base URL is a raw image")
      self.do_url_image_install(imageDetails, imgDir, baseURL, 'raw')
      return
    elif url.path.endswith('.tar.gz') or url.path.endswith('.tgz'):
      print("Base URL is a tarball")
      self.do_url_image_install(imageDetails, imgDir, baseURL, 'tar.gz')
      return
    else:
      print("Error: Unable to determine base URL type.")
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return
    
    self.finish_image_setup(imageDetails, imgDir,repos)
    

    ######
  def do_url_repo_install(self, imageDetails, imgDir, baseURL):
    ver= str(imageDetails['osdistro']['version'])
    # run a clean on yum
    if os.system('dnf -y clean all'):
      print("Error: unable to clear all dnf metadata.")
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return
    if os.system('dnf --installroot=' + imgDir + ' -y clean all'):
      print("Error: unable to clear all dnf metadata.")
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return
    # install gpg keys for the distro into the installroot
    print('dnf -y --installroot=' + imgDir + ' --releasever=' + str(imageDetails['osdistro']['version'])  + ' --nogpgcheck install "*-gpg-keys"')
    if os.system('dnf -y --installroot=' + imgDir + ' --releasever=' + str(imageDetails['osdistro']['version'])  + ' --nogpgcheck install "*-gpg-keys"'):
      print("Error: unable to genergate image filesystem.")
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return 
      
    # build the filesystem.
    print('dnf -y --installroot=' + imgDir + ' --releasever=' + str(imageDetails['osdistro']['version'])  + ' --nogpgcheck groupinstall \'Minimal Install\'')
    if os.system('dnf -y --installroot=' + imgDir + ' --releasever=' + str(imageDetails['osdistro']['version'])  + ' --nogpgcheck groupinstall \'Minimal Install\''):
      print("Error: unable to genergate image filesystem.")
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return

  def do_url_image_install(self, imageDetails, imgDir, baseURL, imagetype):
    # download the image file
    imgfile = imgDir + '/baseimage.' + imagetype
    print(f"Downloading base image from {baseURL} to {imgfile}...")
    if os.system(f"wget -c -O {imgfile} {baseURL}"):
      print("Error: unable to download base image.")
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return

    # extract the image file into the imgDir
    if imagetype == 'qcow2':
      print(f"Extracting qcow2 image {imgfile} to {imgDir}...")
      if os.system(f"qemu-img convert -O raw {imgfile} {imgDir}/baseimage.raw"):
        print("Error: unable to convert qcow2 image to raw.")
        self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
        self.threadOk = False
        return
      os.remove(imgfile)
      imgfile = imgDir + '/baseimage.raw'

    if imagetype == 'tar.gz':
      print(f"Extracting tarball image {imgfile} to {imgDir}...")
      if os.system(f"tar -xzf {imgfile} -C {imgDir}"):
        print("Error: unable to extract tarball image.")
        self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
        self.threadOk = False
        return
      os.remove(imgfile)
      return

    # for raw images, we will use virt-copy-out to extract the filesystem.
    print(f"Extracting raw image {imgfile} to {imgDir}...")
    os.environ['LIBGUESTFS_BACKEND'] = 'direct'
    # if os.system(f"/usr/sbin/virtqemud -d"):
    #   print("Error: unable to start virtqemud for virt-copy-out.")
    #   self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
    #   self.threadOk = False
    #   return
    if os.system(f"virt-copy-out -a {imgfile} / {imgDir}/"):
      print("Error: unable to extract raw image.")
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return
    # os.system(f"pklil virtqemud")


  def finish_image_setup(self, imageDetails, imgDir,repos):   
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

    # disable the default repos if we are managed.
    repos = ['appstream', 'baseos', 'epel', 'extras', 'powertools', 'crb']
    for srepo in repos:
      if imageDetails['osdistro']['managed'] :   
        # here we are going to disable all the default repos, we'll do this again after the main install.
        try:
          print("Disabling repo: " + srepo)
          os.system(f"dnf -y --installroot {imgDir} config-manager --disable {srepo}")
        except:
          pass
      else: 
        # here we are going to disable all the default repos, we'll do this again after the main install.
        try:
          print("Enabling repo: " + srepo)
          os.system(f"dnf -y --installroot {imgDir} config-manager --enable {srepo}")
        except:
          pass   
  # bind mount proc and sys into the image.
    try: 
      os.system(f"mount -o bind /proc {imgDir}/proc")
      os.system(f"mount -o bind /sys {imgDir}/sys")
    except:
      print("Error: unable to bind mount /proc and /sys")
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return
    
    # install and copy the kernel image to the image root
    if float(imageDetails['osdistro']['version']) >= 9 :
      pythonpkgs = " python3 python3-devel python3-pyyaml python3-devel python3-requests python3-jinja2.noarch"
      
    else:
      pythonpkgs = " python38 python38-pip python38-devel python38-pyyaml python38-devel python38-requests python38-jinja2.noarch"
      
    if os.system(f"chroot {imgDir} dnf -y reinstall dnf* *gpg* --nogpgcheck"):
      print("Error unable to reinstall dnf in image filesystem")
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return
    print(f'chroot {imgDir} dnf -y  --releasever=' + str(imageDetails['osdistro']['version'])  + f'  install kernel wget jq parted-devel gcc grub2 mdadm rsync grub2-efi-x64 grub2-efi-x64-modules dosfstools ipmitool python3-dnf-plugin-versionlock.noarch' + pythonpkgs)
    if os.system(f'chroot {imgDir}  dnf -y  --releasever=' + str(imageDetails['osdistro']['version'])  + f'  install kernel wget jq parted-devel gcc grub2 mdadm rsync grub2-efi-x64 grub2-efi-x64-modules dosfstools ipmitool python3-dnf-plugin-versionlock.noarch' + pythonpkgs):
      print("Error unable to install required packages into image filesystem")
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
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
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
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
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return

    if os.system('ls -1 ' + imgDir + '/boot/initramfs-* | grep -v rescue | sort -r | head -n1 | xargs -I{} ln -sf {} ' + imgDir + '/' + imageDetails['slug'] + '.initramfs'): 
      print("Error: unable to copy initramfs.")
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return

    if os.system('rm -f ' + imgDir + '/dev/ram0; mknod -m 600 ' + imgDir + '/dev/ram0 b 1 0'):
      print('Error: trying to make /dev/ram0')
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return
    
    if os.system('rm -f ' + imgDir + '/dev/initrd; mknod -m 400 ' + imgDir + '/dev/initrd b 1 250'):
      print('Error: trying to make /dev/ram0')
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return
      
    print(f"Regenerating initial ramdisk... ")
    cmd=f"chroot {imgDir} dracut --regenerate-all -f --mdadmconf --force-add mdraid --add-drivers \"{imageDetails['osdistro']['initial_mods'].replace(',',' ')}\""
    print(cmd)
    if os.system(cmd):
      print("Error: Unable to dracut a new initramfs.")
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return

    try: 
      os.system(f"umount /proc {imgDir}/proc")
      os.system(f"umount /sys {imgDir}/sys")
    except:
      print("Error: unable to bind umount /proc and /sys")
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return
