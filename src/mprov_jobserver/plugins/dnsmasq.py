from .dnsmasq_mod.config import DnsmasqConfig
from .dnsmasq_mod.dns import DnsmasqDNSConfig
from .dnsmasq_mod.dhcp import DnsmasqDHCPConfig
from .plugin import JobServerPlugin
import sh, os, sys, shutil, tempfile

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
        self.build_ipxe()
    return result

  def build_ipxe(self):
    '''
    Build the iPXE files into the tftproot.  Plugins are threads of one
    process, so this never calls os.chdir(): that would move every other
    plugin's working directory, and theirs would move ours mid-build (#91).
    Everything uses absolute paths, or _cwd for commands.  A file is only
    copied once it has built, so a failed build is retried on the next start.
    '''
    buildDir = tempfile.mkdtemp(prefix="mprov-ipxe-")
    src = f"{buildDir}/ipxe/src"
    try:
      sh.dnf(['-y', 'install', 'mkisofs'])
      print(f"\tCheck out iPXE code to {buildDir}/ipxe/")
      try:
        sh.git(['clone', 'https://github.com/ipxe/ipxe'], _cwd=buildDir)
        sh.git(['checkout', 'e66552eeede19a91b1a52468a550b58fd031777b'], _cwd=f"{buildDir}/ipxe")
      except Exception as e:
        print(f"Error: unable to fetch ipxe source.  You may need to copy some files yourself. {e}")
        return

      # config/local/general.h is included after iPXE's defaults, so it
      # can turn features on and off without patching iPXE's own headers.
      os.makedirs(f"{src}/config/local", exist_ok=True)
      print("\tEnabling HTTPS...")
      with open(f"{src}/config/local/general.h", "w") as f:
        f.write("#define DOWNLOAD_PROTO_HTTPS\n")

      print("\tRunning initial build...")
      try:
        sh.make(['-j4', 'bin-x86_64-efi/snponly.efi', 'bin/undionly.kpxe'], _cwd=src)
      except Exception as e:
        print(f"Error: iPXE make command failed. {e}")
        return
      self.copy_ipxe_file(f"{src}/bin/undionly.kpxe", "undionly.kpxe")
      self.copy_ipxe_file(f"{src}/bin-x86_64-efi/snponly.efi", "snponly.efi")

      # now we will rebuild the EFI version with IPv6 support turned off
      print("\tTurning off IPv6 for the IPv4 only EFI...")
      with open(f"{src}/config/local/general.h", "a") as f:
        f.write("#undef NET_PROTO_IPV6\n")
      print("\tRunning second build...")
      try:
        sh.make(['-j4', 'bin-x86_64-efi/snponly.efi'], _cwd=src, _out=sys.stdout, _err=sys.stderr)
      except Exception as e:
        print(f"Error: iPXE IPv4 only EFI make command failed. {e}")
        return
      self.copy_ipxe_file(f"{src}/bin-x86_64-efi/snponly.efi", "snponly_ipv4.efi")
      print("Build Process finished.  Errors appear above.")
    finally:
      shutil.rmtree(buildDir, ignore_errors=True)

  def copy_ipxe_file(self, built, name):
    try:
      shutil.copyfile(built, f"{self.tftproot}/{name}")
    except Exception as e:
      print(f"Error: Unable to copy {built} -> {self.tftproot}/{name}: {e}")

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

      
