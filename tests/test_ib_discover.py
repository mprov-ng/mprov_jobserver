"""
The ib-discover plugin (#15): parsing `ibnetdiscover -p`, and recording the
fabric in the mPCC through its generic endpoints.

The fabric output here is made up, in the format dump_ports_report() in
rdma-core's infiniband-diags/ibnetdiscover.c prints; it isn't from a real
fabric.
"""
import itertools
import json
import subprocess

import pytest

from conftest import FakeResponse
from mprov_jobserver.plugins import ib_discover as ibd

SW1 = "0x0002c903004e6fd0"
SW2 = "0x0002c903004e7000"
SW3 = "0x7cfe900300a1b2c0"
NODE01 = "0x0002c903000f1235"
NODE02 = "0x0002c903000f2235"
GHOST = "0x0002c903000f9995"

PORTS = f"""\
SW     2  1 {SW1} 4X FDR - CA     3  1 {NODE01} ( 'MF0;ibsw01:SX6036/U1' - 'node01 mlx4_0' )
SW     2  2 {SW1} 4X FDR - CA     4  1 {NODE02} ( 'MF0;ibsw01:SX6036/U1' - 'node02 mlx4_0' )
SW     2  3 {SW1} 4X FDR - SW     5 36 {SW2} ( 'MF0;ibsw01:SX6036/U1' - 'Mellanox Technologies Aggregation Node' )
SW     2  4 {SW1} 4X SDR                                    'MF0;ibsw01:SX6036/U1'
SW     5  1 {SW2} 4X FDR - CA     6  1 {GHOST} ( 'Mellanox Technologies Aggregation Node' - 'MT4099 ConnectX3 Mellanox Technologies' )
SW     5 36 {SW2} 4X FDR - SW     2  3 {SW1} ( 'Mellanox Technologies Aggregation Node' - 'MF0;ibsw01:SX6036/U1' )
SW     7  1 {SW3} 4X EDR                                    'spare-leaf'
CA     3  1 {NODE01} 4X FDR - SW     2  1 {SW1} ( 'node01 mlx4_0' - 'MF0;ibsw01:SX6036/U1' )
CA     4  1 {NODE02} 4X FDR - SW     2  2 {SW1} ( 'node02 mlx4_0' - 'MF0;ibsw01:SX6036/U1' )
CA     6  1 {GHOST} 4X FDR - SW     5  1 {SW2} ( 'MT4099 ConnectX3 Mellanox Technologies' - 'Mellanox Technologies Aggregation Node' )
"""


def test_parse_ports_finds_switches_and_hca_links():
  fabric = ibd.parse_ports(PORTS)
  assert fabric.switches == {
    SW1: "MF0;ibsw01:SX6036/U1",
    SW2: "Mellanox Technologies Aggregation Node",
    SW3: "spare-leaf",
  }
  assert fabric.links == {
    NODE01: (SW1, 1, 1, "node01 mlx4_0"),
    NODE02: (SW1, 2, 1, "node02 mlx4_0"),
    GHOST: (SW2, 1, 1, "MT4099 ConnectX3 Mellanox Technologies"),
  }


def test_parse_ports_skips_what_it_doesnt_know():
  assert ibd.parse_ports("# a comment\n\nibwarn: something\n").switches == {}


@pytest.mark.parametrize("desc, expected", [
  ("MF0;IBSW01:SX6036/U1", "ibsw01"),
  ("leaf-3.ib.example", "leaf-3.ib.example"),
  ("Mellanox Technologies Aggregation Node", "ibsw-" + SW2[2:]),
  ("", "ibsw-" + SW2[2:]),
])
def test_switch_hostname(desc, expected):
  assert ibd.switch_hostname(desc, SW2) == expected


@pytest.mark.parametrize("desc, expected", [
  ("node01 mlx5_0", "node01"),
  ("NODE01 mlx5_0", "node01"),
  ("", None),
  ("node_01 mlx5_0", None),
])
def test_ca_hostname(desc, expected):
  assert ibd.ca_hostname(desc) == expected


# --- the sync against a fake mPCC ---

IB_TYPE = {"id": 2, "slug": "infiniband", "name": "Infiniband"}
ETH_TYPE = {"id": 1, "slug": "ethernet", "name": "Ethernet"}
IB_NET = {"id": 4, "slug": "ib-net", "net_type": 2}
ETH_NET = {"id": 1, "slug": "cluster", "net_type": 1}


