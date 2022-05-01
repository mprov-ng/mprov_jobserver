from jinja2 import Environment, PackageLoader, select_autoescape
from mprov.mprov_jobserver.plugins.plugin import JobServerPlugin

jenv = Environment(
    loader=PackageLoader("mprov.mprov_jobserver"),
    autoescape=select_autoescape()
)

class DnsmasqConfig(JobServerPlugin):
    dnsmasqConfDir=''
    mprovDnsmasqDir=''
    def load_config(self):
        return True
    def handle_jobs(self):
        # Generates some general configuration stuff 
        data={
            'mprov_url': self.js.mprovURL,
            'enableDHCP': True,
        }
        with open(self.dnsmasqConfDir + 'ipxe.conf', 'w') as conf:
            conf.write(jenv.get_template('dnsmasq/ipxe.conf.j2').render(data))