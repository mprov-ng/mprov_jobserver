"""
InfiniBand fabric discovery (#15).

Runs ibnetdiscover on a host with an HCA on the fabric, every `interval`
seconds, and records what it finds in the mPCC through its ordinary
/switches/, /switchports/ and /networkinterfaces/ endpoints:

- each IB switch, by its node GUID (created if the mPCC doesn't have it,
  named from its node description);
- each switch port a node's HCA is plugged into;
- each HCA port's GUID and switch port, on the node's IB network interface,
  found by that GUID or else by the hostname in the HCA's node description.

It only fills in what's empty: a GUID or switch port an admin has set is
never overwritten, a different one is logged instead.

It writes as a person, with a staff user's API token, not with the job
server's certificate, which may not write any of these.  The mPCC takes the
certificate first when a request has one, so the token goes over a session
of its own, without it.  The token user needs view on systems, networks,
network types, switches, switch ports and network interfaces; add and
change on switches; add on switch ports; and change on network interfaces.

The GUID fields came in mPCC 0.7.6; an older one refuses ?guid=, and the
run is skipped.
"""
import json
import os
import re
import subprocess
import time
from urllib.parse import quote

import requests

from .plugin import JobServerPlugin
from mprov_jobserver.log import get_logger

log = get_logger(__name__)

# The job server reloads a module and makes a new instance of its plugin on
# every heartbeat, so when the last run was is kept on the job server.
LAST_RUN = '_ib_discover_last_run'

# One line per port in `ibnetdiscover -p` (dump_ports_report() in
# rdma-core's infiniband-diags/ibnetdiscover.c): type, LID, port number,
# port GUID, width and speed, then the port it's linked to and both node
# descriptions:
#   SW     2  1 0x0002c903004e6fd0 4X FDR - CA     3  1 0x0002c903000f1235 ( 'MF0;ibsw01:SX6036/U1' - 'node01 mlx4_0' )
# or, for a port that's down, only its own node description:
#   SW     2  4 0x0002c903004e6fd0 4X SDR                                    'MF0;ibsw01:SX6036/U1'
# A switch's ports all have the switch's port GUID, which is what's kept as
# its GUID; on the switches we know it's the same as its node GUID.  An
# HCA port's is that port's own.
PORT = re.compile(r"^\s*(SW|CA|RT)\s+\d+\s+(\d+)\s+0x([0-9a-fA-F]{16})\b")
PEER = re.compile(r"\s-\s+(SW|CA|RT)\s+\d+\s+(\d+)\s+0x([0-9a-fA-F]{16})\s+\(\s*'(.*)'\s+-\s+'(.*)'\s*\)\s*$")
DOWN = re.compile(r"'(.*)'\s*$")

HOSTNAME = re.compile(r"[a-z0-9][a-z0-9.-]{0,253}")


def guid(hex16):
  """ A GUID as the mPCC stores it: 0x and 16 lowercase hex digits. """
  return '0x' + hex16.lower()


class Fabric:
  """ What one ibnetdiscover pass found: switches by GUID with their node
  descriptions, and the links from a switch port to an HCA port. """

  def __init__(self):
    self.switches = {}
    # HCA port GUID -> (switch GUID, switch port, HCA port, HCA description)
    self.links = {}

  def add_switch(self, switch_guid, desc):
    if desc or switch_guid not in self.switches:
      self.switches[switch_guid] = desc

  def add_link(self, switch_guid, switch_desc, switch_port, ca_guid, ca_port, ca_desc):
    self.add_switch(switch_guid, switch_desc)
    self.links[ca_guid] = (switch_guid, switch_port, ca_port, ca_desc)


