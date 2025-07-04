from pwd import getpwnam
from jinja2 import Environment, PackageLoader, select_autoescape
from mprov_jobserver.plugins.plugin import JobServerPlugin
import os
import shutil, socket
import dns.resolver
import subprocess
import psutil 
import signal


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
        with open(self.dnsmasqConfDir + '/ipxe.conf', 'w') as conf:
            conf.write(jenv.get_template('dnsmasq/ipxe.conf.j2').render(data))
        jobquery = "&jobserver=" + str(self.js.id) + "&module=[\"dns-update\",\"dns-delete\",\"pxe-update\",\"dhcp-update\",\"pxe-delete\",\"dhcp-delete\"]"

        # # restart dnsmasq
        # os.system('systemctl enable dnsmasq')
        # os.system('systemctl restart dnsmasq')

        # look for the dnsmasq process id.
        pid=None
        process_name="dnsmasq"
        for proc in psutil.process_iter():
            if process_name in proc.name():
              pid = proc.pid
              break
        if pid is None:
            # no process, let's try to start it.
            # NOTE: This backgrounds dnsmasq.  Killing the jobserver should not kill dnsmasq unless
            # the jobserver is run in a container.
            subprocess.run([f"{self.dnsmasqBinary}", "--log-facility=-"])



        # copy in our ipxe.menu file.
        with open(self.tftproot + '/ipversionrouter.ipxe', 'w') as conf:
            conf.write(jenv.get_template('dnsmasq/ipversionrouter.ipxe.j2').render(data))
        # with open(self.tftproot + '/ipv6.ipxe', 'w') as conf:
        #     conf.write(jenv.get_template('dnsmasq/ipv6.ipxe.j2').render(data))
        with open(self.tftproot + '/menu.ipxe', 'w') as conf:
            conf.write(jenv.get_template('dnsmasq/menu.ipxe.j2').render(data))
        self.js.update_job_status(self.jobModule, 4, jobquery=jobquery + "&status=2")