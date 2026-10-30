"""
The dnsmasq submodules write dnsmasq's config (dhcp.conf, dns.conf,
ipxe.conf) and one hosts file per network.  Each test checks the output by
content and, for anything dnsmasq parses as config, with ``dnsmasq --test``.
"""
import os
import pathlib
import signal
import subprocess
import sys
import threading

import pytest

from mprov_jobserver.plugins.dnsmasq_mod import config, dns, service
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
  plugin.dnsmasqPidFile = str(tftproot.parent / "dnsmasq.pid")
  return plugin


def starts(monkeypatch, returncode=0):
  """ Record the commands config runs to start dnsmasq. """
  started = []
  def run(cmd, **kw):
    started.append(cmd)
    return subprocess.CompletedProcess(cmd, returncode)
  monkeypatch.setattr(config.subprocess, "run", run)
  return started


def test_config_writes_ipxe_conf_and_menus_and_starts_dnsmasq(js, dirs, conf_plugin, monkeypatch):
  started = starts(monkeypatch)
  conf_plugin.dnsOk = conf_plugin.dhcpOk = True
  conf_plugin.handle_jobs()

  conf, _, tftproot = dirs
  ok, out = dnsmasq_test(conf / "ipxe.conf")
  assert ok, out
  assert "dhcp-boot=tag:!ipxe,undionly.kpxe" in (conf / "ipxe.conf").read_text()
  assert (tftproot / "ipversionrouter.ipxe").exists()
  assert (tftproot / "menu.ipxe").exists()
  # no dnsmasq was running, so it starts one, with the pid file we find it by
  assert started == [["/usr/sbin/dnsmasq", "--log-facility=-", f"--pid-file={conf_plugin.dnsmasqPidFile}"]]
  # the running DNS, DHCP and PXE jobs are marked done
  [(_, dns_status, dns_query), (_, dhcp_status, dhcp_query)] = js.job_updates
  assert (dns_status, dhcp_status) == (4, 4)
  assert '"dns-update"' in dns_query and '"dhcp-update"' in dhcp_query and dhcp_query.endswith("&status=2")


def test_all_generated_config_passes_dnsmasq_test_together(js, dirs, conf_plugin, no_interfaces, monkeypatch, tmp_path):
  # dnsmasq loads all of these at once, so check them as one config.
  starts(monkeypatch)
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


def test_ipxe_build_installs_nothing_at_runtime(js, tmp_path, monkeypatch, capsys):
  # the tools come with the image; a runtime dnf raced the image builds (#207)
  fake = FakeSh(on={"git": fake_clone, "make": fake_make})
  monkeypatch.setattr(dnsmasq_plugin, "sh", fake)
  tftproot = tmp_path / "tftproot"
  tftproot.mkdir()
  ipxe_plugin(js, tftproot).build_ipxe()

  assert [name for name, _ in fake.calls] == ["git", "git", "make", "make"]
  assert sorted(p.name for p in tftproot.iterdir()) == ["snponly.efi", "snponly_ipv4.efi", "undionly.kpxe"]
  # a build that worked doesn't point at errors (#211)
  out = capsys.readouterr().out
  assert "[I] dnsmasq: iPXE build finished." in out
  assert "[E]" not in out


def test_ipxe_build_failure_doesnt_end_the_plugin(js, tmp_path, monkeypatch, capsys):
  # an unexpected error is logged; the build is retried on the next start (#207)
  def broken(args, kwargs):
    raise OSError("disk full")
  monkeypatch.setattr(dnsmasq_plugin, "sh", FakeSh(on={"git": fake_clone}))
  monkeypatch.setattr(dnsmasq_plugin.os, "makedirs", lambda *a, **kw: broken(a, kw))
  tftproot = tmp_path / "tftproot"
  tftproot.mkdir()
  ipxe_plugin(js, tftproot).build_ipxe()

  assert "[E] dnsmasq: unable to build iPXE: disk full" in capsys.readouterr().out
  assert list(tftproot.iterdir()) == []


# Restart only for the main config, SIGHUP for hosts, nothing for no change
# (#108); the files are swapped in whole (#210); our dnsmasq is found by its
# pid file (#181); the jobs get each worker's outcome (#179).