def parse_ports(text):
  """ The Fabric in the output of `ibnetdiscover -p`.  Lines it doesn't know
  are skipped, as are ports that are down and routers. """
  fabric = Fabric()
  for line in text.splitlines():
    port = PORT.match(line)
    if not port:
      continue
    kind, number, hex16 = port.group(1), int(port.group(2)), port.group(3)
    peer = PEER.search(line)
    if peer is None:
      if kind == 'SW':
        down = DOWN.search(line)
        fabric.add_switch(guid(hex16), down.group(1) if down else '')
      continue
    peer_kind, peer_number, peer_hex, desc, peer_desc = peer.groups()
    peer_number = int(peer_number)
    if kind == 'SW' and peer_kind == 'CA':
      fabric.add_link(guid(hex16), desc, number, guid(peer_hex), peer_number, peer_desc)
    elif kind == 'CA' and peer_kind == 'SW':
      fabric.add_link(guid(peer_hex), peer_desc, peer_number, guid(hex16), number, desc)
    else:
      for k, g, d in ((kind, hex16, desc), (peer_kind, peer_hex, peer_desc)):
        if k == 'SW':
          fabric.add_switch(guid(g), d)
  return fabric


def switch_hostname(desc, switch_guid):
  """ The hostname to create a switch with: the name in a managed Mellanox
  switch's "MF0;<name>:<model>/U1", or a description that's a hostname
  already, else ibsw-<GUID>.  Unmanaged switches mostly share one
  description, such as "Mellanox Technologies Aggregation Node", and
  hostnames are unique. """
  match = re.match(r"^MF0;([^:]+):", desc or '')
  name = (match.group(1) if match else (desc or '')).strip().lower()
  if HOSTNAME.fullmatch(name):
    return name
  return 'ibsw-' + switch_guid[2:]


def ca_hostname(desc):
  """ The host in an HCA's node description, "<hostname> <device>" as
  rdma-ndd sets it, e.g. "node01 mlx5_0"; None if there's none. """
  words = (desc or '').split()
  if not words:
    return None
  name = words[0].lower()
  return name if HOSTNAME.fullmatch(name) else None


class OldMPCC(Exception):
  """ The mPCC has no GUID fields: it's older than 0.7.6. """


class Refused(Exception):
  """ The mPCC refused the token (401) or what it may do (403). """


