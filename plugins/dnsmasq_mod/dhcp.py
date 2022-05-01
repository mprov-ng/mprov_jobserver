from jinja2 import Environment, PackageLoader, select_autoescape
from mprov.mprov_jobserver.plugins.plugin import JobServerPlugin

jenv = Environment(
    loader=PackageLoader("mprov.mprov_jobserver"),
    autoescape=select_autoescape()
)

class DnsmasqDHCPConfig(JobServerPlugin):
    enableTFTP=False
    
    def handle_jobs(self):
        pass