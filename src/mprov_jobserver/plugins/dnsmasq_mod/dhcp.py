from jinja2 import Environment, PackageLoader, select_autoescape
from mprov_jobserver.plugins.plugin import JobServerPlugin
import os

jenv = Environment(
    loader=PackageLoader("mprov_jobserver"),
    autoescape=select_autoescape()
)

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
                'hosts': response.json(),
            }

            # merge in the switches
            response = self.js.session.get( self.js.mprovURL + 'switches/?network=' + network['slug'])
            data_hosts['hosts'] = data_hosts['hosts'] + (response.json())
            # print(data_hosts)
            for idx, host in enumerate(data_hosts['hosts']):
                # print(host)
                if 'mgmt_mac' in host:
                    data_hosts['hosts'][idx]['mac'] = host['mgmt_mac']
                if 'mgmt_ip' in host:
                    data_hosts['hosts'][idx]['ipaddress'] = host['mgmt_ip']
                data_hosts['hosts'][idx]['network'] = network['slug']
            with open(self.mprovDnsmasqDir + '/dhcp/' + network['slug'] + '-dhcp.conf', 'w') as conf:
                conf.write(jenv.get_template('dnsmasq/dhcp_host.conf.j2').render(data_hosts))
        # restart dnsmasq
        os.system('systemctl restart dnsmasq')

