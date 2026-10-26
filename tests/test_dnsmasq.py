"""
The dnsmasq submodules write dnsmasq's config (dhcp.conf, dns.conf,
ipxe.conf) and one hosts file per network.  Each test checks the output by
content and, for anything dnsmasq parses as config, with ``dnsmasq --test``.
"""
import os
import pathlib

import pytest

from mprov_jobserver.plugins.dnsmasq_mod import config, dns
from mprov_jobserver.plugins.dnsmasq_mod.config import DnsmasqConfig
from mprov_jobserver.plugins.dnsmasq_mod.dhcp import DnsmasqDHCPConfig
from mprov_jobserver.plugins.dnsmasq_mod.dns import DnsmasqDNSConfig
from mprov_jobserver.plugins import dnsmasq as dnsmasq_plugin
from tools import run

NETWORK = {
  "id": 3, "slug": "cluster-net", "name": "Cluster Net", "domain": "cluster.local",
  "subnet": "10.0.0.0", "netmask": "24", "gateway": "10.0.0.1", "nameserver1": "10.0.0.2",
  "dhcpstart": "10.0.0.100", "dhcpend": "10.0.0.200",
}
NIC = {"hostname": "node01", "mac": "aa:bb:cc:00:00:01", "ipaddress": "10.0.0.11", "ipv6ll": "", "hostaliases": ""}
SWITCH = {"hostname": "switch01", "mgmt_mac": "aa:bb:cc:00:00:99", "mgmt_ip": "10.0.0.250"}


def dnsmasq_test(conf_file):
  """ Run dnsmasq's syntax check on a config file; return (ok, output). """
  return run(["/usr/sbin/dnsmasq", "--test", "-C", str(conf_file)])


def config_lines(path):
  return [line for line in path.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]


@pytest.fixture
def dirs(tmp_path):
  conf = tmp_path / "dnsmasq.d"
  conf.mkdir()
  return conf, tmp_path / "mprov", tmp_path / "tftproot"


@pytest.fixture
def no_interfaces(monkeypatch):
  # DNS adds the jobserver's own addresses on each network; give it none.
  monkeypatch.setattr(dns.netifaces, "interfaces", lambda: [])


def run_dhcp(js, dirs, enableTFTP=False):
  conf, mprov, tftproot = dirs
  plugin = DnsmasqDHCPConfig(js)
  plugin.dnsmasqConfDir = f"{conf}/"
  plugin.mprovDnsmasqDir = str(mprov)
  plugin.tftproot = str(tftproot)
  plugin.enableTFTP = enableTFTP
  plugin.handle_jobs()
  return conf / "dhcp.conf", mprov / "dhcp" / "cluster-net-dhcp.conf"


def test_dhcp_hosts_are_tagged_by_network_slug_and_switches_merged(js, dirs, tmp_path):
  js.session.routes.update({
    "networks/?isdhcp=True": [NETWORK],
    "networkinterfaces/?network=cluster-net": [NIC],
    "switches/?network=cluster-net": [SWITCH],
  })
  dhcp_conf, hosts = run_dhcp(js, dirs, enableTFTP=True)

  ok, out = dnsmasq_test(dhcp_conf)
  assert ok, out
  conf_text = dhcp_conf.read_text()
  assert "dhcp-range=set:cluster-net,10.0.0.100,10.0.0.200" in conf_text
  assert "dhcp-option=tag:cluster-net,option:router,10.0.0.1" in conf_text
  assert "enable-tftp" in conf_text

  # the per-network options match on the slug, so the hosts are tagged with it (#119)
  assert config_lines(hosts) == [
    "aa:bb:cc:00:00:01,set:cluster-net,10.0.0.11,node01",
    "aa:bb:cc:00:00:99,set:cluster-net,10.0.0.250,switch01",
  ]
  # dnsmasq reads each line of a hostsdir file as a dhcp-host option
  wrapped = tmp_path / "hosts-as-options.conf"
  wrapped.write_text("".join(f"dhcp-host={line}\n" for line in config_lines(hosts)))
  ok, out = dnsmasq_test(wrapped)
  assert ok, out