def test_write_if_changed_swaps_the_file_in_whole(tmp_path):
  path = tmp_path / "a-dns.conf"
  assert service.write_if_changed(str(path), "10.0.0.1 host1\n") is True
  inode = path.stat().st_ino
  # the same text again: not rewritten, so nothing for dnsmasq to reload
  assert service.write_if_changed(str(path), "10.0.0.1 host1\n") is False
  assert path.stat().st_ino == inode
  # new text: a new file renamed over the old one, never truncated in place
  assert service.write_if_changed(str(path), "10.0.0.2 host1\n") is True
  assert path.read_text() == "10.0.0.2 host1\n"
  assert path.stat().st_ino != inode
  # the temp file was a dotfile (dnsmasq's watch skips those) and is gone
  assert [p.name for p in tmp_path.iterdir()] == ["a-dns.conf"]


def test_workers_report_what_changed(js, dirs, no_interfaces):
  js.session.routes.update({
    "networks/?managedns=True": [NETWORK],
    "networkinterfaces/?network=cluster-net": [NIC],
    "systembmcs/?network=3&detail": [],
    "switches/?network=cluster-net": [],
  })
  conf, mprov, _ = dirs

  def dns_run():
    plugin = DnsmasqDNSConfig(js)
    plugin.dnsmasqConfDir = f"{conf}/"
    plugin.mprovDnsmasqDir = str(mprov)
    plugin.handle_jobs()
    return plugin.mainChanged, plugin.hostsChanged, plugin.completed

  assert dns_run() == (True, True, True)
  # nothing new from the mPCC: nothing changed
  assert dns_run() == (False, False, True)
  # a node got a new address: only its hosts file changed
  js.session.routes["networkinterfaces/?network=cluster-net"] = [dict(NIC, ipaddress="10.0.0.12")]
  assert dns_run() == (False, True, True)


def test_dhcp_worker_reports_what_changed(js, dirs):
  js.session.routes.update({
    "networks/?isdhcp=True": [NETWORK],
    "networkinterfaces/?network=cluster-net": [NIC],
    "switches/?network=cluster-net": [],
  })
  conf, mprov, tftproot = dirs

  def dhcp_run():
    plugin = DnsmasqDHCPConfig(js)
    plugin.dnsmasqConfDir = f"{conf}/"
    plugin.mprovDnsmasqDir = str(mprov)
    plugin.tftproot = str(tftproot)
    plugin.handle_jobs()
    return plugin.mainChanged, plugin.hostsChanged, plugin.completed

  assert dhcp_run() == (True, True, True)
  assert dhcp_run() == (False, False, True)
  js.session.routes["networkinterfaces/?network=cluster-net"] = [dict(NIC, mac="aa:bb:cc:00:00:02")]
  assert dhcp_run() == (False, True, True)


class FakeDnsmasq:
  """ A running dnsmasq as running_dnsmasq() returns it; records signals. """
  def __init__(self):
    self.signals = []
  def terminate(self):
    self.signals.append("TERM")
  def wait(self, timeout=None):
    pass
  def kill(self):
    self.signals.append("KILL")
  def send_signal(self, sig):
    self.signals.append(signal.Signals(sig).name)


@pytest.fixture
def running(monkeypatch):
  proc = FakeDnsmasq()
  monkeypatch.setattr(config, "running_dnsmasq", lambda binary, pidfile: proc)
  return proc


@pytest.mark.parametrize("main, hosts, signals, restarted", [
  (True, False, ["TERM"], True),    # main config: restart
  (True, True, ["TERM"], True),     # a restart re-reads the hosts too
  (False, True, ["SIGHUP"], False), # only hosts: reload, no restart
  (False, False, [], False),        # nothing changed: leave it alone
])
def test_dnsmasq_is_restarted_only_for_its_main_config(js, conf_plugin, running, monkeypatch, main, hosts, signals, restarted):
  started = starts(monkeypatch)
  # ipxe.conf is main config too; written once already, so unchanged now
  conf_plugin.handle_jobs()
  running.signals.clear()
  started.clear()
  conf_plugin.mainChanged, conf_plugin.hostsChanged = main, hosts
  conf_plugin.handle_jobs()
  assert running.signals == signals
  assert bool(started) == restarted


def test_a_changed_ipxe_conf_restarts_dnsmasq(js, conf_plugin, running, monkeypatch):
  started = starts(monkeypatch)
  # the first run writes ipxe.conf, a main config file
  conf_plugin.handle_jobs()
  assert running.signals == ["TERM"]
  assert len(started) == 1


