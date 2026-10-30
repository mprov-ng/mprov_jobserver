# This sub-module of the image-update job module
import os
from slugify import slugify
import json
import shutil
import re
import shlex

from mprov_jobserver.plugins.plugin import JobServerPlugin
from mprov_jobserver.log import get_logger

log = get_logger(__name__)

# Values from mPCC records go into shell commands here, so they're checked
# or quoted first: a value that's fine as data but odd as shell syntax
# mustn't change the command (#184).
# a kernel module name, as dracut and modinfo take it
MODULE_NAME = re.compile(r'[A-Za-z0-9_-]+')
# a distro version, as dnf's --releasever takes it (e.g. 9, 8.10)
DISTRO_VERSION = re.compile(r'[0-9]+(\.[0-9]+)*')

# the distro's own repos; the image gets its packages from the mPCC's repos
# instead, so these are disabled wherever the image has them
STOCK_REPOS = ['appstream', 'baseos', 'epel', 'extras', 'powertools', 'crb']

# where the image trusts the mPCC's CA from (update-ca-trust reads it) (#267)
MPCC_CA_ANCHOR = '/etc/pki/ca-trust/source/anchors/mprov-mpcc.pem'


def stock_repos_in(imgDir):
  """ The STOCK_REPOS that the image's yum.repos.d defines, in list order. """
  found = set()
  repoDir = f"{imgDir}/etc/yum.repos.d"
  if os.path.isdir(repoDir):
    for name in os.listdir(repoDir):
      if not name.endswith(".repo"):
        continue
      try:
        with open(f"{repoDir}/{name}") as repofile:
          for line in repofile:
            m = re.match(r'\s*\[([^\]]+)\]', line)
            if m:
              found.add(m.group(1).strip())
      except OSError:
        continue
  return [repo for repo in STOCK_REPOS if repo in found]


class UpdateImage(JobServerPlugin):
  imageDetails = None
  imageDir = ""
  jobid = None
  error = True # true by default, set to False if no errors in the run
  def load_config(self):
    # Do a GET to see if our ostype exists, if it does, patch it, if it doesn't, post it.
    # if 'repo-server' not in self.js.jobmodules:
    #   print("Configuration Error: you MUST run repo-server on repo-update/repo-delete nodes!")
    #   print("                   : repo-update/delete nodes are SOURCE nodes for repos!")
    #   print("                   : and need a way to serve repos! Job Module Halted!!!")
    #   sys.exit(1)
    #res = super().load_config()
    # Register our OS Type with the mPCC. ? Or should this be a fixture?
    ostypeurl = f"{self.js.mprovURL}ostypes/"
    data = { "slug": "rhel", "name": "Yum/DNF Based Linux" }
    response = self.js.session.post(ostypeurl, data=json.dumps(data))
    if response.status_code != 200:
      log.error("Error updating OS Type for module.")
    return True
  def realModules(self, imgDir, initial_mods):
    # dracut stops at a built-in or unknown module and leaves out every
    # driver listed after it (#206), so keep only the ones that are a module
    # for every kernel in the image.
    mods = []
    for mod in initial_mods.replace(',', ' ').split():
      if MODULE_NAME.fullmatch(mod):
        mods.append(mod)
      else:
        log.info(f"Leaving {mod!r} out of the initramfs: not a kernel module name.")
    try:
      kernels = os.listdir(f"{imgDir}/lib/modules")
    except OSError:
      # no kernel installed yet, so nothing to check against.
      return mods
    keep = []
    for mod in mods:
      if mod in keep:
        continue
      if all(os.system(f"chroot {shlex.quote(imgDir)} modinfo -k {shlex.quote(kernel)} -n {mod} >/dev/null 2>&1") == 0 for kernel in kernels):
        keep.append(mod)
      else:
        log.info(f"Leaving {mod} out of the initramfs: it's built in, or not a module of the image's kernel.")
    return keep

  def disable_stock_repos(self, imgDir, cmd):
    # cmd is the config-manager --disable command, without the repo ids
    repos = stock_repos_in(imgDir)
    if not repos:
      return
    if os.system(f"{cmd} {' '.join(repos)}"):
      # the build goes on: the mPCC's repos are still there to install from
      log.warning(f"unable to disable the image's stock repos {', '.join(repos)}.")

  def mpcc_ca(self, imgDir):
    '''
    Copy the job server's configured caBundle, the mPCC's CA, into the
    image: to the same absolute path, and to the trust anchors (#267).  The
    same path because the first installs run the host's dnf against the
    image's repo files, and dnf reads sslcacert as a host path; the chroot's
    dnf and the nodes then read the image's copy.  Returns that path, None
    if no caBundle is configured (the OS bundle is the host's own, and can't
    be copied in as the mPCC's CA), or False if the copy failed.
    '''
    ca = ((self.js.config_data or {}).get('global') or {}).get('caBundle')
    if not ca:
      return None
    ca = os.path.abspath(ca)
    try:
      for dest in (imgDir + ca, imgDir + MPCC_CA_ANCHOR):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(ca, dest)
        os.chmod(dest, 0o644)
    except OSError as err:
      log.error(f"unable to copy the mPCC's CA {ca} into the image: {err}")
      return False
    return ca

  def updateBootFiles(self):
    # update the dracut image one last time.
    imgDir = self.imageDir + '/' + self.imageDetails['slug']
    qimg = shlex.quote(imgDir)
    log.info(f"Regenerating initial ramdisk... ")
    cmd=f"chroot {qimg} dracut --regenerate-all -f --mdadmconf --force-add mdraid --add-drivers sd_mod --add-drivers \"{' '.join(self.realModules(imgDir, self.imageDetails['osdistro']['initial_mods']))}\""
    log.debug(cmd)
    if os.system(cmd):
      log.error("Unable to dracut a new initramfs.")
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return

  def handle_jobs(self):

    imageDetails = self.imageDetails
    imgDir = self.imageDir + '/' + self.imageDetails['slug']
    qimg = shlex.quote(imgDir)

    baseURL=imageDetails['osdistro']['baserepo']['repo_package_url']
    log.debug(baseURL)

    
    # create this image's dir.  Use the image['slug']
    # Don't os.chdir() into it: plugins are threads of one process, so that
    # would move every other plugin's working directory too (#129).  Every
    # path below is absolute.
    os.makedirs(imgDir, exist_ok=True)
