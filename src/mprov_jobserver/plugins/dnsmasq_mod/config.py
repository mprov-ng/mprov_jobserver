from pwd import getpwnam
from jinja2 import Environment, PackageLoader, select_autoescape
from mprov_jobserver.plugins.plugin import JobServerPlugin
import os
import shutil, socket
import dns.resolver
import subprocess
import psutil
import signal
from .service import write_if_changed, running_dnsmasq

DNS_MODULES = '["dns-update","dns-delete"]'
DHCP_MODULES = '["pxe-update","dhcp-update","pxe-delete","dhcp-delete"]'


jenv = Environment(
    loader=PackageLoader("mprov_jobserver"),
    autoescape=select_autoescape(),
    trim_blocks=True,
    lstrip_blocks=True,
)

class DnsmasqConfig(JobServerPlugin):
    dnsmasqConfDir=''
    mprovDnsmasqDir=''
    tftproot=''
    dnsmasqUser=''
    hostname=''
    bootserver6=''
    dnsmasqBinary="/usr/sbin/dnsmasq"
    dnsmasqPidFile="/var/run/dnsmasq.pid"
    # set by the dnsmasq plugin from its DNS and DHCP workers: whether they
    # changed dnsmasq's main config or only its hosts files (#108), and how
    # each one did (#179): True, False (it crashed), or None (it didn't run,
    # so it claimed no jobs).
    mainChanged=False
    hostsChanged=False
    dnsOk=None
    dhcpOk=None
    def __init__(self, js):
        super().__init__(js)
        self.hostname = socket.gethostname()
        if '.' in self.hostname:
            self.hostname, _ = self.hostname.split('.', 1)
        # try to get an IPv6 address for ourself
        try:
            answer = dns.resolver.resolve(self.hostname, "AAAA")
            for ipv6ip in answer.rrset:
                ipv6ip = str(ipv6ip)
                if not ipv6ip.startswith("fe80"):
                    self.bootserver6 = f"[{ipv6ip}]"
                    break
        except Exception as e:
            # print(f'Error: {e}')
            pass
        if self.bootserver6 == '':
            self.bootserver6 = self.hostname
        
    def load_config(self):
        return True
    def handle_jobs(self):
        # Generates some general configuration stuff 
        data={
            'mprov_url': self.js.mprovURL,
            'enableDHCP': True,
            'bootserver': self.hostname,
            'bootserver6': self.bootserver6,
        }
        os.makedirs(self.dnsmasqConfDir, exist_ok=True)
        os.makedirs(self.tftproot, exist_ok=True)
        if write_if_changed(self.dnsmasqConfDir + '/ipxe.conf', jenv.get_template('dnsmasq/ipxe.conf.j2').render(data)):
            self.mainChanged = True

        # copy in our ipxe.menu file.  TFTP reads these per request, so they
        # need no reload, but a node mustn't get half a file either.
        write_if_changed(self.tftproot + '/ipversionrouter.ipxe', jenv.get_template('dnsmasq/ipversionrouter.ipxe.j2').render(data))
        # with open(self.tftproot + '/ipv6.ipxe', 'w') as conf:
        #     conf.write(jenv.get_template('dnsmasq/ipv6.ipxe.j2').render(data))
        write_if_changed(self.tftproot + '/menu.ipxe', jenv.get_template('dnsmasq/menu.ipxe.j2').render(data))

        dnsmasqOk = self.update_dnsmasq()

        # mark the jobs each worker claimed by how it did (#179): a crashed
        # worker, or a dnsmasq that won't start, leaves the old config live.
        for modules, workerOk in ((DNS_MODULES, self.dnsOk), (DHCP_MODULES, self.dhcpOk)):
            if workerOk is None:
                continue
            status = 4 if workerOk and dnsmasqOk else 3
            if status == 3:
                print(f"Error: failing the running {modules} jobs: " + ("dnsmasq didn't start." if workerOk else "generating the config failed."))
            jobquery = "&jobserver=" + str(self.js.id) + "&module=" + modules
            self.js.update_job_status(self.jobModule, status, jobquery=jobquery + "&status=2")

    def update_dnsmasq(self):
        '''
        Make the running dnsmasq use what was written (#108).  It reads its
        main config (dns.conf, dhcp.conf, ipxe.conf) only when it starts, so
        a change there needs a restart.  It watches the hosts directories,
        but the watch only adds records, never drops or replaces them, so
        changed or removed hosts need a SIGHUP, which re-reads them without
        stopping it.  Nothing changed: leave it alone.  Not running: start
        it.  False if it should be running and isn't.
        '''
        proc = running_dnsmasq(self.dnsmasqBinary, self.dnsmasqPidFile)
        if proc is not None and self.mainChanged:
            print("dnsmasq's main config changed, restarting dnsmasq.")
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except psutil.TimeoutExpired:
                print("Warning: dnsmasq didn't stop on SIGTERM, killing it.")
                proc.kill()
                proc.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass
            proc = None
        elif proc is not None and self.hostsChanged:
            print("dnsmasq's hosts changed, reloading them (SIGHUP).")
            try:
                proc.send_signal(signal.SIGHUP)
            except psutil.NoSuchProcess:
                proc = None
        if proc is None:
            # NOTE: This backgrounds dnsmasq.  Killing the jobserver should not kill dnsmasq unless
            # the jobserver is run in a container.
            result = subprocess.run([f"{self.dnsmasqBinary}", "--log-facility=-", f"--pid-file={self.dnsmasqPidFile}"])
            if result.returncode != 0:
                print(f"Error: {self.dnsmasqBinary} failed to start (exit {result.returncode}).")
                return False
        return True