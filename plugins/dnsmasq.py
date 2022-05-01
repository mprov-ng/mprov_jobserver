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
  dnsmasqConfDir='/etc/dnsmasq.d/'
  mprovDnsmasqDir='/var/lib/mprov/'
  threads = []
  def handle_jobs(self):
    # Based on our settings, let's start up the submodules for dnsmasq.
    if(self.enableDNS or self.enableDHCP):
      # DNS or DHCP is on, so let's run the config module
      confThread = DnsmasqConfig(self.js)
      confThread.start()
      self.threads.append(confThread)

    # we can also run the DNS thread
    if self.enableDNS:
      dnsThread = DnsmasqDNSConfig(self.js)
      dnsThread.start()
      self.threads.append(dnsThread)

    # and finally the DHCP thread
    if self.enableDHCP:
      dhcpThread = DnsmasqDHCPConfig(self.js)
      dhcpThread.enableTFTP = self.enableTFTP
      dhcpThread.start()
      self.threads.append(dhcpThread)
      
    for thread in self.threads:
      thread.join()
      