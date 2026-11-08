import importlib
from .plugin import JobServerPlugin, mounts_under, addjs_version
import os
import stat
import subprocess
import sys
import tempfile
import json
import shlex
import shutil
import signal
import tarfile
import time
import urllib.parse

import requests

import yaml
from jinja2 import Environment, PackageLoader, select_autoescape

from os.path import dirname, basename, isfile, join
import glob
from mprov_jobserver.log import get_logger
from mprov_jobserver import identity as ident
from mprov_jobserver.version import jobserver_version

log = get_logger(__name__)

# where an image build's 2-hour credential goes in the image, as the script
# runner in it sees it, and the job server settings it doesn't get
# (mprov_control_center#381)
BUILD_TLS_DIR = '/tmp/mprov/tls'
BUILD_DROPPED_SETTINGS = ('apikey', 'enrollToken', 'enrollTokenFile', 'caFingerprint', 'nodeTlsDir')

# Where the job server image keeps the node-image bundle (#309), and where
# a bundle goes in a node image.  BUNDLE_LINK is the path script-runner.sh
# and the systemd units call.
BUNDLE_DIR = "/usr/share/mprov"
BUNDLE_PREFIX = "/usr/libexec/mprov"
BUNDLE_LINK = "/usr/local/bin/mprov_jobserver"
RELEASES_URL = "https://github.com/mprov-ng/mprov_jobserver/releases"


def bundle_name(version):
  return f"mprov-jobserver-{version}-x86_64.tar.gz"