def nic(id, system, name, network, guid=None, switch_port=None, hostname=None):
  return {"id": id, "system": system, "name": name, "network": network, "guid": guid,
          "switch_port": switch_port, "hostname": hostname, "mac": None}


@pytest.fixture
def mpcc(js):
  """ An mPCC that knows the two nodes and nothing about the fabric. """
  s = js.session
  s.routes.update({
    "networktypes/": [ETH_TYPE, IB_TYPE],
    "networks/": [ETH_NET, IB_NET],
    f"switches/?guid={SW1}": (404, None),
    f"switches/?guid={SW2}": (404, None),
    f"switches/?guid={SW3}": (404, None),
    "switches/?hostname=ibsw01": (404, None),
    f"switches/?hostname=ibsw-{SW2[2:]}": (404, None),
    "switches/?hostname=spare-leaf": (404, None),
    "switchports/?switch=11": (404, None),
    "switchports/?switch=12": (404, None),
    f"networkinterfaces/?guid={NODE01}": (404, None),
    f"networkinterfaces/?guid={NODE02}": (404, None),
    f"networkinterfaces/?guid={GHOST}": (404, None),
    # ?hostname= is a "contains" search on systems
    "systems/?hostname=node01": [{"id": 1, "hostname": "node01"}, {"id": 9, "hostname": "node010"}],
    "systems/?hostname=node02": [{"id": 2, "hostname": "node02"}],
    "systems/?hostname=mt4099": (404, None),
    "networkinterfaces/?system=1": [nic(101, 1, "eth0", 1, hostname="node01"), nic(102, 1, "ib0", 4, hostname="node01")],
    "networkinterfaces/?system=2": [nic(201, 2, "eth0", 1, hostname="node02"), nic(202, 2, "ib0", None, hostname="node02")],
  })
  switch_ids = itertools.count(11)
  port_ids = itertools.count(501)
  real_write = s._write

  def write(method, url, data=None, **kwargs):
    path = url[len(js.mprovURL):]
    if method == "POST" and path == "switches/":
      s.write_routes[(method, path)] = (201, {"id": next(switch_ids)})
    elif method == "POST" and path == "switchports/":
      s.write_routes[(method, path)] = (201, {"id": next(port_ids)})
    return real_write(method, url, data, **kwargs)
  s._write = write
  return s


def plugin_for(js, monkeypatch, output=PORTS, returncode=0):
  calls = []

  def run(cmd, **kwargs):
    calls.append(cmd)
    return subprocess.CompletedProcess(cmd, returncode, stdout=output, stderr="ibwarn: no fabric\n")
  monkeypatch.setattr(ibd.subprocess, "run", run)
  plugin = ibd.ib_discover(js)
  plugin.token = "abc.def"
  monkeypatch.setattr(plugin, "token_session", lambda token: js.session)
  plugin.calls = calls
  return plugin


def test_sync_adds_switches_ports_and_fills_in_the_nics(js, mpcc, monkeypatch, capsys):
  plugin = plugin_for(js, monkeypatch)
  plugin.handle_jobs()

  assert plugin.calls == [["/usr/sbin/ibnetdiscover", "-p"]]
  posts = [(path, data) for method, path, data in mpcc.writes if method == "POST"]
  assert posts == [
    ("switches/", {"hostname": "ibsw01", "guid": SW1}),
    ("switches/", {"hostname": "ibsw-" + SW2[2:], "guid": SW2}),
    ("switches/", {"hostname": "spare-leaf", "guid": SW3}),
    ("switchports/", {"name": "1", "switch": 11}),
    ("switchports/", {"name": "2", "switch": 11}),
    ("switchports/", {"name": "1", "switch": 12}),
  ]
  # node01's ib0 is on the InfiniBand network; node02's has no network but
  # is named ib*; neither eth0 is touched.  The HCA with no hostname in its
  # description is left unmatched.
  assert mpcc.patches == [
    ("networkinterfaces/102/", {"guid": NODE01, "switch_port": 501}),
    ("networkinterfaces/202/", {"guid": NODE02, "switch_port": 502}),
  ]
  out = capsys.readouterr().out
  assert "3 switches, 3 HCA ports, 2 interfaces updated, 1 not matched" in out
  assert not plugin.failed


