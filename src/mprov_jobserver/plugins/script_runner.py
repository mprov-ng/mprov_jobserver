from .plugin import JobServerPlugin
import sys
import os
import requests
from threading import Thread
import json
import subprocess
import yaml
import socket
# import pprint

class script_runner(JobServerPlugin):
  jobModule = 'script-runner'
  scriptTmpDir="/tmp/mprov-script-runner/"

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
    firstPass=True
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
          print("Error: Unresolvable Dependancies, make sure it's listed in distro, group, or entity scripts: ")
          for ent in t:
            print("\t '" + ent + "'")
          return None
        # print("t: " + str(t))
        
        # add any keys without value (items without dep) to t
        t.update(key for key, val in dep_dict.items() if not val)
        if prevT == t:
        # if not firstPass and len(t) > 0:
          # possible cyclic dependency?
          print("Error: Possibe cyclic dependency.")
          print("depDict: " + str(depDict))
          print("t: " + str(t))
          return None
        # print("t: " +  str(t))
        prevT = t
        # After the first pass, we want to check for cyclic deps.
        firstPass=False
        # t contains our dep free items for this iteration, so
        # append t to the result list.
        result.append(t)
        # print("result: " + str(result))
        
        # and cleaned up
        # for each entry in the dict, if it has a value, and we have resolved it's
        # dependancies, remove the resolved dependancies from the value
        # cleans things up in 2 ways:
        #   - removes keys that do not have a value (resolved deps)
        #   - removes values for already resolved deps from left over deps for each item.
        dep_dict=dict(((key, val-t) for key, val in dep_dict.items() if val))
        # print("dep_dict: " + str(dep_dict))
    return result

  def handle_jobs(self):
    
    # see if we are being called from a runonce command
    if not self.js.runonce:
      print("Error: script-runner must be run in a 'runonce' jobserver session.")
      return False
    print("Jobserver Running script-runner...")
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
      print("Error: -i and -s are mutually exclusive!")
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
      print("Error: You must specify -i or -s")
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
    print(self.js.mprovURL + query)
    response = self.js.session.get( self.js.mprovURL + query)
    try:
      entity = response.json()
      if response.status_code != 200:
        print(f"code: {response.status_code}")
        raise Exception()
    except:
      # if this response was bad, try to grab the nads image 
      query="systemimages/nads/"
      print(self.js.mprovURL + query)
    
      response = self.js.session.get( self.js.mprovURL + query )
      try:
        entity = response.json()
      except: 
        print("Error: Unable to get information from mPCC.")
        exit(1)

    if entity == None:
      print("Error: Entity Not loaded.")
      exit(1)

    # some clean up of the entity
    if type(entity) == list:
      if len(entity) == 1:
        entity = entity[0]
    if system :
      entity['osdistro'] = entity['systemimage']['osdistro']
    # pprint.pprint(entity)

    if "name" not in entity and "hostname" not in entity:
      print("Error: Corrupted entity found, cannot continue.")
      exit(1)

    # merge the scripts from distro -> system_groups -> entity

    # we should iterate in all the scripts.  Let's process that into a dependancy tree.
    scriptDeps = {}
    scripts = {}
    # osdistro scripts first
    for script in entity['osdistro']['scripts']:
      deps=[]
      if script['scriptType']['slug'] != scriptMode:
        continue
      if len(script['dependsOn']) > 0:
        # we have deps.
        for dep in script['dependsOn']:
          deps.append(dep['slug'])
      scriptDeps[script['slug']] = deps
      scripts[script['slug']] = script

    # then group scripts
    for group in entity['systemgroups']:
      for script in group['scripts']:
        deps=[]
        if script['scriptType']['slug'] != scriptMode:
          continue
        if len(script['dependsOn']) > 0:
          # we have deps.
          for dep in script['dependsOn']:
            deps.append(dep['slug'])
        scriptDeps[script['slug']] = deps
        scripts[script['slug']] = script

    # finally lay over the entity scirpts.
    for script in entity['scripts']:
      deps=[]
      if script['scriptType']['slug'] != scriptMode:
        continue
      if len(script['dependsOn']) > 0:
        # we have deps.
        for dep in script['dependsOn']:
          deps.append(dep['slug'])
      scriptDeps[script['slug']] = deps
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
      except:
        pass
    
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
        except:
          pass  
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
      except:
        pass
              
    # print(yaml_merged)
    entity['config_params'] = yaml_merged

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
    # resolve our dependancies
    scriptDeps=self.depResolve(scriptDeps)
    # print(scripts)

    # make sure our local tmp dir exists
    os.makedirs(self.scriptTmpDir, exist_ok=True)
    
    # output the entity to a json file
    with open(self.scriptTmpDir + "/entity.json", "w") as entFile:
      entFile.write(json.dumps(entity))
    
    for step in scriptDeps:
      threads = []
      for script in step:
        # download the script and thread it out within this step.
        print(f"Running {scripts[script]['filename']}")
        t = Thread(target=self.runScript, args=(scripts[script]['filename'], ))
        t.start()
        threads.append(t)

        # wait for the threads in this step to finish.
      for t  in threads:
        t.join()
    # remove the entity file.
    os.unlink(self.scriptTmpDir + "/entity.json")
    print("script-runner complete.")

  def runScript(self, filename):
    # grab the file
    local_file = self.download_file(filename)

    # run the file
    subprocess.run(local_file)
    

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