@pytest.mark.parametrize("dns_ok, dhcp_ok, expected", [
  (True, True, [("dns", 4), ("dhcp", 4)]),
  (False, True, [("dns", 3), ("dhcp", 4)]),   # the DNS worker crashed
  (True, False, [("dns", 4), ("dhcp", 3)]),
  (None, True, [("dhcp", 4)]),                # no DNS worker ran: it claimed nothing
])
def test_jobs_get_their_workers_outcome(js, conf_plugin, monkeypatch, dns_ok, dhcp_ok, expected):
  starts(monkeypatch)
  conf_plugin.dnsOk, conf_plugin.dhcpOk = dns_ok, dhcp_ok
  conf_plugin.handle_jobs()
  assert [("dns" if '"dns-update"' in query else "dhcp", status) for _, status, query in js.job_updates] == expected


def test_jobs_fail_when_dnsmasq_wont_start(js, conf_plugin, monkeypatch):
  starts(monkeypatch, returncode=2)
  conf_plugin.dnsOk = conf_plugin.dhcpOk = True
  conf_plugin.handle_jobs()
  assert [status for _, status, _ in js.job_updates] == [3, 3]


def test_running_dnsmasq_is_found_by_pid_file_and_binary(tmp_path):
  pidfile = tmp_path / "dnsmasq.pid"
  assert service.running_dnsmasq(sys.executable, str(pidfile)) is None
  pidfile.write_text("not a pid\n")
  assert service.running_dnsmasq(sys.executable, str(pidfile)) is None
  # this test's own process stands in for dnsmasq
  pidfile.write_text(f"{os.getpid()}\n")
  assert service.running_dnsmasq(sys.executable, str(pidfile)).pid == os.getpid()
  # some other program owns that pid: not ours, never signalled
  assert service.running_dnsmasq("/usr/sbin/dnsmasq", str(pidfile)) is None


class CrashingWorker(threading.Thread):
  """ A DNS/DHCP worker whose run crashes before it completes. """
  mainChanged = False
  hostsChanged = False
  completed = False
  def __init__(self, js):
    super().__init__()
  def run(self):
    pass


class FinishedWorker(CrashingWorker):
  hostsChanged = True
  completed = True


class RecordedConfig(threading.Thread):
  runs = []
  def __init__(self, js):
    super().__init__()
  def run(self):
    RecordedConfig.runs.append(self)


def test_plugin_passes_worker_outcomes_to_config(js, monkeypatch):
  monkeypatch.setattr(dnsmasq_plugin, "DnsmasqDNSConfig", CrashingWorker)
  monkeypatch.setattr(dnsmasq_plugin, "DnsmasqDHCPConfig", FinishedWorker)
  monkeypatch.setattr(dnsmasq_plugin, "DnsmasqConfig", RecordedConfig)
  RecordedConfig.runs = []
  plugin = dnsmasq_plugin.dnsmasq(js)
  plugin.enableDNS = plugin.enableDHCP = True
  plugin.handle_jobs()
  [conf] = RecordedConfig.runs
  assert (conf.dnsOk, conf.dhcpOk) == (False, True)
  assert (conf.mainChanged, conf.hostsChanged) == (False, True)


@pytest.mark.parametrize("dns, dhcp_jobs, config_runs", [
  (False, False, False),  # DHCP only, idle: nothing to do this pass
  (False, True, True),    # DHCP only, with work
  (True, False, True),    # DNS on: config runs every pass (it keeps dnsmasq up)
])
def test_config_runs_when_there_is_work_or_dns_is_on(js, monkeypatch, dns, dhcp_jobs, config_runs):
  # the DHCP no-jobs branch set self.enableDHCP = False to get this (#163)
  monkeypatch.setattr(dnsmasq_plugin, "DnsmasqDNSConfig", FinishedWorker)
  monkeypatch.setattr(dnsmasq_plugin, "DnsmasqDHCPConfig", FinishedWorker)
  monkeypatch.setattr(dnsmasq_plugin, "DnsmasqConfig", RecordedConfig)
  RecordedConfig.runs = []
  js.has_jobs = dhcp_jobs
  plugin = dnsmasq_plugin.dnsmasq(js)
  plugin.enableDNS, plugin.enableDHCP = dns, True
  plugin.handle_jobs()
  assert bool(RecordedConfig.runs) == config_runs
  # and the setting itself is left alone
  assert plugin.enableDHCP is True


# A network the mPCC stops listing loses its hosts file, so dnsmasq stops
# answering for it after the reload (#244).