#     print()
#     if imageDetails['osdistro']['baserepo']['managed']:
#       # locally managed repo, let's just generate a repo file and point it.
#       repofileContents=f"""
# [baseos]
# name= {imageDetails['osdistro']['name']}- mProv
# baseurl={self.js.mprovURL}osrepos/{imageDetails['osdistro']['baserepo']['id']}/
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
    
    # an https mPCC with a site or self-signed CA: the image trusts it too
    caBundle = self.mpcc_ca(imgDir)
    if caBundle is False:
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return

    # install the extra repository packages on the system image
    log.info("Installing extra repos...")
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
      log.debug(repo)
      log.debug("\nx")
      log.debug(repo['name'])
      if repo['managed'] :
        # we are managed, so point the repo to the mPCC
        repourl = f"{self.js.mprovURL}osrepos/{repo['id']}/"
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
          if repo['managed'] and caBundle:
            # the host's dnf (with --installroot) and the image's dnf both
            # find the mPCC's CA at this path (#267)
            repofile.write(f"sslcacert={caBundle}\n")
      
    ver= str(imageDetails['osdistro']['version'])
    if not DISTRO_VERSION.fullmatch(ver):
      log.error(f"the OS distro's version {ver!r} isn't a version number (like 9 or 8.10).")
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return

    # disable the stock repos an existing image already has; a new image has
    # none yet, and asking dnf to disable a missing repo logs "Error:" (#211).
    # Done again after the main install.
    self.disable_stock_repos(imgDir, f"dnf -y --installroot={qimg} --releasever={ver} config-manager --disable")
    # run a clean on yum, in the image only: a plain `dnf clean all` here
    # wiped the job server's own dnf cache on every build (#164)
    if os.system('dnf --installroot=' + qimg + ' -y clean all'):
      log.error("unable to clear all dnf metadata.")
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return
    # install gpg keys for the distro into the installroot
    log.info('dnf -y --installroot=' + qimg + ' --releasever=' + ver  + ' --nogpgcheck install "*-gpg-keys"')
    if os.system('dnf -y --installroot=' + qimg + ' --releasever=' + ver  + ' --nogpgcheck install "*-gpg-keys"'):
      log.error("unable to generate image filesystem.")
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return 
    # print(f'chroot {imgDir} rpm --import "/etc/pki/rpm-gpg/*"')
    # if os.system(f'chroot {imgDir} rpm --import "/etc/pki/rpm-gpg/*"'):
    #   print("Error: unable to generate image filesystem.")
    #   self.js.update_job_status(self.jobModule, 3, jobquery='jobserver=' + str(self.js.id) + '&status=2')
    #   self.threadOk = False
    #   return 
      
    # build the filesystem.
    log.info('dnf -y --installroot=' + qimg + ' --releasever=' + ver  + ' --nogpgcheck groupinstall \'Minimal Install\'')
    if os.system('dnf -y --installroot=' + qimg + ' --releasever=' + ver  + ' --nogpgcheck groupinstall \'Minimal Install\''):
      # if this fails, lets try something slightly different...
      log.info(f'chroot {qimg} dnf -y --releasever=' + ver  + ' --nogpgcheck groupinstall \'Minimal Install\'')
      if os.system(f'chroot {qimg} dnf -y --releasever=' + ver  + ' --nogpgcheck groupinstall \'Minimal Install\''):

        log.error("unable to generate image filesystem.")
        self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
        self.threadOk = False
        return
    
    # ca-certificates is in now: add the mPCC's CA to the image's bundle,
    # which the job server and node scripts in the image use (#267)
    if caBundle and os.system(f"chroot {qimg} update-ca-trust"):
      log.error("unable to add the mPCC's CA to the image's trust store.")
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return

    # the main install brought in the stock repo files; disable them again
    self.disable_stock_repos(imgDir, f"chroot {qimg} dnf -y config-manager --disable")

    # install and copy the kernel image to the image root
    if float(imageDetails['osdistro']['version']) >= 9 :
      pythonpkgs = " python3 python3-devel python3-pyyaml python3-devel python3-requests python3-jinja2.noarch glibc-langpack-en" 
      
    else:
      pythonpkgs = " python38 python38-pip python38-devel python38-pyyaml python38-devel python38-requests python38-jinja2.noarch glibc-langpack-en"
      

    log.info(f'chroot {qimg} dnf -y  --releasever=' + ver  + f'  install kernel wget jq parted-devel gcc grub2 mdadm rsync grub2-efi-x64 grub2-efi-x64-modules dosfstools ipmitool python3-dnf-plugin-versionlock.noarch' + pythonpkgs)
    if os.system(f'chroot {qimg}  dnf -y  --releasever=' + ver  + f'  install kernel wget jq parted-devel gcc grub2 mdadm rsync grub2-efi-x64 grub2-efi-x64-modules dosfstools ipmitool python3-dnf-plugin-versionlock.noarch' + pythonpkgs):
      # if this fails, let's try to rebuild the rpm db
      if os.system(f'chroot {qimg} rpm --rebuilddb'):
        log.error("unable to install required packages into image filesystem")
        self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
        self.threadOk = False
        return
      else:
        # we succeeded so try to run the original command again.
        if os.system(f'chroot {qimg}  dnf -y  --releasever=' + ver  + f'  install kernel wget jq parted-devel gcc grub2 mdadm rsync grub2-efi-x64 grub2-efi-x64-modules dosfstools ipmitool python3-dnf-plugin-versionlock.noarch' + pythonpkgs):
          # still failed, uh... let's die.
          log.error("Unable to use dnf for some reason.  Bailing.")
          log.error("No image generated.")
          self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          self.threadOk = False
          return

    # set up a version lock on the kernel
    os.system(f'chroot {qimg} dnf -y versionlock kernel*')

    shutil.copyfile("/etc/resolv.conf", f"{imgDir}/etc/resolv.conf")
    
    if float(imageDetails['osdistro']['version']) < 9 :
      os.system(f'chroot {qimg} alternatives --set python3 /usr/bin/python3.8')
      os.system(f'chroot {qimg} alternatives --set python /usr/bin/python3.8')
      os.system(f'chroot {qimg} alternatives --set pip3 /usr/bin/pip3.8')

    # pip install some stuff
    if os.system(f"chroot {qimg} pip3 install sh pyparted==3.11.7 requests"):
      log.error("uanble to install pip packages into image filesystem")
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

    if os.system('ls -1 ' + qimg + '/boot/vmlinuz-* | grep -v rescue | sort -r | head -n1 | xargs -I{} ln -sf {} ' + qimg + '/' + imageDetails['slug'] + '.vmlinuz'): 
      log.error("unable to copy kernel image.")
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return

    if os.system('ls -1 ' + qimg + '/boot/initramfs-* | grep -v rescue | sort -r | head -n1 | xargs -I{} ln -sf {} ' + qimg + '/' + imageDetails['slug'] + '.initramfs'): 
      log.error("unable to copy initramfs.")
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return

    if os.system('rm -f ' + qimg + '/dev/ram0; mknod -m 600 ' + qimg + '/dev/ram0 b 1 0'):
      log.error('trying to make /dev/ram0')
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return
    
    if os.system('rm -f ' + qimg + '/dev/initrd; mknod -m 400 ' + qimg + '/dev/initrd b 1 250'):
      log.error('trying to make /dev/ram0')
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return
      
    log.info(f"Regenerating initial ramdisk... ")
    cmd=f"chroot {qimg} dracut --regenerate-all -f --mdadmconf --force-add mdraid --add-drivers sd_mod --add-drivers \"{' '.join(self.realModules(imgDir, imageDetails['osdistro']['initial_mods']))}\""
    log.debug(cmd)
    if os.system(cmd):
      log.error("Unable to dracut a new initramfs.")
      self.js.update_job_status(self.jobModule, 3, jobid=self.jobid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
      self.threadOk = False
      return
