from pwd import getpwnam
from jinja2 import Environment, PackageLoader, select_autoescape
from mprov_jobserver.plugins.plugin import JobServerPlugin
import os
import shutil, socket

jenv = Environment(
    loader=PackageLoader("mprov_jobserver"),
    autoescape=select_autoescape()
)

class DnsmasqConfig(JobServerPlugin):
    dnsmasqConfDir=''
    mprovDnsmasqDir=''
    tftproot=''
    dnsmasqUser=''
    hostname=''
    def __init__(self, js):
        super().__init__(js)
        self.hostname = socket.gethostname()
        if '.' in self.hostname:
            self.hostname, _ = self.hostname.split('.', 1)
    def load_config(self):
        return True
    def handle_jobs(self):
        # Generates some general configuration stuff 
        data={
            'mprov_url': self.js.mprovURL,
            'enableDHCP': True,
            'bootserver': self.hostname
        }
        os.makedirs(self.dnsmasqConfDir, exist_ok=True)
        os.makedirs(self.tftproot, exist_ok=True)
        with open(self.dnsmasqConfDir + '/ipxe.conf', 'w') as conf:
            conf.write(jenv.get_template('dnsmasq/ipxe.conf.j2').render(data))
        jobquery = "&jobserver=" + str(self.js.id) + "&module=[\"dns-update\",\"dns-delete\",\"pxe-update\",\"dhcp-update\",\"pxe-delete\",\"dhcp-delete\"]"

        # restart dnsmasq
        os.system('systemctl restart dnsmasq')
        # copy in our ipxe.menu file.
        
        with open(self.tftproot + '/menu.ipxe', 'w') as conf:
            conf.write(jenv.get_template('dnsmasq/menu.ipxe.j2').render(data))
        self.js.update_job_status(self.jobModule, 4, jobquery=jobquery + "&status=2")