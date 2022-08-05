from ipaddress import IPv4Network
import ipaddress
import os
from .plugin import JobServerPlugin
import time
import json

"""
Node Auto Detection System

Parts for LLDP parsing are borrowed from https://github.com/GoozeyX/lldp.discovery

"""
import netifaces
import fcntl, struct

from ctypes import c_char, c_short, Structure
from fcntl import ioctl
from socket import socket, htons, inet_ntoa
from socket import AF_PACKET, AF_INET, SOCK_DGRAM, SOCK_RAW
from socket import gaierror

from struct import pack, unpack

from dmidecode import DMIDecode

## Magic constants from `/usr/include/linux/if_ether.h`:
ETH_P_ALL = 0x0003
ETH_ALEN = 6
ETH_HLEN = 14

## LLDP Ethernet Protocol:
# LLDP Length:
LLDP_TLV_TYPE_BIT_LEN = 7
LLDP_TLV_LEN_BIT_LEN = 9
LLDP_TLV_HEADER_LEN = 2         # 7 + 9 = 16
LLDP_TLV_OUI_LEN = 3
LLDP_TLV_SUBTYPE_LEN = 1
# LLDP Protocol BitFiddling Mask:
LLDP_TLV_TYPE_MASK = 0xfe00
LLDP_TLV_LEN_MASK = 0x1ff
# LLDP Protocol ID:
LLDP_PROTO_ID = 0x88cc
# LLDP TLV Type:
LLDP_TLV_TYPE_CHASSISID = 0x01
LLDP_TLV_TYPE_PORTID = 0x02
LLDP_TLV_TYPE_PORTDESC = 0x04
LLDP_TLV_DEVICE_NAME = 0x05
LLDP_PDUEND = 0x00
LLDP_TLV_ORGANIZATIONALLY_SPECIFIC = 0x7f
# LLDP TLV OUI Type:
LLDP_TLV_OUI_802_1 = 0x0008c2
LLDP_TLV_OUI_802_3 = 0x00120f

## Magic string for unpack packet:
UNPACK_ETH_HEADER_DEST = '!%s' % ('B' * ETH_ALEN)
UNPACK_ETH_HEADER_SRC = '!%s' % ('B' * ETH_ALEN)
UNPACK_ETH_HEADER_PROTO = '!H'

## Magic string for unpack LLDP packet:
UNPACK_LLDP_TLV_TYPE = '!H'
UNPACK_LLDP_TLV_OUI = '!%s' % ('B' * LLDP_TLV_OUI_LEN)
UNPACK_LLDP_TLV_SUBTYPE = '!B'

## Other info about network under linux:
NETDEV_INFO = '/proc/net/dev'
SIOCGIFADDR = 0x8915    # Socket opt for get ip addr under linux
SIOCSIFHWADDR = 0x8927  # Socket opt for get mac addr under linux
SIOCGIFFLAGS = 0x8913   # `G` for Get socket flags
SIOCSIFFLAGS = 0x8914   # `S` for Set socket flags
IFF_PROMISC = 0x100     # Enter Promiscuous mode

def promiscuous_mode(interface, sock, enable=False):
    """ Enable/Disable NIC promiscuous mode via `ioctl` system call
            with c-compatible `ifreq` struct and `SIOC[G|S]IFFLAGS` """

    ifr = ifreq()
    ifr.ifr_ifrn = bytes(interface, 'utf-8')
    ioctl(sock.fileno(), SIOCGIFFLAGS, ifr)

    if enable:
        ifr.ifr_flags |= IFF_PROMISC
    else:
        ifr.ifr_flags &= ~IFF_PROMISC
    ioctl(sock.fileno(), SIOCSIFFLAGS, ifr)