def test_dhcp_skips_hosts_without_a_mac_or_ip(js, dirs):
  js.session.routes.update({
    "networks/?isdhcp=True": [NETWORK],
    "networkinterfaces/?network=cluster-net": [
      dict(NIC, hostname="nomac", mac=""),
      dict(NIC, hostname="noip", ipaddress=None),
      dict(NIC, hostname=None),
    ],
    "switches/?network=cluster-net": [],
  })
  _, hosts = run_dhcp(js, dirs)
  # a host with no hostname still gets a reservation, without the name
  assert config_lines(hosts) == ["aa:bb:cc:00:00:01,set:cluster-net,10.0.0.11"]


def test_dhcp_handles_404_null_for_a_network(js, dirs):
  # The mPCC answers 404 with a null body when a filter matches nothing.
  js.session.routes.update({
    "networks/?isdhcp=True": [NETWORK],
    "networkinterfaces/?network=cluster-net": (404, None),
    "switches/?network=cluster-net": (404, None),
  })
  _, hosts = run_dhcp(js, dirs)
  assert config_lines(hosts) == []


@pytest.mark.parametrize("unset", [None, "", "missing"])
@pytest.mark.parametrize("field, option", [
  ("dhcpstart", "dhcp-range="),
  ("dhcpend", "dhcp-range="),
  ("gateway", "option:router"),
  ("nameserver1", "option:dns-server"),
  ("domain", "option:domain-name"),
])
def test_dhcp_leaves_out_options_for_unset_network_fields(js, dirs, field, option, unset):
  # the mPCC allows each of these to be null or blank (#178)
  network = dict(NETWORK)
  if unset == "missing":
    del network[field]
  else:
    network[field] = unset
  js.session.routes.update({
    "networks/?isdhcp=True": [network],
    "networkinterfaces/?network=cluster-net": [NIC],
    "switches/?network=cluster-net": [],
  })
  dhcp_conf, hosts = run_dhcp(js, dirs)

  ok, out = dnsmasq_test(dhcp_conf)
  assert ok, out
  lines = config_lines(dhcp_conf)
  assert not [line for line in lines if option in line]
  # the other options are still there
  expected = {
    "dhcp-range=": "dhcp-range=set:cluster-net,10.0.0.100,10.0.0.200",
    "option:router": "dhcp-option=tag:cluster-net,option:router,10.0.0.1",
    "option:dns-server": "dhcp-option=tag:cluster-net,option:dns-server,10.0.0.2",
  }
  for other, line in expected.items():
    if other != option:
      assert line in lines
  # the hosts file is still written
  assert config_lines(hosts) == ["aa:bb:cc:00:00:01,set:cluster-net,10.0.0.11,node01"]


def test_dhcp_renders_a_network_with_only_a_dhcp_range(js, dirs):
  network = dict(NETWORK, gateway=None, nameserver1=None, domain=None)
  js.session.routes.update({
    "networks/?isdhcp=True": [network],
    "networkinterfaces/?network=cluster-net": [],
    "switches/?network=cluster-net": [],
  })
  dhcp_conf, _ = run_dhcp(js, dirs)

  ok, out = dnsmasq_test(dhcp_conf)
  assert ok, out
  text = dhcp_conf.read_text()
  assert "# no router option: the gateway isn't set" in text
  assert "# no DNS server option: the nameserver isn't set" in text
  assert [line for line in config_lines(dhcp_conf) if "cluster-net" in line] == [
    "dhcp-range=set:cluster-net,10.0.0.100,10.0.0.200",
  ]


def run_dns(js, dirs):
  conf, mprov, _ = dirs
  plugin = DnsmasqDNSConfig(js)
  plugin.dnsmasqConfDir = f"{conf}/"
  plugin.mprovDnsmasqDir = str(mprov)
  plugin.handle_jobs()
  return conf / "dns.conf", mprov / "dns" / "cluster-net-dns.conf"


