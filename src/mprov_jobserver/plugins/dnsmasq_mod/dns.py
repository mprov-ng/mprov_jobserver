from jinja2 import Environment, PackageLoader, select_autoescape
from mprov_jobserver.plugins.plugin import JobServerPlugin
import os, socket, ipaddress, netifaces
from socket import AF_INET, AF_INET6


jenv = Environment(
    loader=PackageLoader("mprov_jobserver"),
    autoescape=select_autoescape()
)

class DnsmasqDNSConfig(JobServerPlugin):
    dnsmasqConfDir=''
    mprovDnsmasqDir=''
    hostname=''
    def __init__(self, js):
        super().__init__(js)
        self.hostname = socket.gethostname()
        if '.' in self.hostname:
            self.hostname, _ = self.hostname.split('.', 1)
    def load_config(self):
        return True
    def _inmProvNet(self, ipaddr, net):
        iface_ip = ipaddress.ip_address(ipaddr)
        iface_net = ipaddress.ip_network(f"{net['subnet']}/{net['netmask']}", strict=False)
        if iface_ip in iface_net:
            return True
        return False
    def _addSelfToNet(self, network):
        selfHosts = []
        for iface in netifaces.interfaces():
            # do not run on the local interface.
            if iface == 'lo':
                continue

            iface_details = netifaces.ifaddresses(iface)
            # only process IPv4 and IPv6 addresses.
            if AF_INET in iface_details or AF_INET6 in iface_details:
                for af in [AF_INET, AF_INET6]:
                    if af in iface_details:
                        for address in iface_details[af]:
                            if "%" in address['addr']:
                                address['addr'], _ = address['addr'].split('%', 1)
                            if self._inmProvNet(address['addr'], network):
                                selfHosts.append({
                                    'ipaddress': address['addr'],
                                    'ipv6ll': '',
                                    'hostname': self.hostname,
                                    'domain': network['domain'] + f"# {iface}"
                                })
            # # grab and add the LL ipv6 ip addresses.
            # if AF_INET6 in netifaces.ifaddresses(iface):
            #     for v6ip in netifaces.ifaddresses(iface)[AF_INET6]:
            #         if v6ip['addr'].startswith("fe80:"):
            #             if "%" in v6ip['addr']:
            #                 v6ip['addr'], _ = v6ip['addr'].split("%", 1)
            #             selfHosts.append({
            #                 'ipaddresss': '',
            #                 'ipv6ll': v6ip['addr'],
            #                 'hostname': self.hostname,
            #                 'domain': network['domain'] + f"# {iface}"
            #             })
                


        return selfHosts
               
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
            
        
            # grab the system network interfaces that are on this network.
            print(self.js.mprovURL + 'networkinterfaces/?network=' + network['slug'])
            response = self.js.session.get( self.js.mprovURL + 'networkinterfaces/?network=' + network['slug'])
            if not self.checkHTTPStatus(response.status_code):
                print(f"Error: Issue with network {network['slug']}")
                continue
            data_hosts = {
                'enableDNS': True,
                'hosts': response.json(),
            }
            # add ourself if we are listening on this net
            data_hosts['hosts'].extend(self._addSelfToNet(network))

            # grab the bmcs that are on this network.
            print(self.js.mprovURL + f"systembmcs/?network={network['id']}&detail")
            response = self.js.session.get(self.js.mprovURL + f"systembmcs/?network={network['id']}&detail")
            if self.checkHTTPStatus(response.status_code):
                # only do the bmcs if we get some.
                
                try: 
                    for bmc in response.json():
                        tmpHost = {
                            'ipaddress': bmc['ipaddress'],
                            'ipv6ll': "", 
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

