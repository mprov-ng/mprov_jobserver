from .dnsmasq_mod.config import DnsmasqConfig
from .dnsmasq_mod.dns import DnsmasqDNSConfig
from .dnsmasq_mod.dhcp import DnsmasqDHCPConfig
from .plugin import JobServerPlugin

# class to handle dnsmaq generation.
# This class can job modules:
# - pxe-update
# - pxe-delete
# - dns-update
# - dns-delete
# - dhcp-update
# - dhcp-delete
# If other jobmodules are running that can consume these jobmodules,
# you may have unexpected results.
class dnsmasq(JobServerPlugin):
  jobModule = 'dnsmasq'
  enableDNS=False
  enableDHCP=False
  enableTFTP=False
  dnsmasqConfDir=''
  mprovDnsmasqDir=''
  tftproot=''
  undionlyImg = ''
  dnsmasqUser=''
  threads = []
  def handle_jobs(self):
    # we can also run the DNS thread
    if self.enableDNS:
      # grab any DNS jobs.
      # See if we have any image-delete jobs, and take 'em if we do, else just exit
      jobquery = "&module=[\"dns-update\",\"dns-delete\"]"
      # print(jobquery)
      if not self.js.update_job_status(self.jobModule, 2, jobquery=jobquery + "&status=1"):
        pass # no jobs.
        self.enableDNS = False
      else:
        dnsThread = DnsmasqDNSConfig(self.js)
        dnsThread.dnsmasqConfDir = self.dnsmasqConfDir
        dnsThread.mprovDnsmasqDir = self.mprovDnsmasqDir
        dnsThread.start()
        self.threads.append(dnsThread)

    # and finally the DHCP thread
    if self.enableDHCP:
      # grab any DHCP/PXE jobs.
      # See if we have any image-delete jobs, and take 'em if we do, else just exit
      jobquery = "&module=[\"pxe-update\",\"dhcp-update\",\"pxe-delete\",\"dhcp-delete\"]"
      # print(jobquery)
      if not self.js.update_job_status(self.jobModule, 2, jobquery=jobquery + "&status=1"):
        pass # no jobs.
        self.enableDHCP = False
      else:
        dhcpThread = DnsmasqDHCPConfig(self.js)
        dhcpThread.dnsmasqConfDir = self.dnsmasqConfDir
        dhcpThread.mprovDnsmasqDir = self.mprovDnsmasqDir
        dhcpThread.tftproot=self.tftproot
        
        dhcpThread.enableTFTP = self.enableTFTP
        dhcpThread.start()
        self.threads.append(dhcpThread)
    # Based on our settings, let's start up the submodules for dnsmasq.
    if(self.enableDNS or self.enableDHCP):
      
      # DNS or DHCP is on, so let's run the config module
      confThread = DnsmasqConfig(self.js)
      confThread.dnsmasqConfDir=self.dnsmasqConfDir
      confThread.mprovDnsmasqDir = self.mprovDnsmasqDir
      confThread.tftproot = self.tftproot
      confThread.undionlyImg = self.undionlyImg
      confThread.dnsmasqUser = self.dnsmasqUser
      confThread.start()
      self.threads.append(confThread)    

    for thread in self.threads:
      thread.join()
      