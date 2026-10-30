from .plugin import JobServerPlugin
import sys
import os
import requests
from threading import Thread, Lock
import json
import subprocess
import yaml
import socket
import stat
from pathlib import Path
from mprov_jobserver.log import get_logger

log = get_logger(__name__)
# import pprint

def slug_of(value):
  # A related object as the mPCC nests it: an object with a slug, or just
  # the slug once the nesting is deeper than the serializer's depth (#146).
  return value['slug'] if isinstance(value, dict) else value


class script_runner(JobServerPlugin):
  jobModule = 'script-runner'
  scriptTmpDir="/tmp/mprov-script-runner/"

  def __init__(self, js):
    super().__init__(js)
    # each script thread records its subprocess return code (or an error
    # string) here so handle_jobs() can check them all after joining (#174)
    self.scriptResults = {}
    self.scriptLock = Lock()
    self.failed = False

  def depResolve(self, depDict):
    '''
    Returns an array of 'steps' for dependancy resolution.  Each 'step' guarantees each entry has no other dependencies.

      Parameters:
        depDict (dict):  is a dependency dictionary of arrays which are lists of the dependencies of their respective keys.

      Returns:
        result (list | None): a list of dicts, each entry in the list is a step of dependency resolution. None on error
    '''
    # this snippet is a bit of beautiful python borrowed from
    # https://stackoverflow.com/questions/5287516/dependencies-tree-implementation#5288547
    # and modified for clarity and implemention 

    # convert the dict of lists to a dict of dicts.
    dep_dict=dict((key, set(depDict[key])) for key in depDict)
    result=[]
    prevT=set([])
    while dep_dict:
        # print("-----")
        # print("dep_dict: " + str(dep_dict))

        # Should values not in keys produce an error? Unresolvable deps?
        # values not in keys (items without dep)
        # turns the values of the incoming dict into a set.  Then removes all the
        # keys that exist in the dict, giving us dep free items in t
        t=set(item for vals in dep_dict.values() for item in vals)-set(dep_dict.keys())
        if len(t) > 0:
          log.error("Unresolvable Dependencies, make sure it's listed in distro, group, or entity scripts: ")
          for ent in t:
            log.error("\t '" + ent + "'")
          return None
        # print("t: " + str(t))
        
        # add any keys without value (items without dep) to t
        t.update(key for key, val in dep_dict.items() if not val)
        if prevT == t:
        # if not firstPass and len(t) > 0:
          # possible cyclic dependency?
          log.error("Possible cyclic dependency.")
          log.debug("depDict: " + str(depDict))
          log.debug("t: " + str(t))
          return None
        # print("t: " +  str(t))
        prevT = t
        # t contains our dep free items for this iteration, so
        # append t to the result list.
        result.append(t)
        # print("result: " + str(result))
        
        # and cleaned up
        # for each entry in the dict, if it has a value, and we have resolved it's
        # dependencies, remove the resolved dependencies from the value
        # cleans things up in 2 ways:
        #   - removes keys that do not have a value (resolved deps)
        #   - removes values for already resolved deps from left over deps for each item.
        dep_dict=dict(((key, val-t) for key, val in dep_dict.items() if val))
        # print("dep_dict: " + str(dep_dict))
    return result

  def handle_jobs(self):
    
    # see if we are being called from a runonce command
    if not self.js.runonce:
      log.error("script-runner must be run in a 'runonce' jobserver session.")
      return False
    log.info("Jobserver Running script-runner...")

    sysimage=False
    system=False
    scriptMode='image-gen' # should be img-gen or post-boot, def: img-gen
    entityId=None 

    # now we need to look for some commandline options
    if '-h' in sys.argv:
      # someone passed the -h flag, print the help.
      self.printHelp()
      return False

    # are -i and -s passed together?
    if '-i' in sys.argv and '-s' in sys.argv:
      log.error("-i and -s are mutually exclusive!")
      self.printHelp()
      return False


    # look for a s
    if '-i' in sys.argv:
      sysimage = True
      system = False
      entityId = sys.argv[sys.argv.index('-i')+1]
    elif '-s' in sys.argv:
      sysimage = False
      system = True
      entityId = socket.gethostname()
    else:
      log.error("You must specify -i or -s")
      self.printHelp()
      return False
    entity = None
    if '-b' in sys.argv:
      # someone is asking for post-boot scripts
      scriptMode = 'post-boot'

    # XXX: Can this be better optimized?
    query=""
    if sysimage:
      query="systemimages/" + entityId + "/"
    else:
      query="systems/?self" #?hostname=" + entityId
    log.debug(self.js.mprovURL + query)
    # If the image or system we were asked for can't be loaded, stop and say
    # so.  This used to fall back to the nads image's scripts: an image build
    # that got a 5xx ran nads' image-gen scripts in the image it was building
    # and carried on, and with -s the fallback had no 'systemimage' and
    # crashed.  exit() only ended this thread, so runonce still exited 0 (#156).
    what = f"system image '{entityId}'" if sysimage else "this system (systems/?self)"
    try:
      response = self.js.session.get( self.js.mprovURL + query)
    except requests.RequestException as err:
      return self.entityFailed(f"Error: unable to reach the mPCC for {what}: {err}")
    entity = None
    if response.status_code == 200:
      try:
        entity = response.json()
      except ValueError:
        pass
    if entity is None:
      return self.entityFailed(f"Error: unable to load {what} from the mPCC: {response.status_code} {response.text[:200]}")

    # some clean up of the entity
    if type(entity) == list:
      if len(entity) == 1:
        entity = entity[0]
    if not isinstance(entity, dict):
      return self.entityFailed(f"Error: the mPCC's reply for {what} isn't one entity, cannot continue.")
    if system :
      if not isinstance(entity.get('systemimage'), dict):
        return self.entityFailed(f"Error: {what} has no system image, so it has no scripts to run.")
      entity['osdistro'] = entity['systemimage']['osdistro']
    # pprint.pprint(entity)

    if "name" not in entity and "hostname" not in entity:
      return self.entityFailed("Error: Corrupted entity found, cannot continue.")

    # merge the scripts and config_params from distro -> system groups ->
    # image -> system.  The mPCC does the merge and serves it as `config`;
    # an mPCC from before #269 doesn't, so merge the layers here instead.
    merged = self.mpccMerge(entity, scriptMode)
    if merged is None:
      merged = self.layerMerge(entity, scriptMode)
    # read before config_params is replaced by the merge: the lists from
    # every layer count, not just the most specific one (#110)
    disabled = self.disabledScripts(entity, merged[2])
    scripts, scriptDeps, entity['config_params'] = merged
    if not self.dropDisabled(scripts, scriptDeps, disabled):
      self.failed = True
      self.set_job_failure()
      return

    # add the mprovURL to the entity. 
    entity['mprovURL'] = self.js.mprovURL
    
    # only add the API key for images that will be sync'ed 
    # to registered machines only.
    # nads images are public, so don't sync the api key.
    if entityId == "nads" and sysimage:
      entity['apikey'] = ""
    else:
      # add our apikey to the entity
      entity['apikey'] = self.js.apikey

    # print(scriptDeps)
    # resolve our dependencies
    scriptDeps=self.depResolve(scriptDeps)
    if scriptDeps is None:
      # depResolve() said why (a missing or cyclic dependency).  Running
      # some of them in a guessed order is worse than running none (#146).
      log.error("unable to order the scripts by their dependencies, not running any scripts.")
      self.failed = True
      self.set_job_failure()
      return
    # print(scripts)

    # make sure our local tmp dir exists.  the entity file we write there
    # holds our api key, so the workspace must be reachable by us alone (#169).
    entityPath = self.scriptTmpDir + "/entity.json"
    os.makedirs(self.scriptTmpDir, mode=0o700, exist_ok=True)
    # On a node the workspace is under /tmp, so another local user could
    # have made it first, or left a symlink there.  We download scripts into
    # it and run them as root, so only use a real directory we own (#169).
    workspace = os.lstat(self.scriptTmpDir.rstrip("/") or "/")
    if not stat.S_ISDIR(workspace.st_mode) or workspace.st_uid != os.geteuid():
      log.error(f"{self.scriptTmpDir} is not a directory owned by this user, not running any scripts.")
      self.failed = True
      self.set_job_failure()
      return
    # a dir left behind by an older version may be world-readable; tighten it.
    os.chmod(self.scriptTmpDir, 0o700)

    # output the entity to a json file, owner read/write only (#169).  An
    # existing file keeps its mode through O_TRUNC, so set it again.
    entFd = os.open(entityPath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(entFd, 0o600)
    with os.fdopen(entFd, "w") as entFile:
      entFile.write(json.dumps(entity))

    # all argument and entity validation has passed; now, and only now,
    # keep users out while the scripts run.  Creating it earlier would
    # lock users out of the node on any bad invocation (#173).
    file_path = Path("/etc/nologin")
    try:
      file_path.touch()
    except OSError:
      pass
    try:
      for step in scriptDeps:
        threads = []
        for script in step:
          # download the script and thread it out within this step.
          log.info(f"Running {scripts[script]['filename']}")
          t = Thread(target=self.runScript, args=(scripts[script]['filename'], ))
          t.start()
          threads.append(t)

          # wait for the threads in this step to finish.
        for t  in threads:
          t.join()
      # if any script failed, or never ran, report failure instead of success (#174)
      failures = [(name, rc) for name, rc in self.scriptResults.items() if rc != 0]
      if failures:
        self.failed = True
        for name, rc in failures:
          log.error(f"script '{name}' failed: {rc}")
        log.info(f"{len(failures)} script(s) failed.")
        self.set_job_failure()
    finally:
      try:
        # remove the entity file, even if the run raised (#169).  If it
        # can't be removed, that still raises, but only after...
        try:
          os.unlink(entityPath)
        except FileNotFoundError:
          pass
      finally:
        # ...the login block is lifted, however we leave (#173).
        try:
          os.unlink("/etc/nologin")
        except OSError:
          pass
    if not failures:
      log.info("script-runner complete.")

  def entityFailed(self, message):
    # the run fails, and runonce exits nonzero; nothing has run yet and
    # /etc/nologin hasn't been created, so there's nothing to undo.
    log.error(message)
    self.failed = True
    self.set_job_failure()
    return False

  def layerParams(self, layer):
    ''' A layer's own config_params as a dict (YAML text, a dict, or a list of mappings), or {}. '''
    params = layer.get('config_params') if isinstance(layer, dict) else None
    if isinstance(params, str):
      try:
        params = yaml.safe_load(params)
      except yaml.YAMLError:
        return {}
    if isinstance(params, list):
      flat = {}
      for entry in params:
        if isinstance(entry, dict):
          flat.update(entry)
      params = flat
    return params if isinstance(params, dict) else {}

  def disabledScripts(self, entity, merged_params):
    '''
    The script slugs listed under disable_scripts in any layer's
    config_params (distro, system groups, image, system), combined (#110):
    a node can switch off a script its group runs while it tests another.
    A single slug may be given as a string.
    '''
    layers = [('the OS distro', entity.get('osdistro'))]
    image = entity.get('systemimage')
    if isinstance(image, dict):
      layers += [(f"system group {g.get('name')}", g) for g in image.get('systemgroups') or [] if isinstance(g, dict)]
      layers.append((f"system image {image.get('name')}", image))
    layers += [(f"system group {g.get('name')}", g) for g in entity.get('systemgroups') or [] if isinstance(g, dict)]
    layers.append((f"{entity.get('hostname') or entity.get('name')}", entity))
    sources = [(name, self.layerParams(layer)) for name, layer in layers]
    sources.append(("the merged config", merged_params if isinstance(merged_params, dict) else {}))
    disabled = []
    for name, params in sources:
      value = params.get('disable_scripts')
      if value is None:
        continue
      if isinstance(value, str):
        value = [value]
      if not isinstance(value, list) or not all(isinstance(slug, str) for slug in value):
        log.warning(f"ignoring disable_scripts in {name}'s config_params: it isn't a list of script slugs")
        continue
      for slug in value:
        if slug.strip() and slug.strip() not in disabled:
          disabled.append(slug.strip())
    return disabled

  def dropDisabled(self, scripts, scriptDeps, disabled):
    '''
    Leave the disabled scripts out of this run.  False if a script that
    still runs depends on one: running it without what it needs is worse
    than not running (#110).
    '''
    for slug in disabled:
      if slug in scripts:
        log.info(f"skipping {slug} (disable_scripts)")
        del scripts[slug]
        scriptDeps.pop(slug, None)
      else:
        log.warning(f"disable_scripts names {slug}, which isn't in this run")
    ok = True
    for slug, deps in scriptDeps.items():
      for dep in deps:
        if dep in disabled:
          log.error(f"script {slug} depends on {dep}, which disable_scripts turns off; not running any scripts.")
          ok = False
    return ok

  def mpccMerge(self, entity, scriptMode):
    '''
    Use the merge the mPCC serves as the entity's `config` (#269): its
    config_params, and its scripts in order, looked up in the entity's layers
    for their file, type and dependencies.

      Returns:
        (scripts, scriptDeps, config_params), or None if the mPCC doesn't
        serve the merge.
    '''
    config = entity.get('config')
    if not isinstance(config, dict) or not isinstance(config.get('config_params'), dict) \
        or not isinstance(config.get('scripts'), list):
      return None

    # every script object in the entity's layers, by slug
    layers = [entity, entity.get('osdistro')]
    layers += entity.get('systemgroups') or []
    image = entity.get('systemimage')
    if isinstance(image, dict):
      layers += [image] + (image.get('systemgroups') or [])
    found = {}
    for layer in layers:
      if not isinstance(layer, dict):
        continue
      for script in layer.get('scripts') or []:
        if isinstance(script, dict):
          found.setdefault(script['slug'], script)

    scriptDeps = {}
    scripts = {}
    for slug in config['scripts']:
      script = found.get(slug)
      if script is None:
        # it would run, but we can't tell what it is or where to get it.
        log.error(f"script '{slug}' is in the mPCC's config but not in the entity.")
        self.scriptResults[slug] = "error: not found in the entity"
        continue
      if slug_of(script['scriptType']) != scriptMode:
        continue
      # dependsOn is a list of objects or of slugs, depending on the nesting
      scriptDeps[slug] = [slug_of(dep) for dep in script['dependsOn']]
      scripts[slug] = script
    return scripts, scriptDeps, config['config_params']

  def layerMerge(self, entity, scriptMode):
    '''
    Merge the scripts and config_params from the entity's own layers (distro
    -> system groups -> entity), for an mPCC that doesn't serve `config`.

      Returns:
        (scripts, scriptDeps, config_params)
    '''
    # merge the scripts from distro -> system_groups -> entity

    # we should iterate in all the scripts.  Let's process that into a dependancy tree.
    scriptDeps = {}
    scripts = {}
    # osdistro scripts first
    if entity['osdistro']['scripts'] is not None:
      for script in entity['osdistro']['scripts']:
        # with -s the distro is nested deeper (system -> image -> distro), so
        # scriptType and dependsOn can be slugs instead of objects (#146).
        if slug_of(script['scriptType']) != scriptMode:
          continue
        scriptDeps[script['slug']] = [slug_of(dep) for dep in script['dependsOn']]
        scripts[script['slug']] = script

    # then group scripts
    for group in entity['systemgroups']:
      if group['scripts'] is not None:
        for script in group['scripts']:
          if slug_of(script['scriptType']) != scriptMode:
            continue
          # these were used as given, so objects broke depResolve() (#146)
          scriptDeps[script['slug']] = [slug_of(dep) for dep in script['dependsOn']]
          scripts[script['slug']] = script

    # finally lay over the entity scirpts.
    if entity['scripts'] is not None:
      for script in entity['scripts']:
        if slug_of(script['scriptType']) != scriptMode:
          continue
        scriptDeps[script['slug']] = [slug_of(dep) for dep in script['dependsOn']]
        scripts[script['slug']] = script

    yaml_merged={}
    # then we will flatten config_params on the host.
    # distro first
    
    # print(entity['osdistro']['config_params'])
    tmpYamlDict = yaml.safe_load(entity['osdistro']['config_params'])
    if tmpYamlDict is not None:
      try:
        if type(tmpYamlDict) is list:
          for dict_entry in tmpYamlDict:
            yaml_merged.update(dict_entry)
        else: 
            yaml_merged.update(tmpYamlDict)
      except (TypeError, ValueError) as err:
        log.warning(f"ignoring the OS distro's config_params, they aren't a mapping: {err}")
    
    # print(yaml_merged)
    # then the system groups
    for group in entity['systemgroups']:
      # print(group['config_params'])
      tmpYamlDict = yaml.safe_load(group['config_params'])
      if tmpYamlDict is not None:
        try:
          if type(tmpYamlDict) is list:
            for dict_entry in tmpYamlDict:
              yaml_merged.update(dict_entry)
          else:
              yaml_merged.update(tmpYamlDict)
        except (TypeError, ValueError) as err:
          log.warning(f"ignoring system group {group.get('name')}'s config_params, they aren't a mapping: {err}")  
    # and finally the system/image params
    # print(yaml_merged)
    # print(yaml.safe_load(entity['config_params']))
    if type(entity['config_params']) is dict:
      tmpYamlDict = entity['config_params']
    else:
      tmpYamlDict = yaml.safe_load(entity['config_params'])
    # print(tmpYamlDict)
    if tmpYamlDict is not None:
      try:
        if type(tmpYamlDict) is list:
          for dict_entry in tmpYamlDict:
            yaml_merged.update(dict_entry)
        else: 
          yaml_merged.update(tmpYamlDict)
      except (TypeError, ValueError) as err:
        log.warning(f"ignoring the entity's config_params, they aren't a mapping: {err}")
              
    # print(yaml_merged)
    return scripts, scriptDeps, yaml_merged

  def runScript(self, filename):
    try:
      # grab the file
      local_file = self.download_file(filename)
      # run the file, and record its return code (#174)
      return_code = subprocess.run(local_file, shell=True).returncode
    except Exception as e:
      # a script that never started is a failure, not a success
      return_code = f"error: {e}"
    with self.scriptLock:
      self.scriptResults[filename] = return_code
    

  def download_file(self, url):
    local_filename = url.split('/')[-1]
    local_filename = self.scriptTmpDir + "/" + local_filename
    # NOTE the stream=True parameter below
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        with open(local_filename, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192): 
                # If you have chunk encoded response uncomment if
                # and set chunk_size parameter to None.
                #if chunk: 
                f.write(chunk)
        os.chmod(local_filename, 0o755)
    return local_filename

  def printHelp(self):
    print(sys.argv[0] + ":")
    print("\t-i <systemimage-ID>    - run scripts for systemimage <systemimage-ID>")
    print("\t-s                     - run scripts for system")
    print("\t                         (NOTE: NOT the fqdn!)")
    print("\t-b                     - run in post-boot mode, defaults to image-gen mode without this flag")
    return False