def unpack_ethernet_frame(packet):
    """ Unpack ethernet frame """

    eth_header = packet[0:ETH_HLEN]
    eth_dest_mac = unpack(UNPACK_ETH_HEADER_DEST, eth_header[0:ETH_ALEN])
    eth_src_mac = unpack(UNPACK_ETH_HEADER_SRC, eth_header[ETH_ALEN:ETH_ALEN*2])
    eth_protocol = unpack(UNPACK_ETH_HEADER_PROTO, eth_header[ETH_ALEN*2:ETH_HLEN])[0]
    eth_payload = packet[ETH_HLEN:]

    return (eth_header, eth_dest_mac, eth_src_mac, eth_protocol, eth_payload)


def covert_hex_string(decimals):
    """ Covert decimals to hex string which start with `0x`, 
            and `strip` by `0x` """
    return [ hex(decimal).strip('0x').rjust(2, '0') for decimal in decimals ]


def unpack_lldp_frame(eth_payload):
    """ Unpack lldp frame """

    while eth_payload:

        tlv_header = unpack(UNPACK_LLDP_TLV_TYPE, eth_payload[:LLDP_TLV_HEADER_LEN])
        tlv_type = (tlv_header[0] & LLDP_TLV_TYPE_MASK) >> LLDP_TLV_LEN_BIT_LEN
        tlv_data_len = (tlv_header[0] & LLDP_TLV_LEN_MASK)
        tlv_payload = eth_payload[LLDP_TLV_HEADER_LEN:LLDP_TLV_HEADER_LEN + tlv_data_len]

        # These headers only available with 
        #   `LLDP_TLV_ORGANIZATIONALLY_SPECIFIC` TLV
        tlv_oui = None
        tlv_subtype = None

        if tlv_type == LLDP_TLV_ORGANIZATIONALLY_SPECIFIC:
            _tlv_oui = unpack(UNPACK_LLDP_TLV_OUI, tlv_payload[:LLDP_TLV_OUI_LEN])
            tlv_subtype = unpack(UNPACK_LLDP_TLV_SUBTYPE, 
                            tlv_payload[LLDP_TLV_OUI_LEN:LLDP_TLV_OUI_LEN + LLDP_TLV_SUBTYPE_LEN])[0]
            tlv_payload = tlv_payload[LLDP_TLV_OUI_LEN + LLDP_TLV_SUBTYPE_LEN:]
                
            # Covert oui from list to hex/decimals
            tlv_oui = str()
            for bit in _tlv_oui:
                tlv_oui += hex(bit).strip('0x').rjust(2, '0')
            tlv_oui = int(tlv_oui, 16)

        elif tlv_type == LLDP_PDUEND:
            break

        eth_payload = eth_payload[LLDP_TLV_HEADER_LEN + tlv_data_len:]

        yield (tlv_header, tlv_type, tlv_data_len, tlv_oui, \
                                        tlv_subtype, tlv_payload)
    


class ifreq(Structure):
    """ C-compatible `ifreq` struct """
    _fields_ = [("ifr_ifrn", c_char * 16),
                ("ifr_flags", c_short)]


