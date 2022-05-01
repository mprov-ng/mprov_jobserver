from .plugin import JobServerPlugin

class pxe_update(JobServerPlugin):
  jobModule = 'pxe-update'
  tftpPath = "/tftpboot/"
  networks = None

  def handle_jobs(self):
    # See if we have any pxe-update jobs, and take 'em if we do, else just exit
    jobquery = "jobserver=" + str(self.js.id) + "&module=pxe-update"
    if not self.js.update_job_status(self.jobModule, 2, jobquery=jobquery + "&status=1"):
      return # no jobs.
    
    # we have some jobs, let's do this.
    # get the jobs
    response = self.js.session.get( self.js.mprovURL + 'jobs/?' + jobquery + '&status=2')
    for job in response.json():


      # if networks is None, grab all the networks.  
      if self.networks is None:
        self.networks = []
        # no imageList in config, grab it from MPCC
        response = self.js.session.get( self.js.mprovURL + 'networks/')
        for network in response.json():
          self.networks.append(network['slug'])
      
      # so here we should have either a hard coded config list of networks, 
      # or a list of all networks grabbed from the MPCC.
      for network in self.networks: 
        
        # grab the network interface.
        response = self.js.session.get( self.js.mprovURL + 'networkinterfaces/?network=' + network)

        response = self.js.session.get( self.js.mprovURL + 'networkinterfaces/' + network + '/details')