OTHER = dict(NETWORK, id=4, slug="old-net", domain="old.local")


def dns_worker(js, dirs):
  conf, mprov, _ = dirs
  plugin = DnsmasqDNSConfig(js)
  plugin.dnsmasqConfDir = f"{conf}/"
  plugin.mprovDnsmasqDir = str(mprov)
  plugin.handle_jobs()
  return plugin


def dhcp_worker(js, dirs):
  conf, mprov, tftproot = dirs
  plugin = DnsmasqDHCPConfig(js)
  plugin.dnsmasqConfDir = f"{conf}/"
  plugin.mprovDnsmasqDir = str(mprov)
  plugin.tftproot = str(tftproot)
  plugin.enableTFTP = False
  plugin.handle_jobs()
  return plugin


def both_networks(js, kind):
  query = "managedns" if kind == "dns" else "isdhcp"
  js.session.routes[f"networks/?{query}=True"] = [NETWORK, OTHER]
  for net in (NETWORK, OTHER):
    js.session.routes.update({
      f"networkinterfaces/?network={net['slug']}": [NIC],
      f"systembmcs/?network={net['id']}&detail": [],
      f"switches/?network={net['slug']}": [],
    })
  return f"networks/?{query}=True"


@pytest.mark.parametrize("kind, worker", [("dns", dns_worker), ("dhcp", dhcp_worker)])
def test_a_network_that_drops_out_loses_its_hosts_file(js, dirs, no_interfaces, kind, worker):
  _, mprov, _ = dirs
  route = both_networks(js, kind)
  worker(js, dirs)
  hosts = mprov / kind
  assert sorted(p.name for p in hosts.iterdir()) == [f"cluster-net-{kind}.conf", f"old-net-{kind}.conf"]
  # an operator's own files, and other names, are left alone
  (hosts / "site-extra.conf").write_text("# by hand\n")
  (hosts / f".scratch-{kind}.conf").write_text("")

  js.session.routes[route] = [NETWORK]
  plugin = worker(js, dirs)
  assert plugin.hostsChanged and plugin.completed
  assert sorted(p.name for p in hosts.iterdir()) == [f".scratch-{kind}.conf", f"cluster-net-{kind}.conf", "site-extra.conf"]
  # and nothing more to do next time
  plugin = worker(js, dirs)
  assert not plugin.hostsChanged


@pytest.mark.parametrize("kind, worker", [("dns", dns_worker), ("dhcp", dhcp_worker)])
def test_no_networks_at_all_removes_every_hosts_file(js, dirs, no_interfaces, kind, worker):
  _, mprov, _ = dirs
  route = both_networks(js, kind)
  worker(js, dirs)
  js.session.routes[route] = []
  assert worker(js, dirs).hostsChanged
  assert list((mprov / kind).iterdir()) == []


@pytest.mark.parametrize("kind, worker", [("dns", dns_worker), ("dhcp", dhcp_worker)])
def test_an_error_reply_removes_nothing(js, dirs, no_interfaces, kind, worker):
  _, mprov, _ = dirs
  route = both_networks(js, kind)
  worker(js, dirs)
  js.session.routes[route] = (500, [])
  plugin = worker(js, dirs)
  assert not plugin.hostsChanged
  assert len(list((mprov / kind).iterdir())) == 2


def test_dns_leaves_out_only_the_bmc_with_missing_fields(js, dirs, no_interfaces, capsys):
  # one bad BMC used to drop every BMC of the network
  js.session.routes.update({
    "networks/?managedns=True": [NETWORK],
    "networkinterfaces/?network=cluster-net": [],
    "systembmcs/?network=3&detail": [
      {"ipaddress": "10.0.0.51", "mac": "aa:bb:cc:00:01:01", "system": {"hostname": "node01-bmc"}},
      {"ipaddress": "10.0.0.52", "mac": "aa:bb:cc:00:01:02", "system": None},
      {"ipaddress": "10.0.0.53", "mac": "aa:bb:cc:00:01:03", "system": {"hostname": "node03-bmc"}},
    ],
    "switches/?network=cluster-net": [],
  })
  _, hosts = run_dns(js, dirs)
  assert [line.split() for line in config_lines(hosts)] == [
    ["10.0.0.51", "node01-bmc.cluster.local"],
    ["10.0.0.53", "node03-bmc.cluster.local"],
  ]
  assert "[W] dnsmasq/dns: leaving a BMC on network cluster-net out of DNS" in capsys.readouterr().out