def test_a_fabric_already_recorded_writes_nothing(js, mpcc, monkeypatch):
  mpcc.routes.update({
    f"switches/?guid={SW1}": [{"id": 11, "hostname": "ibsw01", "guid": SW1}],
    f"switches/?guid={SW2}": [{"id": 12, "hostname": "spine", "guid": SW2}],
    f"switches/?guid={SW3}": [{"id": 13, "hostname": "spare", "guid": SW3}],
    "switchports/?switch=11": [{"id": 501, "name": "1", "switch": 11}, {"id": 502, "name": "2", "switch": 11}],
    "switchports/?switch=12": [{"id": 601, "name": "1", "switch": 12}],
    f"networkinterfaces/?guid={NODE01}": [nic(102, 1, "ib0", 4, guid=NODE01, switch_port=501)],
    f"networkinterfaces/?guid={NODE02}": [nic(202, 2, "ib0", None, guid=NODE02, switch_port=502)],
  })
  plugin_for(js, monkeypatch).handle_jobs()
  assert mpcc.writes == []


def test_an_admin_set_switch_port_is_left_alone(js, mpcc, monkeypatch, capsys):
  mpcc.routes.update({
    f"networkinterfaces/?guid={NODE01}": [nic(102, 1, "ib0", 4, guid=NODE01, switch_port=777, hostname="node01")],
  })
  plugin_for(js, monkeypatch).handle_jobs()
  assert ("networkinterfaces/102/", {"guid": NODE01, "switch_port": 501}) not in mpcc.patches
  assert not any(path == "networkinterfaces/102/" for path, _ in mpcc.patches)
  assert "is set to switch port 777" in capsys.readouterr().out


def test_a_switch_made_by_hand_gets_its_guid(js, mpcc, monkeypatch):
  mpcc.routes["switches/?hostname=ibsw01"] = [{"id": 11, "hostname": "ibsw01", "guid": None}]
  plugin_for(js, monkeypatch).handle_jobs()
  assert ("switches/11/", {"guid": SW1}) in mpcc.patches
  assert ("switches/", {"hostname": "ibsw01", "guid": SW1}) not in [(p, d) for m, p, d in mpcc.writes if m == "POST"]


def test_a_hostname_taken_by_another_guid_isnt_used(js, mpcc, monkeypatch, capsys):
  other = "0x0002c903004eaaaa"
  mpcc.routes["switches/?hostname=ibsw01"] = [{"id": 30, "hostname": "ibsw01", "guid": other}]
  plugin_for(js, monkeypatch).handle_jobs()
  assert not any(path == "switches/30/" for path, _ in mpcc.patches)
  # its ports aren't added, but the nodes on it still get their GUIDs
  assert ("networkinterfaces/102/", {"guid": NODE01}) in mpcc.patches
  assert f"switch ibsw01 has GUID {other}" in capsys.readouterr().out


def test_a_switch_added_meanwhile_is_found_again(js, mpcc, monkeypatch):
  # a node adds ibsw01 between our GET and our POST, so the POST gets a 400
  mpcc.routes["switchports/?switch=40"] = (404, None)
  answers = iter([(404, None), [{"id": 40, "hostname": "ibsw01", "guid": None}]])
  real_get = mpcc.get

  def get(url, **kwargs):
    if url.endswith("switches/?hostname=ibsw01"):
      mpcc.routes["switches/?hostname=ibsw01"] = next(answers)
    return real_get(url, **kwargs)
  mpcc.get = get
  real_write = mpcc._write

  def write(method, url, data=None, **kwargs):
    if method == "POST" and url.endswith("switches/") and json.loads(data)["hostname"] == "ibsw01":
      mpcc.writes.append((method, "switches/", json.loads(data)))
      return FakeResponse(400, {"hostname": ["Switch with this Host Name already exists."]})
    return real_write(method, url, data, **kwargs)
  mpcc._write = write
  plugin_for(js, monkeypatch).handle_jobs()
  assert ("switches/40/", {"guid": SW1}) in mpcc.patches
  assert ("switchports/", {"name": "1", "switch": 40}) in [(p, d) for m, p, d in mpcc.writes if m == "POST"]


def test_an_older_mpcc_is_skipped(js, mpcc, monkeypatch, capsys):
  mpcc.routes[f"switches/?guid={SW1}"] = (400, {"guid": "Error searching for guid: Field doesn't exist."})
  plugin = plugin_for(js, monkeypatch)
  plugin.handle_jobs()
  assert mpcc.writes == []
  assert "mPCC older than 0.7.6, skipping InfiniBand sync." in capsys.readouterr().out
  assert not plugin.failed


