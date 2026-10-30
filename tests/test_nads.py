"""
nads (Node Auto Detection System) runs on an unknown machine booted into the
N.A.D.S. image: it listens for an LLDP frame on each interface that's up,
and registers the switch, port and MAC it finds with the mPCC.  The raw
socket, the interfaces and dmidecode are faked.
"""
import socket as socket_module
import struct
import types

import pytest

from mprov_jobserver.plugins import nads as nads_module
from mprov_jobserver.plugins.nads import nads, unpack_lldp_frame, LLDP_PROTO_ID

MAC = "aa:bb:cc:00:00:01"


def tlv(tlv_type, payload):
  return struct.pack("!H", (tlv_type << 9) | len(payload)) + payload


def lldp_frame(switch=b"leaf01", port=b"\x05Ethernet1/5"):
  # Port ID TLVs start with a subtype byte (5 = interface name).
  # port=None leaves the Port ID TLV out.
  payload = (tlv(1, b"\x04" + bytes(6)) + (tlv(2, port) if port is not None else b"") + tlv(3, b"\x00\x78")
             + tlv(5, switch) + tlv(0, b""))
  return bytes.fromhex("0180c200000e") + bytes.fromhex("020000000001") + struct.pack("!H", LLDP_PROTO_ID) + payload


def other_frame():
  return bytes(12) + struct.pack("!H", 0x0800) + b"ip packet"


class FakeSocket:
  def __init__(self, frames):
    self.frames = list(frames)

  def settimeout(self, t):
    pass

  def recvfrom(self, size):
    if not self.frames:
      raise socket_module.timeout()
    return (self.frames.pop(0), None)


@pytest.fixture
def frames(monkeypatch):
  frames = []
  monkeypatch.setattr(nads_module, "socket", lambda *args: FakeSocket(frames))
  monkeypatch.setattr(nads_module, "promiscuous_mode", lambda *args: None)
  return frames


@pytest.fixture
def host(monkeypatch, frames):
  """ One interface (eth0) that's up, besides lo. """
  monkeypatch.setattr(nads_module, "glob", lambda pattern: [])
  ifaces = {"lo": "UNKNOWN", "eth0": "UP", "eth1": "DOWN"}
  ipdb = types.SimpleNamespace(
    interfaces={name: types.SimpleNamespace(operstate=state) for name, state in ifaces.items()},
    release=lambda: None)
  monkeypatch.setattr(nads_module, "IPDB", lambda: ipdb)
  monkeypatch.setattr(nads_module.netifaces, "interfaces", lambda: list(ifaces))
  monkeypatch.setattr(nads_module.netifaces, "ifaddresses",
                      lambda iface: {nads_module.AF_PACKET: [{"addr": MAC}]})
  monkeypatch.setattr(nads_module, "DMIDecode",
                      lambda: types.SimpleNamespace(manufacturer=lambda: "Dell Inc.", model=lambda: "R650"))
  return ifaces


@pytest.fixture
def plugin(js):
  js.runonce = True
  plugin = nads(js)
  plugin.maxLLDPWait = 0.2
  return plugin


def register_writes(js):
  return [data for method, path, data in js.session.writes if path.endswith("systems/register")]


def test_unpack_lldp_frame():
  eth_payload = lldp_frame()[14:]
  tlvs = [(t[1], t[5]) for t in unpack_lldp_frame(eth_payload)]
  # stops at the end-of-LLDPDU TLV
  assert [t for t, _ in tlvs] == [1, 2, 3, 5]
  assert dict(tlvs)[2] == b"\x05Ethernet1/5"
  assert dict(tlvs)[5] == b"leaf01"


def test_get_lldp_finds_switch_and_port(plugin, frames):
  frames.extend([other_frame(), lldp_frame()])
  plugin.provIntf = "eth0"
  assert plugin.getLLDP()
  assert plugin.switch == "leaf01"
  assert plugin.port.endswith("Ethernet1/5")


def test_registers_switch_port_and_mac(js, plugin, host, frames):
  frames.append(lldp_frame())
  assert plugin.handle_jobs() is True
  # the URL has a double slash (#115)
  assert register_writes(js) == [
    {"switch": "leaf01", "port": "5", "vendor": "Dell Inc.", "model": "R650", "mac": MAC},
  ]
  assert plugin.provIntf == "eth0"


def test_reboots_after_registering_when_set_to(js, plugin, host, frames, commands):
  plugin.reboot = True
  frames.append(lldp_frame())
  plugin.handle_jobs()
  assert commands.run == ["/sbin/reboot"]


def test_failed_registration_doesnt_reboot(js, plugin, host, frames, commands):
  plugin.reboot = True
  frames.append(lldp_frame())
  js.session.write_routes[("POST", "/systems/register")] = (400, {})
  assert plugin.handle_jobs() is False
  assert commands.run == []


def test_refuses_to_run_outside_runonce(js, plugin, host):
  js.runonce = False
  assert plugin.handle_jobs() is False
  assert js.session.writes == []


def test_no_lldp_doesnt_register(js, plugin, host, frames):
  # getLLDP() said True without a switch and port, so nads registered None (#149)
  assert plugin.handle_jobs() is False
  assert register_writes(js) == []


def test_port_without_a_slash(js, plugin, host, frames):
  # the subtype byte was only dropped along with everything before a '/' (#149)
  frames.append(lldp_frame(port=b"\x05swp12"))
  plugin.handle_jobs()
  assert register_writes(js)[0]["port"] == "swp12"


def test_port_given_as_a_mac_address(js, plugin, host, frames):
  frames.append(lldp_frame(port=b"\x03" + bytes.fromhex("0c42a1b2c3d4")))
  plugin.handle_jobs()
  assert register_writes(js)[0]["port"] == "0c:42:a1:b2:c3:d4"


def test_switch_without_a_port_doesnt_register(js, plugin, host, frames):
  frames.append(lldp_frame(port=None))
  assert plugin.handle_jobs() is False
  assert register_writes(js) == []