def inside(imgDir, path):
  """
  True if path, with every symlink in it resolved, is still in imgDir.  The
  image tree comes from an image we unpacked, which may have put a symlink
  where we're about to write, pointing out at the host (#169).
  """
  root = os.path.realpath(imgDir)
  return os.path.realpath(path).startswith(root + os.sep)

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
  # How long the image-gen script runner may take before the build is
  # failed, in seconds (#304).  Builds are legitimately long -- a big
  # package set or an ansible run is nothing like the handful of minutes a
  # small image takes -- so this is a safety net, not a schedule.  0 means
  # no limit, for a site whose builds really do run longer than this.
  scriptTimeout = 7200
  # How long a timed-out script runner gets to stop on a TERM before it is
  # killed outright.  A KILL runs no traps, so it costs us the script's own
  # unmount and we do it ourselves instead; give it a moment first.
  killGrace = 30
  # No longer used: the job server goes into images as a bundle, not with
  # pip (#309).  Kept so a 0.6 config that sets it (#317) doesn't log an
  # unknown-setting error at every start.
  pipTimeout = 1800
  # The node-image bundle to unpack into each image (#309).  Empty means the
  # one the job server image carries, in BUNDLE_DIR.  A job server installed
  # with pip has none, so it sets this to a downloaded release tarball.
  pythonBundle = ""
  # How long uninstalling a 0.6-era pip-installed job server from an image
  # being rebuilt may take.
  pipUninstallTimeout = 300

  # override default load config because we have to check that mprov-webserver is also enabled.
  def load_config(self):
    if 'mprov-webserver' not in self.js.jobmodules:
      log.error("Configuration error: you MUST run mprov-webserver on image-update/image-delete nodes!")
      log.error("image-update/delete nodes are SOURCE nodes for images!")
      log.error("and need a way to serve images! Job Module Halted!!!")
      sys.exit(1)
    # run load config on each sub_module
    modules = glob.glob(join(dirname(__file__) + "/image_update_mod", "*.py"))
    mods = [ basename(f)[:-3] for f in modules if isfile(f) and not f.endswith('__init__.py')]
    for name in mods:
      try:
        mod = importlib.import_module(f".image_update_mod.{name}", "mprov_jobserver.plugins")
        update_klass = getattr(mod, 'UpdateImage')
      except BaseException as err:
        log.error(f"Unable to import Image Update Module for {name}.")
        log.error(f"Exception: {err=}, {type(err)=}")
        continue

      try:
        image_update = update_klass(self.js)
      except Exception as err:
        log.error(f"Unable to instantiate UpdateImage class on {name}: {err}")
        continue
      image_update.load_config()
    return super().load_config()

  def addAsHost(self, image, version):
    # tell the mPCC we host this image too, at the version we have.  Only our
    # id: the mPCC merges it into the image's current hosts.  Sending the list
    # from our GET put back every host a rebuild had cleared since (#248,
    # mprov_control_center#396).
    # no needs_rebuild: we only add ourselves when the image doesn't need
    # one, and sending False would clear a rebuild asked for since our GET (#226).
    # False if the mPCC says the image has moved past our version (only an
    # mPCC that checks it, see addjs_version()); we're not a host then.
    data = {
      'slug': image,
      'jobservers': [self.js.id],
      **addjs_version(self.js, version),
    }
    response = self.js.session.patch(self.js.mprovURL + 'systemimages/' + str(data['slug']) + '/?addjs', data=json.dumps(data))
    if response.status_code == 409:
      log.warning(f"not hosting image {image}: the mPCC has it past our version {version}; we'll fetch it again.")
      return False
    if response.status_code <= 199 or response.status_code >= 300:
      log.error(f"mPCC wouldn't let us add ourselves, status code: {response.status_code}")
    return True

  def publishedFiles(self, imgDir, slug):
    # The image, kernel and initramfs this build just put in imgDir, by path
    # and the (device, inode) of the file itself (the kernel and initramfs
    # are symlinks; lstat, not the file they point to).
    published = {}
    for name in (slug + '.img', slug + '.vmlinuz', slug + '.initramfs'):
      path = imgDir + '/' + name
      try:
        st = os.lstat(path)
        published[path] = (st.st_dev, st.st_ino)
      except FileNotFoundError:
        pass
    return published

  def unpublish(self, published, slug, version):
    # Remove what this build published, but only files that are still the
    # ones it made: a pull of the newer version replaces them with new files
    # (os.replace), which are left alone.  Likewise the version entry goes
    # only if it isn't newer than what we built.  Nothing here registers
    # anything with the mPCC.
    for path, ident in published.items():
      try:
        st = os.lstat(path)
      except FileNotFoundError:
        continue
      if (st.st_dev, st.st_ino) != ident:
        log.info(f"leaving {path}, it was replaced since this build.")
        continue
      os.unlink(path)
    try:
      with open(self.imageDir + '/image-versions', "r") as vfile:
        imgVersions = json.loads(vfile.read())
    except (OSError, ValueError):
      return
    if slug in imgVersions and imgVersions[slug] <= version:
      self.forgetVersion(imgVersions, slug)

  def forgetVersion(self, imgVersions, image):
    # we don't have this image's current version after all; without an
    # entry the next pass fetches it again.
    imgVersions.pop(image, None)
    with open(self.imageDir + '/image-versions', "w") as vfile:
      vfile.write(json.dumps(imgVersions))

  def pullImage(self, image):
    # Pull the image, its initramfs and its kernel from another job server,
    # through the mPCC's redirect.  Each goes to a temporary file, moved into
    # place only once all three are complete, so a failed pull (e.g. every
    # host is full and answers 404) leaves what we had.  False if any
    # download failed; we try again on the next pass (#142).
    imgDir = self.imageDir + '/' + image
    pulled = []
    try:
      os.makedirs(imgDir, exist_ok=True)
      for remote, local in [(image, image + '.img'), (image + '.initramfs', image + '.initramfs'), (image + '.vmlinuz', image + '.vmlinuz')]:
        tmp = imgDir + '/.' + local + '.pull'
        pulled.append((tmp, imgDir + '/' + local))
        with self.js.session.get(self.js.mprovURL + 'images/' + remote, stream=True) as remoteImage:
          remoteImage.raise_for_status()
          with open(tmp, 'wb') as localFile:
            for chunk in remoteImage.iter_content(chunk_size=8192):
              localFile.write(chunk)
    except (requests.RequestException, OSError) as err:
      log.error(f"Unable to pull image {image} from another job server, will try again: {err}")
      for tmp, _ in pulled:
        try:
          os.unlink(tmp)
        except FileNotFoundError:
          pass
      return False
    for tmp, final in pulled:
      os.replace(tmp, final)
    return True

  def remove_build_credential(self, path):
    '''
    Remove an image build's credential directory (or whatever the image
    left at that path), never following a link.  False, said, if it
    couldn't be.
    '''
    try:
      if os.path.islink(path) or (os.path.lexists(path) and not os.path.isdir(path)):
        os.unlink(path)
      elif os.path.isdir(path):
        shutil.rmtree(path)
    except OSError as err:
      log.error(f"unable to remove {path}: {err}")
      return False
    return True

  def packImage(self, busybox, imgDir, dest):
    # Pack imgDir into the gzipped tar at dest.  tar and gzip run as two
    # processes so both exit statuses are checked: in a shell pipeline only
    # gzip's counts, and a tar that failed partway gave a truncated image
    # (#216).  The archive is written to a temporary file in the image store
    # (outside imgDir, so it isn't packed into itself) and moved into place
    # only when both succeeded.  False if anything failed.
    # dest is usually in imgDir itself: the previous archive stays out of the
    # new one, and stays in place until the new one replaces it, so a failed
    # pack keeps the image nodes are booting (#232)
    exclude = []
    if os.path.dirname(os.path.abspath(dest)) == os.path.abspath(imgDir):
      exclude = ['--exclude', './' + basename(dest)]
    tmp = None
    try:
      fd, tmp = tempfile.mkstemp(dir=self.imageDir, prefix='.' + basename(dest) + '.', suffix='.pack')
      with os.fdopen(fd, 'wb') as out:
        tar = subprocess.Popen([busybox, 'tar', 'c', '-C' + imgDir] + exclude + ['-f', '-', './'], stdout=subprocess.PIPE)
        gzip = subprocess.Popen(['gzip', '-1', '-c'], stdin=tar.stdout, stdout=out)
        # so tar gets SIGPIPE if gzip dies, instead of blocking on a full pipe
        tar.stdout.close()
        gzipStatus = gzip.wait()
        tarStatus = tar.wait()
      if tarStatus != 0 or gzipStatus != 0:
        log.error(f"packing {imgDir} failed: tar exited {tarStatus}, gzip exited {gzipStatus}")
        os.unlink(tmp)
        return False
      # mkstemp makes it 0600; images are served to nodes, as before from /tmp
      os.chmod(tmp, 0o644)
      os.replace(tmp, dest)
    except OSError as err:
      log.error(f"packing {imgDir} failed: {err}")
      if tmp is not None:
        try:
          os.unlink(tmp)
        except FileNotFoundError:
          pass
      return False
    return True

  def updateImageJobservers(self):

    # if we don't have an ~image path, make one.  If we can't, there's
    # nothing to host; handle_jobs() fails the claimed jobs (#144).
    try:
      os.makedirs(self.imageDir, exist_ok=True)
    except OSError as err:
      log.error(f"Unable to make imageDir: {self.imageDir}: {err}")
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
      except ValueError:
        pass
        

    # cycle through the images we should be serving and 
    # check our version(s) against the mPCC.
    imageList = []
    if self.imageList is not None:
      # only the configured images (#142)
      imageList = list(self.imageList)
    else:
      # no config for image list, so grab all of them from the mPCC
      response = self.js.session.get(f"{self.js.mprovURL}images/")
      #if we don't get a good reply, error out of this function only. We'll try again
      # on the next iteration
      if response.status_code <=199 or response.status_code >=300:
        log.error("Unable to get a list of images from the mPCC.")
        return
      try: 
        for image in response.json():
          imageList.append(image['slug'])
      except Exception:
        log.error("Unable to get a list of images from the mPCC")

        return

    # here we should have some sort of an image list.  So let's begin.
    for image in imageList:
      response = self.js.session.get(f"{self.js.mprovURL}systemimages/{image}/")
      if response.status_code <= 199 or response.status_code >= 300:
        log.error(f"Unable to retrieve data for image {image}")
        continue
      try:
        imageData = response.json()
      except ValueError:
        imageData = None
        log.debug(response.text)
      if imageData is None:
        log.error(f"Error trying to retrieve data for image {image}")
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
        if not self.addAsHost(image, imgVersions[image]):
          self.forgetVersion(imgVersions, image)
        continue
      # check our version against the mPCC version
      if imgVersions[image] != imageData['version']:
        if len(imageData['jobservers']) > 0:
          # if our version doesn't match, and there are job servers available, pull the image from someone else.
          if not self.pullImage(image):
            continue # try again on the next pass.
          # record the version we have now, or we'd pull it again every
          # pass, and tell the mPCC about us (#142).
          imgVersions[image] = imageData['version']
          with open(self.imageDir + '/image-versions', "w") as vfile:
            vfile.write(json.dumps(imgVersions))
          if not self.addAsHost(image, imgVersions[image]):
            self.forgetVersion(imgVersions, image)
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

  def unmountStaleBinds(self, imgDir=None):
    # A build killed with the job server (on a bare-metal install; a
    # container's mounts go with the container) leaves the host's dev,
    # proc, run and sys bound into its image tree (#183).  Nothing else
    # builds images here, so once at start-up, unmount those.
    # With imgDir, only that one build's: script-runner.sh unmounts its own
    # on any exit, but a run we had to SIGKILL never got to (#304).
    under = self.imageDir if imgDir is None else imgDir
    if not os.path.isdir(under):
      return
    for point in mounts_under(under):
      inImage = os.path.dirname(os.path.dirname(point)) == os.path.realpath(self.imageDir)
      if inImage and os.path.basename(point) in ("dev", "proc", "run", "sys"):
        log.warning(f"unmounting {point}, left by an image build that didn't finish.")
        if subprocess.run(["umount", point]).returncode != 0:
          subprocess.run(["umount", "-l", point])

  def runBounded(self, argv, timeout, what, onKill=None):
    """
    Run argv with a time limit, and return True if it succeeded.  An
    unbounded step is why a blocked build never finishes and never says
    why: nothing fails the job and the thread stays occupied (#303, #304).

    The kill goes to the process group, not just the command we started.
    A shell waiting on a foreground command defers its signal handlers, so
    a TERM to it alone would sit unhandled while the thing that is really
    stuck carries on.  Killing the group ends the child too, so the parent
    returns and gets to run its handlers.

    onKill is called only when even SIGKILL was needed, for a caller with
    cleaning up of its own that the killed command would have done.
    """
    proc = subprocess.Popen(argv, start_new_session=True)
    try:
      return proc.wait(timeout=timeout or None) == 0
    except subprocess.TimeoutExpired:
      log.error(f"{what} has run for over {timeout}s; stopping it.")
    try:
      group = os.getpgid(proc.pid)
    except ProcessLookupError:
      return False
    os.killpg(group, signal.SIGTERM)
    try:
      proc.wait(timeout=self.killGrace)
    except subprocess.TimeoutExpired:
      log.error(f"{what} didn't stop; killing it.")
      try:
        os.killpg(group, signal.SIGKILL)
      except ProcessLookupError:
        pass
      proc.wait()
      if onKill is not None:
        onKill()
    return False

  def bundlePath(self):
    if self.pythonBundle:
      return self.pythonBundle
    return os.path.join(BUNDLE_DIR, bundle_name(jobserver_version()))

  def removePipInstall(self, imgDir, image):
    """
    Uninstall a job server that a 0.6 build pip-installed onto the image's
    own Python (#309).  Left in place, a rebuilt image would carry a stale
    one beside the bundle.  Best effort: it only logs a warning.
    """
    if not glob.glob(os.path.join(imgDir, "usr/local/lib/python3*/site-packages/mprov_jobserver")):
      return
    if not self.runBounded(["chroot", imgDir, "pip3", "uninstall", "-y", "mprov_jobserver"],
                           self.pipUninstallTimeout,
                           f"removing the old pip-installed job server from {image}"):
      log.warning(f"Unable to uninstall the old pip-installed job server from {image}; "
                  f"the image keeps it, unused, beside {BUNDLE_PREFIX}.")

  def installBundle(self, imgDir, image):
    """
    Put the job server into the image as the bundle: a self-contained
    Python with the job server in it, unpacked at BUNDLE_PREFIX, with
    BUNDLE_LINK pointing into it (#309).  It needs no network and no
    Python in the image, and it is always the job server that built the
    image: a bundle of any other version is refused.  Returns True if done.
    """
    ours = jobserver_version()
    path = self.bundlePath()
    if not os.path.isfile(path):
      log.error(f"No job server bundle at {path}, so {image} can't be built. A job server "
                f"installed with pip has none built in: download {bundle_name(ours)} from "
                f"{RELEASES_URL}/tag/v{ours} and set pythonBundle: to its path in the "
                f"image-update section of the config.")
      return False

    target = imgDir + BUNDLE_PREFIX
    new = target + ".new"
    old = target + ".old"
    link = imgDir + BUNDLE_LINK
    for where in (os.path.dirname(target), os.path.dirname(link)):
      os.makedirs(where, exist_ok=True)
      if not inside(imgDir, where):
        log.error(f"{where} leads out of the image tree; not installing the job server into {image}.")
        return False

    try:
      with tarfile.open(path) as tar:
        try:
          marker = tar.extractfile("./MPROV_BUNDLE_VERSION")
        except KeyError:
          marker = None
        theirs = marker.read().decode().strip() if marker else None
        if theirs != ours:
          log.error(f"{path} holds job server {theirs or 'of an unknown version'}, not {ours}; "
                    f"an image must get the job server that builds it. Use {bundle_name(ours)}.")
          return False
        for stale in (new, old):
          if os.path.lexists(stale):
            shutil.rmtree(stale) if os.path.isdir(stale) and not os.path.islink(stale) else os.unlink(stale)
        os.mkdir(new, 0o755)
        # the "tar" filter keeps the bundle's own relative symlinks
        # (bin/python3 -> python3.11) but refuses anything that would land
        # outside it
        tar.extractall(new, filter="tar")
    except (OSError, tarfile.TarError) as err:
      log.error(f"Unable to unpack {path} into {image}: {err}")
      if os.path.isdir(new):
        shutil.rmtree(new, ignore_errors=True)
      return False

    # swap the new bundle in; a failure above left any old one in place
    if os.path.islink(target) or os.path.isfile(target):
      os.unlink(target)
    elif os.path.isdir(target):
      os.rename(target, old)
    os.rename(new, target)
    if os.path.isdir(old):
      shutil.rmtree(old, ignore_errors=True)

    if os.path.lexists(link):
      os.unlink(link)
    os.symlink(BUNDLE_PREFIX + "/bin/mprov_jobserver", link)
    log.info(f"installed job server {ours} into {image} at {BUNDLE_PREFIX}.")
    return True

  def runImageScripts(self, script, imgDir, image):
    """
    Run the image-gen script runner, bounded by scriptTimeout (#304).

    script-runner.sh unmounts the host's dev, proc, run and sys on any
    exit, including the one its TERM trap makes (#183) -- but a SIGKILL
    runs no traps, so that path unmounts them here instead.
    """
    return self.runBounded([script], self.scriptTimeout,
                           f"image-gen scripts for {image}",
                           onKill=lambda: self.unmountStaleBinds(imgDir))

  def handle_jobs(self):
    if self.js.firstrun:
      self.unmountStaleBinds()
    # try:
    self.updateImageJobservers()
    # except Exception as e:
    #   print("Error: There was an error updating Jobservers for Images.")
    #   print(f"{e=}")

    # check to see if we have any dependancy jobs running
    response = self.js.session.get( self.js.mprovURL + 'jobs/?&module=repo-update&status=1')
    if response.status_code != 200:
      self.threadOk = False
      log.error(f"Unable to check on dependent jobs.")
      return
    jobs = {}
    try:
      jobs = response.json()
      if len(jobs):
        self.threadOk = False
        log.info(f"Dependent Jobs Present, waiting for them to finish.")
        return
    except Exception:
      log.warning("Unable to parse reply, trying to continue anyway.")
    
    response = self.js.session.get( self.js.mprovURL + 'jobs/?&module=repo-update&status=2')
    if response.status_code != 200:
      self.threadOk = False
      log.error(f"Unable to check on dependent jobs.")
      return
    jobs = {}
    try:
      jobs = response.json()
      if len(jobs):
        self.threadOk = False
        log.info(f"Dependent Jobs Present, waiting for them to finish.")
        return
    except Exception:
      log.warning("Unable to parse reply, trying to continue anyway.")
    

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
        except (KeyError, TypeError):
          log.error("Image Update Job with no imageID present, cannot parse params")
          self.js.update_job_status(self.jobModule, 3, jobid=job['id'])
          return
        try:
          self.imageList[params['imageId']] = job['id']
        except (KeyError, TypeError):
          log.error("Image Update Job failed, corrupted params.")
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
        except Exception:
          log.error(f"Unable to read the claimed job for image {image}.")
      if len(self.imageList) == 0:
        return # no updates found.
        

    # check if the imageDir exists.
    if not os.path.exists(self.imageDir):
      # if not, attempt to make it, recursively.
      try:
        os.makedirs(self.imageDir, exist_ok=True)
      except OSError:
        log.error("Unable to make imageDir: " + self.imageDir)
        # no image is being built yet, so fail every job we claimed (#144).
        for jid in self.imageList.values():
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
        return
    
    # now we iterate over our imageList, and build the image(s).  An error
    # fails that image's job and moves on: returning would leave the other
    # claimed jobs running (#220).
    for image,jid in self.imageList.items():
      log.info(f"Processing update for image {image}")
      query = 'systemimages/' + image + '/'
      response = self.js.session.get( self.js.mprovURL + query)
      if response.status_code == 200:

                # instantiate the sub module and pass it the repo.
        try:
          imageDetails = response.json()
        except ValueError:
          log.error("Unable to parse response.")
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          continue
        
        try:
          if imageDetails['osdistro'] is None:
            log.error(f"No OS Distrubtion set. Not generating image.")
            self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
            continue
          log.debug(f"..{redacted_image_details(imageDetails)}")
          log.debug(f".image_update_mod.{imageDetails['osdistro']['baserepo']['ostype']['slug']}")
          mod = importlib.import_module(f".image_update_mod.{imageDetails['osdistro']['baserepo']['ostype']['slug']}", "mprov_jobserver.plugins")
          update_klass = getattr(mod, 'UpdateImage')
        except BaseException as err:
          exc_type, exc_obj, exc_tb = sys.exc_info()
          fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
          log.error("Unable to import Image Update Module.")
          log.error(f"Exception: {err=}, {type(err)=}, File={fname}, Line={exc_tb.tb_lineno}")
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          continue

        try:
          image_update = update_klass(self.js)
        except Exception as err:
          log.error(f"Unable to instantiate UpdateImage class on {imageDetails['osdistro']['baserepo']['ostype']['slug']}: {err}")
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          continue
        image_update.imageDetails = imageDetails
        image_update.imageDir = self.imageDir
        image_update.jobid = jid
        image_update.start()

        image_update.join()
        if image_update.threadOk is not True:
          continue
        # run image-gen scripts.
    
        
        imgDir = self.imageDir + '/' + imageDetails['slug']
        # the image path goes into shell commands quoted, so an odd
        # character in it can't change the command (#184)
        qimg = shlex.quote(imgDir)
        # copy in the system resolv.conf
        os.system(f"/bin/cp -f /etc/resolv.conf {qimg}/etc/resolv.conf")
        # the job server goes into the image as a bundle with its own
        # Python, not pip-installed onto the image's (#309): no PyPI, no
        # resolver to hang (#317), and always exactly our own version, which
        # is stricter than the old API-version pin (#303).  Nothing is
        # bind-mounted into the image yet -- script-runner.sh does that
        # later -- so a failure here leaves nothing to unmount.
        self.removePipInstall(imgDir, imageDetails['slug'])
        if not self.installBundle(imgDir, imageDetails['slug']):
          log.error("Unable to install the job server into the image.")
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
          log.error(f"{mprovDir} is not a directory owned by this user, failing job.")
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          continue
        os.chmod(mprovDir, 0o700)
        os.makedirs(mprovDir + '/plugins', mode=0o700, exist_ok=True)
        os.chmod(mprovDir + '/plugins', 0o700)

        # create a config file to /tmp for our script-runner instance.
        localConfig = [dict()]
        # a copy: this changed the job server's own global settings too
        localConfig[0]['global'] = dict(self.js.config_data['global'])
        # the host's CA file isn't in the image; the job server in there
        # uses the image's own OS bundle (#102)
        localConfig[0]['global'].pop('caBundle', None)
        log.debug(localConfig[0]['global']['jobmodules'])
        localConfig[0]['global']['jobmodules'] = ['script-runner']
        localConfig[0]['global']['runonce'] = True
        # A job server with its own certificate gives the script runner in
        # the image a 2-hour credential that can only read this image,
        # instead of the shared API key, which image-gen scripts used to
        # write into every image (mprov_control_center#381).  It's removed
        # before the image is packed.
        buildTls = mprovDir + '/tls'
        if not self.remove_build_credential(buildTls):
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          continue
        if getattr(self.js, 'auth', None) == 'cert':
          try:
            ident.Identity(buildTls).save(*ident.build_credential(self.js.session, self.js.mprovURL, imageDetails['slug']))
          except (ident.EnrollError, OSError) as err:
            log.error(f"unable to get a build credential for image {imageDetails['slug']}, failing job: {err}")
            self.remove_build_credential(buildTls)
            self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
            continue
          for setting in BUILD_DROPPED_SETTINGS:
            localConfig[0]['global'].pop(setting, None)
          localConfig[0]['global']['tlsDir'] = BUILD_TLS_DIR
          localConfig[0]['global']['mprovURL'] = self.js.mprovURL
        try:
          fd = os.open(jobserverYaml, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        except OSError as err:
          log.error(f"Unable to create {jobserverYaml}, failing job: {err}")
          self.remove_build_credential(buildTls)
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          continue
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as confFile:
          yaml.dump(localConfig, confFile)
          confFile.write("\n- !include plugins/*.yaml")

        try:
          # script-runner.sh takes these as shell words, so quote them (#184)
          data = {
            'imgDir': shlex.quote(imgDir),
            'image': shlex.quote(imageDetails['slug']),
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
                log.error(f"Unable to write root authorized_keys into the image, failing job: {err}")
                self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
                continue
          
          # set the root pw to the password in the distro
          if 'rootpw' in imageDetails['osdistro']:
            if imageDetails['osdistro']['rootpw'] is not None and imageDetails['osdistro']['rootpw'] != "" :
              log.info("Setting root password...")
              rootpw = imageDetails['osdistro']['rootpw']
              # chpasswd reads one user:password line; a line break would end it early
              if "\n" in rootpw or "\r" in rootpw:
                log.error("the OS distro's root password contains a line break, failing job.")
                self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
                continue
              # feed the password to the chrooted chpasswd over its stdin pipe,
              # which is private to this (root) process; an `echo ... | passwd`
              # shell command would expose it in the sh -c argv (#168).
              # chpasswd is non-interactive, unlike passwd's prompts (#229).
              if subprocess.run(["chroot", imgDir, "chpasswd"],
                                input=f"root:{rootpw}\n",
                                text=True).returncode != 0:
                # don't ship an image with the distro's default root password
                log.error("unable to set the image's root password, failing job.")
                self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
                continue

          # now let's run our script-runner shell script.
          with open(os.open(imgDir + '/tmp/mprov/script-runner.sh',os.O_CREAT | os.O_WRONLY, 0o755) , 'w') as conf:
              conf.write(jenv.get_template('image-update/script-runner.sh').render(data))
          # now let's run our script-runner shell script.
          with open(os.open(imgDir + '/tmp/mprov/plugins/script-runner.yaml',os.O_CREAT | os.O_WRONLY, 0o755) , 'w') as conf:
              conf.write(jenv.get_template('image-update/script-runner.yaml.j2').render(data))

          # a failed image-gen script fails the job, and no image is packed (#221)
          if not self.runImageScripts(imgDir + '/tmp/mprov/script-runner.sh', imgDir, imageDetails['slug']):
            log.error("while running image-gen scripts via job server, failing job.")
            self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
            continue
        finally:
          try:
            os.unlink(jobserverYaml)
          except FileNotFoundError:
            pass
          self.remove_build_credential(buildTls)
        # never pack an image with the build credential in it
        if os.path.lexists(buildTls):
          log.error(f"{buildTls} is still there, so the image isn't packed with it; failing job.")
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          continue

        # grab a copy of busybox into a root-only directory under the image
        # store.  A fixed /tmp path is world-writable: another user could
        # plant a binary there for us to run as root (#170).
        busyboxDir = self.imageDir + '/.busybox'
        os.makedirs(busyboxDir, mode=0o700, exist_ok=True)
        os.chmod(busyboxDir, 0o700)
        busybox = busyboxDir + '/busybox'
        if os.system(f"wget -q -O {shlex.quote(busybox)} https://busybox.net/downloads/binaries/1.35.0-x86_64-linux-musl/busybox"):
          log.error("unable to download busybox")
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
          log.error("downloaded busybox is not a valid ELF binary, refusing to run it")
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          continue
        os.chmod(busybox, 0o755)

        # attempt to call updateBootFiles()
        # Check if 'updateBootFiles' exists and is callable
        if hasattr(image_update, 'updateBootFiles') and callable(getattr(image_update, 'updateBootFiles', None)):
          image_update.updateBootFiles() # You can call it
          # it fails the job itself; don't pack and register the image (#144).
          if image_update.threadOk is not True:
            continue
        else:
          log.info(f"{imageDetails['osdistro']['baserepo']['ostype']['slug']} does not have updateBootFiles.")


        # script-runner.sh unmounts the host's dev, proc, run and sys from
        # the image; if any is still there, tar would pack the host's (#183)
        mounted = mounts_under(imgDir)
        if mounted:
          log.error(f"not packing {imgDir}, something is still mounted in it: {', '.join(mounted)}")
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          continue

        # package the filesystem into an initramfs
        log.info("Building " +  imgDir + '/' + imageDetails['slug'] + '.img')
        startTime=time.time()
        if not self.packImage(busybox, imgDir, imgDir + '/' + imageDetails['slug'] + '.img'):
          log.error("unable to create initramfs")
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          continue
        endTime = time.time()
        lapsed = endTime - startTime
        log.info("Image generated in " + str(lapsed) + " seconds.")
        log.info("Image saved to "+  imgDir + '/' + imageDetails['slug'] + '.img')


        # what this build published, so a 409 below removes exactly these and
        # never a newer file a pull or another build has put there since
        published = self.publishedFiles(imgDir, imageDetails['slug'])

        # update the 'jobservers' field to be us, so that the 
        data = {
          'slug': imageDetails['slug'],
          'needs_rebuild': False,
          'jobservers':[
            self.js.id,
          ],
          **addjs_version(self.js, imageDetails['version']),
        }
        response = self.js.session.patch(self.js.mprovURL + 'systemimages/' + str(data['slug']) + '/?addjs', data=json.dumps(data))
        if response.status_code == 409:
          # the image was rebuilt (its version moved on) while we built it,
          # so what we built is the wrong version: don't host it, take it
          # down, and fail the job; the newer rebuild job builds it (#248).
          log.error(f"image {imageDetails['slug']} moved past version {imageDetails['version']} while we built it; removing what this build published.")
          self.unpublish(published, imageDetails['slug'], imageDetails['version'])
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')
          continue
        with open(self.imageDir + '/image-versions', "r") as vfile:
          # grab the version info into a var
          try:
            imgVersions = json.loads(vfile.read())
          except ValueError:
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
          log.error(f"handling job for {image} server returned {response.status_code} for request {self.js.mprovURL + query}")
          self.js.update_job_status(self.jobModule, 3, jobid=jid, jobquery='jobserver=' + str(self.js.id) + '&status=2')