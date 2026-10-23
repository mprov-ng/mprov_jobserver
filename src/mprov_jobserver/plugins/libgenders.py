import shutil
from threading import local
from .plugin import JobServerPlugin
import os
import json
import sys
from slugify import slugify

# the libgenders module is used to generate a genders file for use 
# with the pdsh command.

# genders attributes can't contain whitespace, ',' or '=', so slugify
# every name we write.  Underscores are kept so mPCC slugs come through
# unchanged.
def genders_slug(name):
  return slugify(str(name), regex_pattern=r'[^-a-z0-9_]+')

class libgenders(JobServerPlugin):
  jobModule = 'libgenders'
  gendersFile = "/etc/genders"
  # hand-written entries, appended to the end of gendersFile.
  gendersInclude = "/etc/genders.include"

  def read_include(self):
    """ The include file's contents, ending in a newline; "" if there isn't one.
    An include that can't be read is skipped with a warning, so the
    generated entries are still written. """
    if not os.path.exists(self.gendersInclude):
      return ""
    try:
      with open(self.gendersInclude) as includeFile:
        contents = includeFile.read()
    except Exception as e:
      print(f"Warning[libgenders]: Unable to read {self.gendersInclude}, not including it: {e}")
      return f"# Unable to read {self.gendersInclude}: {e}\n"
    if contents and not contents.endswith("\n"):
      contents += "\n"
    return f"# Included from {self.gendersInclude}\n" + contents

  def handle_jobs(self):
    nodes = None
    images = None
    groups = None
    distros = None
    # Update all pending jobs of oUr module to running
    if(not self.set_job_running()):
      # no jobs to run, just return
      return
    print("[libgenders] Handling Job")
    # get a list of all nodes.
    response = self.js.session.get( self.js.mprovURL + 'systems/')
    if not self.checkHTTPStatus(response.status_code):
      print("Error[libgenders]: Unable to get a list of sysetms from the mPCC.")
      self.set_job_failure()
      return
    nodes = response.json()
    # get a list of all groups. 
    response = self.js.session.get( self.js.mprovURL + 'systemgroups/')
    if not self.checkHTTPStatus(response.status_code):
      print("Error[libgenders]: Unable to get a list of system groups from the mPCC.")
      self.set_job_failure()
      return
    groups = response.json()

    # get a list of all the images.
    response = self.js.session.get ( self.js.mprovURL + 'systemimages/')
    if not self.checkHTTPStatus(response.status_code):
      print("Error[libgenders]: Unable to get a list of system images from the mPCC.")
      self.set_job_failure()
      return    
    images = response.json()

    # get a list of all os distros.
    response = self.js.session.get ( self.js.mprovURL + 'distros/')
    if not self.checkHTTPStatus(response.status_code):
      print("Error[libgenders]: Unable to get a list of OS Distrubutions from the mPCC.")
      self.set_job_failure()
      return    
    distros = response.json()

    # loop through all nodes and add them to the genders file making 
    # sure to add all system groups and os's they are part of.
    genders_file_contents = """
# This genders file is maintained by mProv.
# if you would like entries included in this file
# create an /etc/genders.include file and put them 
# there.  They will be added to the end of this file.
# All libgenders rules for this file and any 
# included files still apply.
#
# See 'man libgenders' for more information.
#
# THIS FILE WILL BE OVERRIDDEN ON NODE SAVES!
# DO NOT DIRECTLY MODIFY THIS FILE AND EXPECT YOUR
# CHANGES TO BE SAVED
# """
    try:
      with open(self.gendersFile, 'w') as gendersFile:
        gendersFile.write(genders_file_contents + "\n")
        for node in nodes:
          try:
            distStr = ""
            imgStr = ""
            if node['systemimage'] is not None:
              image_name = node['systemimage']
              # find the system image in question
              image = [image for image in images if image.get('slug')==image_name]
              image = image[0]
              imgStr = f"image={genders_slug(image_name)},"
              # find the distro for the image.  The systemimages/ list can
              # return osdistro as an ID or as a nested object.
              osdistro = image['osdistro']
              distro_id = osdistro['id'] if isinstance(osdistro, dict) else osdistro
              distro = [distro for distro in distros if distro.get('id')==distro_id]
              distro = distro[0]
              distStr = f"os={genders_slug(distro['name'])},"

            node_groups=""
            for groupId in node['systemgroups']:
              group = [group for group in groups if group.get('id')==groupId]
              group = group[0]
              node_groups += genders_slug(group['name']) + ","

            line = f"{node['hostname']}   {distStr}{imgStr}{node_groups}all"
          except Exception as f:
            exc_type, exc_obj, exc_tb = sys.exc_info()

            line = f"# Error processing node {node['hostname']}: {f} Line: {exc_tb.tb_lineno}"
          gendersFile.write(line + "\n")
        gendersFile.write(self.read_include())
    except Exception as e:
        print(f"Error[libgenders]: Unable to write to {self.gendersFile}: {e}")
        self.set_job_failure()
        return
    self.set_job_success()
    print("[libgenders] Job Complete.")