def test_dns_lists_nics_bmcs_and_switches(js, dirs, no_interfaces):
  js.session.routes.update({
    "networks/?managedns=True": [NETWORK],
    "networkinterfaces/?network=cluster-net": [dict(NIC, hostaliases="web db")],
    "systembmcs/?network=3&detail": [{"ipaddress": "10.0.0.51", "mac": "aa:bb:cc:00:01:01", "system": {"hostname": "node01-bmc"}}],
    "switches/?network=cluster-net": [SWITCH],
  })
  dns_conf, hosts = run_dns(js, dirs)

  ok, out = dnsmasq_test(dns_conf)
  assert ok, out
  assert [line.split() for line in config_lines(hosts)] == [
    ["10.0.0.11", "node01.cluster.local"],
    ["10.0.0.11", "web.cluster.local"],
    ["10.0.0.11", "db.cluster.local"],
    ["10.0.0.51", "node01-bmc.cluster.local"],
    ["10.0.0.250", "switch01.cluster.local"],
  ]


def test_dns_handles_404_null_for_nics_and_bmcs(js, dirs, no_interfaces):
  js.session.routes.update({
    "networks/?managedns=True": [NETWORK],
    "networkinterfaces/?network=cluster-net": (404, None),
    "systembmcs/?network=3&detail": (404, None),
    "switches/?network=cluster-net": [SWITCH],
  })
  _, hosts = run_dns(js, dirs)
  assert [line.split() for line in config_lines(hosts)] == [["10.0.0.250", "switch01.cluster.local"]]


def test_dns_handles_a_network_with_no_switches(js, dirs, no_interfaces):
  # A network with no switches gets the mPCC's 404 with null (#135).
  js.session.routes.update({
    "networks/?managedns=True": [NETWORK, dict(NETWORK, id=4, slug="other-net")],
    "networkinterfaces/?network=cluster-net": [NIC],
    "networkinterfaces/?network=other-net": [dict(NIC, hostname="node02", ipaddress="10.0.0.12")],
    "systembmcs/?network=3&detail": [],
    "systembmcs/?network=4&detail": [],
    "switches/?network=cluster-net": (404, None),
    "switches/?network=other-net": (404, None),
  })
  _, hosts = run_dns(js, dirs)
  assert [line.split() for line in config_lines(hosts)] == [["10.0.0.11", "node01.cluster.local"]]
  # and the networks after it are still written
  other = hosts.parent / "other-net-dns.conf"
  assert [line.split() for line in config_lines(other)] == [["10.0.0.12", "node02.cluster.local"]]


@pytest.fixture
def conf_plugin(js, dirs, monkeypatch):
  # Don't look up our own AAAA record.
  def no_aaaa(*args, **kwargs):
    raise config.dns.resolver.NXDOMAIN()
  monkeypatch.setattr(config.dns.resolver, "resolve", no_aaaa)
  conf, mprov, tftproot = dirs
  plugin = DnsmasqConfig(js)
  plugin.dnsmasqConfDir = str(conf)
  plugin.mprovDnsmasqDir = str(mprov)
  plugin.tftproot = str(tftproot)
  return plugin


def test_config_writes_ipxe_conf_and_menus_and_starts_dnsmasq(js, dirs, conf_plugin, monkeypatch):
  started = []
  monkeypatch.setattr(config.subprocess, "run", lambda cmd, **kw: started.append(cmd))
  conf_plugin.handle_jobs()

  conf, _, tftproot = dirs
  ok, out = dnsmasq_test(conf / "ipxe.conf")
  assert ok, out
  assert "dhcp-boot=tag:!ipxe,undionly.kpxe" in (conf / "ipxe.conf").read_text()
  assert (tftproot / "ipversionrouter.ipxe").exists()
  assert (tftproot / "menu.ipxe").exists()
  # no dnsmasq was running, so it starts one
  assert started == [["/usr/sbin/dnsmasq", "--log-facility=-"]]
  # the running DNS, DHCP and PXE jobs are marked done
  _, status, jobquery = js.job_updates[-1]
  assert status == 4
  assert '"dns-update"' in jobquery and '"dhcp-update"' in jobquery and jobquery.endswith("&status=2")


