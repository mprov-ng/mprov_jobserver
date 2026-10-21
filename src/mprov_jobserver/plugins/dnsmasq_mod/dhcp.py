from jinja2 import Environment, PackageLoader, select_autoescape
from mprov_jobserver.plugins.plugin import JobServerPlugin
import os
import psutil 
import signal 

jenv = Environment(
    loader=PackageLoader("mprov_jobserver"),
    autoescape=select_autoescape(),
    trim_blocks=True,
    lstrip_blocks=True,
)

def host_list(response):
    # The hosts in an mPCC list response, or [] if it isn't a list, e.g. the
    # 404 with a null body the mPCC returns for a network that has gone.
    try:
        hosts = response.json()
    except ValueError:
        return []
    return hosts if isinstance(hosts, list) else []

class DnsmasqDHCPConfig(JobServerPlugin):
    enableTFTP=False
    dnsmasqConfDir=''
    mprovDnsmasqDir='' 
    tftproot=''
    def load_config(self):
        return True
    def handle_jobs(self):
        # get the network informatioin
        response = self.js.session.get( self.js.mprovURL + 'networks/?isdhcp=True')
        # print(self.enableTFTP)
        data = {
            'networks': response.json(),
            'enableDHCP': True,
            'enableTFTP': self.enableTFTP,
            'tftproot': self.tftproot,
        }
        with open(self.dnsmasqConfDir + 'dhcp.conf', 'w') as conf:
            conf.write(jenv.get_template('dnsmasq/dhcp.conf.j2').render(data))

        # make sure the mprov hosts dir exists
        os.makedirs(self.mprovDnsmasqDir + '/dhcp/', exist_ok=True)
        # now the interfaces
        for network in data['networks']: 
        
            # grab the network interface.
            response = self.js.session.get( self.js.mprovURL + 'networkinterfaces/?network=' + network['slug'])
            data_hosts = {
                'enableDHCP': True,
                'hosts': host_list(response),
            }

            # merge in the switches
            response = self.js.session.get( self.js.mprovURL + 'switches/?network=' + network['slug'])
            data_hosts['hosts'] = data_hosts['hosts'] + host_list(response)
            for host in data_hosts['hosts']:
                if 'mgmt_mac' in host:
                    host['mac'] = host['mgmt_mac']
                if 'mgmt_ip' in host:
                    host['ipaddress'] = host['mgmt_ip']
                # the per-network options in dhcp.conf match on the slug.
                host['network'] = network['slug']
            with open(self.mprovDnsmasqDir + '/dhcp/' + network['slug'] + '-dhcp.conf', 'w') as conf:
                conf.write(jenv.get_template('dnsmasq/dhcp_host.conf.j2').render(data_hosts))
        # # restart dnsmasq
        # os.system('systemctl restart dnsmasq')
                # look for the dnsmasq process id.
        pid=None
        process_name="dnsmasq"
        for proc in psutil.process_iter():
            if process_name in proc.name():
              pid = proc.pid
              break
        if pid is not None:
            # process was found, kill it and let conf restart it.
            os.kill(pid, signal.SIGKILL)