class ib_discover(JobServerPlugin):
  jobModule = 'ib-discover'
  # seconds between runs
  interval = 3600
  # the API token of a staff user that may write switches and NICs, or a
  # file it's in.  Unset, the file ib-discover-token next to the config.
  token = ''
  tokenFile = ''
  ibnetdiscover = '/usr/sbin/ibnetdiscover'
  # the HCA and port to discover from (-C, -P); unset, ibnetdiscover's own
  hca = ''
  hcaPort = 0
  # seconds to let ibnetdiscover run
  timeout = 300

  def claimedModules(self):
    # it runs on a timer and claims no jobs
    return []

  def handle_jobs(self):
    now = time.monotonic()
    last = getattr(self.js, LAST_RUN, None)
    if last is not None and now - last < self.interval:
      return
    setattr(self.js, LAST_RUN, now)

    token = self.read_token()
    if not token:
      log.error("no API token, so no InfiniBand sync: set token or tokenFile for ib-discover.")
      self.failed = True
      return
    text = self.discover()
    if text is None:
      self.failed = True
      return
    fabric = parse_ports(text)
    self.api = self.token_session(token)
    try:
      self.sync(fabric)
    except OldMPCC:
      log.warning("mPCC older than 0.7.6, skipping InfiniBand sync.")
    except Refused as err:
      log.error(f"the mPCC refused the InfiniBand sync: {err}")
      self.failed = True

  def read_token(self):
    if self.token:
      return str(self.token).strip()
    path = self.tokenFile
    if not path and callable(getattr(self.js, 'beside_config', None)):
      path = self.js.beside_config('ib-discover-token')
    if not path or not os.path.isfile(path):
      return ''
    try:
      with open(path) as f:
        return f.read().strip()
    except OSError as err:
      log.error(f"unable to read the API token in {path}: {err}")
      return ''

  def discover(self):
    """ The output of ibnetdiscover -p, or None if it didn't run. """
    cmd = [self.ibnetdiscover, '-p']
    if self.hca:
      cmd += ['-C', str(self.hca)]
    if self.hcaPort:
      cmd += ['-P', str(self.hcaPort)]
    try:
      result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
    except FileNotFoundError:
      log.error(f"{self.ibnetdiscover} not found: install infiniband-diags, or set ibnetdiscover.")
      return None
    except subprocess.TimeoutExpired:
      log.error(f"ibnetdiscover took more than {self.timeout} seconds, stopped it.")
      return None
    if result.returncode != 0:
      detail = (result.stderr or '').strip().splitlines()
      log.error(f"ibnetdiscover failed (exit {result.returncode}): {detail[-1] if detail else 'no output'}")
      return None
    return result.stdout

  def token_session(self, token):
    """ A session that sends the token and no client certificate, otherwise
    like the job server's: its headers, CA and version check. """
    session = requests.Session()
    session.headers.update({k: v for k, v in self.js.session.headers.items() if k.lower() != 'authorization'})
    session.headers['Authorization'] = 'Token ' + token
    session.verify = getattr(self.js.session, 'verify', True)
    session.cert = None
    session.hooks = {'response': list(getattr(self.js.session, 'hooks', {}).get('response', []))}
    return session

  # --- the mPCC ---

  def url(self, path):
    return self.js.mprovURL + path

  def check(self, response, what):
    if response.status_code in (401, 403):
      try:
        detail = response.json().get('detail', '')
      except (ValueError, AttributeError):
        detail = ''
      raise Refused(f"{what}: HTTP {response.status_code} {detail}".strip())

  def get_list(self, path):
    """ The objects a list GET returns; [] for the mPCC's 404 with null. """
    response = self.api.get(self.url(path))
    self.check(response, f"GET {path}")
    if response.status_code == 404:
      return []
    if response.status_code == 400 and 'guid=' in path:
      try:
        body = response.json()
      except ValueError:
        body = None
      if isinstance(body, dict) and "doesn't exist" in str(body.get('guid', '')):
        raise OldMPCC()
    response.raise_for_status()
    body = response.json()
    return body if isinstance(body, list) else []

  def write(self, method, path, data):
    response = getattr(self.api, method)(self.url(path), data=json.dumps(data))
    self.check(response, f"{method.upper()} {path}")
    return response

  def sync(self, fabric):
    ib_types = {t.get('id') for t in self.get_list('networktypes/') if t.get('slug') == 'infiniband'}
    self.ib_networks = {n.get('id') for n in self.get_list('networks/') if n.get('net_type') in ib_types}
    self.ports = {}
    switch_ids = {}
    for switch_guid, desc in sorted(fabric.switches.items()):
      switch_ids[switch_guid] = self.ensure_switch(switch_guid, desc)
    updated = unmatched = 0
    for ca_guid, (switch_guid, switch_port, ca_port, ca_desc) in sorted(fabric.links.items()):
      switch_id = switch_ids.get(switch_guid)
      port_id = self.ensure_port(switch_id, switch_port) if switch_id is not None else None
      nic = self.find_nic(ca_guid, ca_port, ca_desc)
      if nic is None:
        unmatched += 1
        log.info(f"no network interface for HCA port {ca_guid} ({ca_desc!r}).")
        continue
      if self.update_nic(nic, ca_guid, port_id):
        updated += 1
    log.info(f"InfiniBand sync: {len(fabric.switches)} switches, {len(fabric.links)} HCA ports, "
             f"{updated} interfaces updated, {unmatched} not matched to a system.")

  def switch_by(self, key, value):
    # exact matches only: an mPCC filter is exact, but don't depend on it
    found = [s for s in self.get_list(f"switches/?{key}={quote(value)}") if (s.get(key) or '').lower() == value]
    return found[0] if found else None

  def ensure_switch(self, switch_guid, desc):
    """ The id of the switch with this GUID, created if there isn't one. """
    switch = self.switch_by('guid', switch_guid)
    if switch is not None:
      return switch['id']
    name = switch_hostname(desc, switch_guid)
    switch = self.switch_by('hostname', name)
    if switch is None:
      response = self.write('post', 'switches/', {'hostname': name, 'guid': switch_guid})
      if 199 < response.status_code < 300:
        log.info(f"added InfiniBand switch {name} ({switch_guid}).")
        return response.json().get('id')
      body = self.error_body(response)
      if any('required' in str(body.get(field, '')) for field in ('mgmt_ip', 'mgmt_mac')):
        raise OldMPCC()
      if 'hostname' not in body:
        log.error(f"unable to add InfiniBand switch {name} ({switch_guid}): HTTP {response.status_code} {body}")
        return None
      # made since we looked, e.g. by a node as it booted
      switch = self.switch_by('hostname', name)
      if switch is None:
        log.error(f"unable to add InfiniBand switch {name} ({switch_guid}): {body['hostname']}")
        return None
    if switch.get('guid'):
      log.warning(f"switch {name} has GUID {switch['guid']}, not {switch_guid}: not adding {switch_guid}; "
                  "rename one of them.")
      return None
    # a switch made by hand, or by a node, before its GUID was known
    response = self.write('patch', f"switches/{switch['id']}/", {'guid': switch_guid})
    if not 199 < response.status_code < 300:
      log.error(f"unable to set GUID {switch_guid} on switch {name}: HTTP {response.status_code} "
                f"{self.error_body(response)}")
    return switch['id']

  def ensure_port(self, switch_id, number):
    """ The id of port `number` of the switch, created if there isn't one. """
    if switch_id not in self.ports:
      self.ports[switch_id] = {str(p.get('name')): p.get('id') for p in self.get_list(f"switchports/?switch={switch_id}")
                               if p.get('switch') == switch_id}
    name = str(number)
    if name not in self.ports[switch_id]:
      response = self.write('post', 'switchports/', {'name': name, 'switch': switch_id})
      if not 199 < response.status_code < 300:
        log.error(f"unable to add port {name} of switch {switch_id}: HTTP {response.status_code} "
                  f"{self.error_body(response)}")
        return None
      self.ports[switch_id][name] = response.json().get('id')
    return self.ports[switch_id][name]

  def find_nic(self, ca_guid, ca_port, ca_desc):
    """ The network interface for an HCA port: the one with its GUID, else
    the IB interface without a GUID of the system its description names
    (on an InfiniBand network, or on none and named ib*), with ib<port-1>
    deciding between several.  None if there's no one interface. """
    nics = [n for n in self.get_list(f"networkinterfaces/?guid={quote(ca_guid)}") if (n.get('guid') or '').lower() == ca_guid]
    if nics:
      return nics[0]
    host = ca_hostname(ca_desc)
    if host is None:
      return None
    # ?hostname= finds systems whose hostname contains it
    systems = [s for s in self.get_list(f"systems/?hostname={quote(host)}") if (s.get('hostname') or '').lower() == host]
    if len(systems) != 1:
      return None
    candidates = [n for n in self.get_list(f"networkinterfaces/?system={systems[0]['id']}")
                  if not n.get('guid') and (n.get('network') in self.ib_networks
                                            or (n.get('network') is None and str(n.get('name', '')).startswith('ib')))]
    if len(candidates) > 1:
      candidates = [n for n in candidates if n.get('name') == f"ib{ca_port - 1}"]
    if len(candidates) != 1:
      log.info(f"{host} hasn't one InfiniBand interface without a GUID for HCA port {ca_guid}.")
      return None
    return candidates[0]

  def update_nic(self, nic, ca_guid, port_id):
    """ Fill in the interface's GUID and switch port where they're empty.
    True if it was changed. """
    data = {}
    if not nic.get('guid'):
      data['guid'] = ca_guid
    if port_id is not None:
      if nic.get('switch_port') is None:
        data['switch_port'] = port_id
      elif nic['switch_port'] != port_id:
        log.warning(f"interface {nic.get('name')} of {nic.get('hostname')} is set to switch port "
                    f"{nic['switch_port']}, but the fabric has HCA port {ca_guid} on port {port_id}: left as it is.")
    if not data:
      return False
    response = self.write('patch', f"networkinterfaces/{nic['id']}/", data)
    if not 199 < response.status_code < 300:
      log.error(f"unable to update interface {nic.get('name')} of {nic.get('hostname')}: "
                f"HTTP {response.status_code} {self.error_body(response)}")
      return False
    return True

  @staticmethod
  def error_body(response):
    try:
      body = response.json()
    except ValueError:
      return {}
    return body if isinstance(body, dict) else {}