def test_all_generated_config_passes_dnsmasq_test_together(js, dirs, conf_plugin, no_interfaces, monkeypatch, tmp_path):
  # dnsmasq loads all of these at once, so check them as one config.
  monkeypatch.setattr(config.subprocess, "run", lambda cmd, **kw: None)
  js.session.routes.update({
    "networks/?isdhcp=True": [NETWORK],
    "networks/?managedns=True": [NETWORK],
    "networkinterfaces/?network=cluster-net": [NIC],
    "systembmcs/?network=3&detail": [],
    "switches/?network=cluster-net": [SWITCH],
  })
  run_dhcp(js, dirs, enableTFTP=True)
  run_dns(js, dirs)
  conf_plugin.handle_jobs()

  conf, _, _ = dirs
  combined = tmp_path / "combined.conf"
  combined.write_text("".join(f"conf-file={conf / name}\n" for name in ("dhcp.conf", "dns.conf", "ipxe.conf")))
  ok, out = dnsmasq_test(combined)
  assert ok, out


class FakeSh:
  """ Stands in for the sh module in build_ipxe(): records the commands and
  runs ``on`` hooks instead of git and make. """
  def __init__(self, on=None):
    self.calls = []
    self.on = on or {}
  def __getattr__(self, name):
    def command(args, **kwargs):
      self.calls.append((name, args))
      if name in self.on:
        self.on[name](args, kwargs)
    return command


def ipxe_plugin(js, tftproot):
  plugin = dnsmasq_plugin.dnsmasq(js)
  plugin.tftproot = str(tftproot)
  return plugin


def fake_make(args, kwargs):
  # "build" each target named on the command line
  for target in args[1:]:
    built = f"{kwargs['_cwd']}/{target}"
    os.makedirs(os.path.dirname(built), exist_ok=True)
    with open(built, "w") as f:
      f.write(target)


def fake_clone(args, kwargs):
  # pathlib, not os.makedirs, which a test breaks on purpose
  if args[0] == "clone":
    pathlib.Path(kwargs["_cwd"], "ipxe", "src").mkdir(parents=True)


def test_ipxe_build_installs_nothing_at_runtime(js, tmp_path, monkeypatch):
  # the tools come with the image; a runtime dnf raced the image builds (#207)
  fake = FakeSh(on={"git": fake_clone, "make": fake_make})
  monkeypatch.setattr(dnsmasq_plugin, "sh", fake)
  tftproot = tmp_path / "tftproot"
  tftproot.mkdir()
  ipxe_plugin(js, tftproot).build_ipxe()

  assert [name for name, _ in fake.calls] == ["git", "git", "make", "make"]
  assert sorted(p.name for p in tftproot.iterdir()) == ["snponly.efi", "snponly_ipv4.efi", "undionly.kpxe"]


def test_ipxe_build_failure_doesnt_end_the_plugin(js, tmp_path, monkeypatch, capsys):
  # an unexpected error is logged; the build is retried on the next start (#207)
  def broken(args, kwargs):
    raise OSError("disk full")
  monkeypatch.setattr(dnsmasq_plugin, "sh", FakeSh(on={"git": fake_clone}))
  monkeypatch.setattr(dnsmasq_plugin.os, "makedirs", lambda *a, **kw: broken(a, kw))
  tftproot = tmp_path / "tftproot"
  tftproot.mkdir()
  ipxe_plugin(js, tftproot).build_ipxe()

  assert "Error: unable to build iPXE: disk full" in capsys.readouterr().out
  assert list(tftproot.iterdir()) == []
