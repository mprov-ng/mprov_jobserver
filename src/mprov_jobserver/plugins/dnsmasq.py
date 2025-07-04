from distutils.command.build import build
from .dnsmasq_mod.config import DnsmasqConfig
from .dnsmasq_mod.dns import DnsmasqDNSConfig
from .dnsmasq_mod.dhcp import DnsmasqDHCPConfig
from .plugin import JobServerPlugin
import sh, os, sys

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
  dnsmasqBinary="/usr/sbin/dnsmasq"
  threads = []
  ipxe_files = ['undionly.kpxe', 'snponly.efi', 'snponly_ipv4.efi']
  firstRun=True

  def load_config(self):
    result =  super().load_config()
    self.firstRun = self.js.firstrun
    if result:
      os.makedirs(f"{self.tftproot}", exist_ok=True)
      # Check for the ipxe files in the tftproot, and build it from 
      # git source if not there.
      buildIpxe=False
      for file in self.ipxe_files:
        if not os.path.exists(f"{self.tftproot}/{file}"):
          buildIpxe = True

      if buildIpxe:
        print("Rebuilding iPXE files, some or all are missing.")
        sh.dnf(['-y', 'install', 'mkisofs'])
        oldCwd = os.getcwd()
        os.chdir('/tmp')
        print("\tCheck out iPXE code to /tmp/ipxe/")
        try:
          sh.rm('-rf', '/tmp/ipxe')
          sh.git(['clone', 'https://github.com/ipxe/ipxe'])
          os.chdir('ipxe')
          sh.git(['checkout', 'e66552eeede19a91b1a52468a550b58fd031777b'])
        except:
          print("Error: unable to fetch ipxe source.  You may need to copy some files yourself.")
          return result
        
        print("\tRunning inintial build")
        os.chdir('src')
        print("\tPatching https enable...")
        with open("config/defaults/efi.h", "r") as f:
            lines = f.readlines()
        with open("config/defaults/efi.h", "w") as f:
            for line in lines:
                if "DOWNLOAD_PROTO_HTTPS" in line.strip("\n"):
                    f.write(f"#define DOWNLOAD_PROTO_HTTPS    /* Secure Hypertext Transfer Protocol */")
                else:
                  f.write(f"{line}")
        try:
          sh.make(['-j4', 'bin-x86_64-efi/snponly.efi', 'bin/undionly.kpxe'])
        except:
            print("Error: iPXE make command failed.")
            return result
        try:
          sh.cp(['-f','bin/undionly.kpxe', f"{self.tftproot}"])
        except:
          print(f"Error: Unable to copy bin/undionly.kpxe -> {self.tftproot}")
        try:
           sh.cp(['-f','bin-x86_64-efi/snponly.efi', f"{self.tftproot}"])
        except:
          print(f"Error: Unable to copy bin-x86_64-efi/snponly.efi -> {self.tftproot}")
        
        # now we will rebuild the EFI version with IPv6 support turned off
        print("\tPatching EFI to IPv4 only...")
        with open("config/defaults/efi.h", "r") as f:
            lines = f.readlines()
        with open("config/defaults/efi.h", "w") as f:
            for line in lines:
                if "NET_PROTO_IPV6" in line.strip("\n"):
                    f.write(f"//{line}")
                else:
                  f.write(f"{line}")
        print("\tRunning second build...")
        try:
          sh.make(['-j4','bin-x86_64-efi/snponly.efi'], _out=sys.stdout, _err=sys.stderr)
        except:
            print("Error: iPXE IPv4 only EFI make command failed.")
            sh.touch([f"{self.tftproot}/snponly_ipv4.efi"])
            sys.exit(1) #return result
        try:
           sh.cp(['-f','bin-x86_64-efi/snponly.efi', f"{self.tftproot}/snponly_ipv4.efi"])
        except:
          print(f"Error: Unable to copy bin-x86_64-efi/snponly.efi -> {self.tftproot}/snponly_ipv4.efi")
        print("Build Process finished.  Errors appear above.")
        os.chdir(oldCwd)

      pass
    return result
  def handle_jobs(self):
    self.firstRun = self.js.firstrun
    # we can also run the DNS thread
    if self.enableDNS:
      # grab any DNS jobs.
      # See if we have any image-delete jobs, and take 'em if we do, else just exit
      jobquery = "&module=[\"dns-update\",\"dns-delete\"]"
      # print(jobquery)
      if not self.js.update_job_status(self.jobModule, 2, jobquery=jobquery + "&status=1") and not self.firstRun:
        pass # no jobs.
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
      if not self.js.update_job_status(self.jobModule, 2, jobquery=jobquery + "&status=1") and not self.firstRun:
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
    # wait for DNS and DHCP to finish.
    for thread in self.threads:
      thread.join()

    # Based on our settings, let's start up the submodules for dnsmasq.
    if(self.enableDNS or self.enableDHCP):
      
      # DNS or DHCP is on, so let's run the config module
      # the config module for dnsmasq will also be responsible for
      # maintaining the service.
      confThread = DnsmasqConfig(self.js)
      confThread.dnsmasqConfDir=self.dnsmasqConfDir
      confThread.mprovDnsmasqDir = self.mprovDnsmasqDir
      confThread.tftproot = self.tftproot
      confThread.dnsmasqUser = self.dnsmasqUser
      confThread.start()
      confThread.join() # wait for the conf to finish.

      