def test_an_older_mpcc_found_by_its_required_mgmt_fields_is_skipped(js, mpcc, monkeypatch, capsys):
  real_write = mpcc._write

  def write(method, url, data=None, **kwargs):
    if method == "POST" and url.endswith("switches/"):
      mpcc.writes.append((method, "switches/", None))
      return FakeResponse(400, {"mgmt_ip": ["This field is required."], "mgmt_mac": ["This field is required."]})
    return real_write(method, url, data, **kwargs)
  mpcc._write = write
  plugin_for(js, monkeypatch).handle_jobs()
  assert mpcc.patches == []
  assert "mPCC older than 0.7.6" in capsys.readouterr().out


def test_a_refused_token_stops_the_run(js, mpcc, monkeypatch, capsys):
  mpcc.routes["networktypes/"] = (403, {"detail": "ib needs the permission networks.view_networktype."})
  plugin = plugin_for(js, monkeypatch)
  plugin.handle_jobs()
  assert mpcc.writes == []
  assert plugin.failed
  assert "networks.view_networktype" in capsys.readouterr().out


def test_it_runs_once_per_interval(js, mpcc, monkeypatch):
  now = [1000.0]
  monkeypatch.setattr(ibd.time, "monotonic", lambda: now[0])
  first = plugin_for(js, monkeypatch, output="")
  first.handle_jobs()
  assert len(first.calls) == 1
  # the job server makes a new plugin on each heartbeat
  now[0] += 10
  second = plugin_for(js, monkeypatch, output="")
  second.handle_jobs()
  assert second.calls == []
  now[0] += 3600
  third = plugin_for(js, monkeypatch, output="")
  third.handle_jobs()
  assert len(third.calls) == 1


def test_no_token_no_run(js, monkeypatch, tmp_path, capsys):
  plugin = plugin_for(js, monkeypatch)
  plugin.token = ""
  plugin.tokenFile = str(tmp_path / "missing")
  plugin.handle_jobs()
  assert plugin.calls == [] and plugin.failed
  assert "no API token" in capsys.readouterr().out


def test_the_token_can_come_from_a_file(js, monkeypatch, tmp_path):
  path = tmp_path / "ib-discover-token"
  path.write_text("abc.def\n")
  plugin = ibd.ib_discover(js)
  plugin.tokenFile = str(path)
  assert plugin.read_token() == "abc.def"


def test_hca_and_port_are_passed_on(js, mpcc, monkeypatch):
  plugin = plugin_for(js, monkeypatch, output="")
  plugin.hca, plugin.hcaPort = "mlx5_0", 1
  plugin.handle_jobs()
  assert plugin.calls == [["/usr/sbin/ibnetdiscover", "-p", "-C", "mlx5_0", "-P", "1"]]


def test_ibnetdiscover_failing_is_reported(js, monkeypatch, capsys):
  plugin = plugin_for(js, monkeypatch, returncode=1)
  plugin.handle_jobs()
  assert plugin.failed
  assert "ibnetdiscover failed (exit 1): ibwarn: no fabric" in capsys.readouterr().out
  assert js.session.writes == []


def test_ibnetdiscover_missing_is_reported(js, monkeypatch, capsys):
  plugin = plugin_for(js, monkeypatch)

  def missing(cmd, **kwargs):
    raise FileNotFoundError(cmd[0])
  monkeypatch.setattr(ibd.subprocess, "run", missing)
  plugin.handle_jobs()
  assert plugin.failed
  assert "install infiniband-diags" in capsys.readouterr().out


def test_the_token_session_has_no_client_certificate(js):
  js.session.headers = {"Content-Type": "application/json", "Authorization": "Api-Key old", "mProv-Version": "0.7"}
  js.session.verify = "/var/lib/mprov/tls/ca.pem"
  js.session.cert = "/var/lib/mprov/tls/credential.pem"
  session = ibd.ib_discover(js).token_session("abc.def")
  assert session.cert is None
  assert session.verify == "/var/lib/mprov/tls/ca.pem"
  assert session.headers["Authorization"] == "Token abc.def"
  assert session.headers["mProv-Version"] == "0.7"
  assert session.headers["Content-Type"] == "application/json"


def test_it_claims_no_jobs(js):
  assert ibd.ib_discover(js).claimedModules() == []
