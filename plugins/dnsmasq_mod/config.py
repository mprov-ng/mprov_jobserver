from jinja2 import Environment, PackageLoader, select_autoescape
from mprov.mprov_jobserver.plugins.plugin import JobServerPlugin

jenv = Environment(
    loader=PackageLoader("mprov.mprov_jobserver"),
    autoescape=select_autoescape()
)

class DnsmasqConfig(JobServerPlugin):
    
    def handle_jobs(self):
        # Generates some general configuration stuff 
        data={
            'mpcc_host': '10.1.2.80',
            'enableDHCP': True,
        }
        print(jenv.get_template('dnsmasq/ipxe.conf.j2').render(data))