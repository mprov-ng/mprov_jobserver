from jinja2 import Environment, PackageLoader, select_autoescape
from mprov_jobserver.plugins.plugin import JobServerPlugin
import os


jenv = Environment(
    loader=PackageLoader("mprov_jobserver"),
    autoescape=select_autoescape()
)

class DnsmasqDNSConfig(JobServerPlugin):
    dnsmasqConfDir=''
    mprovDnsmasqDir=''
    def load_config(self):
        return True
    def handle_jobs(self):

        # get the network informatioin
        response = self.js.session.get( self.js.mprovURL + 'networks/?managedns=True')
        data = {
            'networks': response.json(),
            'enableDNS': True,
        }
        with open(self.dnsmasqConfDir + 'dns.conf', 'w') as conf:
            conf.write(jenv.get_template('dnsmasq/dns.conf.j2').render(data))

        # make sure the mprov hosts dir exists
        os.makedirs(self.mprovDnsmasqDir + '/dns/', exist_ok=True)
        # now the interfaces
        for network in data['networks']: 
        
            # grab the network interface.
            print(self.js.mprovURL + 'networkinterfaces/?network=' + network['slug'])
            response = self.js.session.get( self.js.mprovURL + 'networkinterfaces/?network=' + network['slug'])
            if not self.checkHTTPStatus(response.status_code):
                print(f"Error: Issue with network {network['slug']}")
                continue
            data_hosts = {
                'enableDNS': True,
                'hosts': response.json(),
            }

            # grab the bmcs that are on this network.
            print(self.js.mprovURL + f"systembmcs/?network={network['id']}&detail")
            response = self.js.session.get(self.js.mprovURL + f"systembmcs/?network={network['id']}&detail")
            if self.checkHTTPStatus(response.status_code):
                # only do the bmcs if we get some.
                
                try: 
                    for bmc in response.json():
                        tmpHost = {
                            'ipaddress': bmc['ipaddress'],
                            'mac': bmc['mac'],
                            'hostname': bmc['system']['hostname'] + '-bmc',
                            'domain': network['domain']
                        }
                        data_hosts['hosts'].append(tmpHost)
                except:
                    pass
            # merge in the switches
            response = self.js.session.get( self.js.mprovURL + 'switches/?network=' + network['slug'])
            data_hosts['hosts'] = data_hosts['hosts'] + (response.json())
            # print(data_hosts)
            for idx, host in enumerate(data_hosts['hosts']):
                if 'mgmt_mac' in host:
                    data_hosts['hosts'][idx]['mac'] = host['mgmt_mac']
                if 'mgmt_ip' in host:
                    data_hosts['hosts'][idx]['ipaddress'] = host['mgmt_ip']
                data_hosts['hosts'][idx]['domain'] = network['domain']
                # print(data_hosts['hosts'][idx])

            with open(self.mprovDnsmasqDir + '/dns/' + network['slug'] + '-dns.conf', 'w') as conf:
                conf.write(jenv.get_template('dnsmasq/dns_host.conf.j2').render(data_hosts))
        # restart dnsmasq
        os.system('systemctl restart dnsmasq')