class nads(JobServerPlugin):
  jobModule = 'nads'
  provIntf = ''
  maxLLDPWait = 60
  port = None
  switch = None
  mac = None
  reboot = False

  def getLLDP(self):
    startTime = time.time()

    # setup a capture socket.
    capture_sock = socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ALL))

    # enable promiscuous mode on the interface. 
    promiscuous_mode(self.provIntf, capture_sock, True)
    print("Start at: " + str(startTime))
    # grab some traffic and process it for LLDP
    while(time.time() < (startTime + self.maxLLDPWait)):
      # print("Current: " + str(time.time()))
      # print("End at: " + str(startTime + self.maxLLDPWait))
      # grab a packet.
      packet = capture_sock.recvfrom(65565)
      packet = packet[0]
      eth_protocol, eth_payload = unpack_ethernet_frame(packet)[3:]

      if eth_protocol == LLDP_PROTO_ID:
        # turn off promiscuous mode while we process our packet.  
        # XXX: Should we wait until we are done to turn this off?
        promiscuous_mode(self.provIntf, capture_sock, False)

        for tlv_parse_rv in unpack_lldp_frame(eth_payload):
  
          tlv_header, tlv_type, tlv_data_len, tlv_oui, tlv_subtype, tlv_payload \
                                                                  = tlv_parse_rv

          # if tlv_type == LLDP_TLV_TYPE_PORTDESC:
          #   self.port = tlv_payload.decode('utf-8')
          if tlv_type == LLDP_TLV_DEVICE_NAME:
            self.switch= tlv_payload.decode('utf-8')
          elif tlv_type == LLDP_TLV_TYPE_PORTID:
            self.port = tlv_payload.decode('utf-8')

          # exit our loops, we have what we came for.
          if  self.port is not None and self.switch is not None:
            # print("Exiting for loop: '" + self.port + "' and '" + self.switch + "'")
            break
      #time.sleep(1)
      if  self.port is not None and self.switch is not None:
        # print("Exiting while loop: '" + self.port + "' and '" + self.switch + "'")
        break
    # print("Current: " + str(time.time()))

    if self.port is None or self.switch is None:
      # if we get here and don't have a port and a switch name,
      # we cannot continue.
      print("Error: Unable to detect switch and port via LLDP")
      return False
    # if we get here, we're all set.
    return True


  def getMac(self):
    s = socket(AF_INET, SOCK_DGRAM)
    info = fcntl.ioctl(s.fileno(), 0x8927,  struct.pack('256s', bytes(self.provIntf, 'utf-8')))
    # print(info[18:24])
    # for byte in info[18:24]:
      
    #   print(hex(byte))
    return ':'.join(['%02x' % char for char in info[18:24]])

  def detect_provIntf(self):
    # try to detect our provisioning interface.

    # get a list of interfaces on this system.
    for iface in netifaces.interfaces():
      if iface == 'lo':
        continue
      iface_details = netifaces.ifaddresses(iface)
      if netifaces.AF_INET in iface_details:
        iface_addr = iface_details[netifaces.AF_INET]
        #print(iface_addr)
        mask = IPv4Network(f"0.0.0.0/{iface_addr[0]['netmask']}").prefixlen
        #print(mask)
        iface_ip = ipaddress.ip_address(iface_addr[0]['addr'])
        iface_net = ipaddress.ip_network(f"{iface_ip}/{mask}", strict=False)
        if iface_ip in iface_net:
          return iface
    return None

  def handle_jobs(self):
    # see if we are being called from a runonce command
    if not self.js.runonce:
      print("Error: script-runner must be run in a 'runonce' jobserver session.")
      return False
    
    # attempt to get our provision interface.
    self.provIntf = self.detect_provIntf()
    if self.provIntf is None:
      print("Error: Unable to detect what interface we should provision over.")
    
    # if we cannot get LLDP then exit.
    if self.getLLDP() == False:
      return False

    # grab the mac of the prov interface.
    self.mac = self.getMac()

    if self.port.find('/') >= 0:
      # we need to parse the port out of the port id.
      if self.port.rfind('/') >= 0:
        self.port = self.port[self.port.rfind('/')+1:]

    # if we got LLDP, tell the MPCC who we are, or better put, send it our switch, port, and mac.
    # and let it sort it out.
    query = '/systems/register'

    # grab our DMIDecode information
    dmi = DMIDecode()
    data = {
      'switch': self.switch,
      'port': self.port,
      'vendor': dmi.manufacturer(),
      'model': dmi.model(),
      'mac': self.mac,
    }
    print("Attempting to register with MPCC...")
    print(data)
    response = self.js.session.post(f"{self.js.mprovURL}{query}", data=json.dumps(data))
    if response.status_code == 200:
      print("We were able to register.")
      # Issue  a reboot if we are supposed to.
      if self.reboot:
        os.system("/sbin/reboot")
      return True
    print("There was a problem registering with the MPCC.")

    return False