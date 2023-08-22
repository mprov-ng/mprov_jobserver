import shutil
from threading import local
from .plugin import JobServerPlugin
import os
import json
import sys
from slugify import slugify

# the libgenders module is used to generate a genders file for use 
# with the pdsh command.

class libgenders(JobServerPlugin):
  jobModule = 'libgenders'
  gendersFile = "/etc/genders"
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
            image_name = node['systemimage']
            # find the system image in question
            image = [image for image in images if image.get('slug')==image_name]
            image = image[0]
            
            # find the distro for the image
            distro = [distro for distro in distros if distro.get('id')==image['osdistro']['id']]
            distro = distro[0]
            node_groups = ""
            for groupId in node['systemgroups']:
              group = [group for group in groups if group.get('id')==groupId]
              group = group[0]
              node_groups += group['name']
            line = f"{node['hostname']}   os={slugify(distro['name'])},image={image_name},{node_groups},all"
          except Exception as f:
            exc_type, exc_obj, exc_tb = sys.exc_info()

            line = f"# Error processing node {node['hostname']}: {f} Line: {exc_tb.tb_lineno}"
          gendersFile.write(line + "\n")
    except Exception as e:
        print(f"Error[libgenders]: Unable to write to {self.gendersFile}: {e}")
        self.set_job_failure()
        return
    self.set_job_success()
    print("[libgenders] Job Complete.")


