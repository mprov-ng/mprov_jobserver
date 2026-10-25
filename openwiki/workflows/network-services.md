---
type: Reference
title: Network Services - dnsmasq, NADS, and Genders
description: How mprov_jobserver generates dnsmasq DNS/DHCP/PXE-iPXE configuration from mPCC network data, builds iPXE boot binaries, auto-discovers unknown nodes over LLDP (NADS), and renders /etc/genders for cluster tools.
tags: [mprov, jobserver, dnsmasq, dhcp, dns, pxe, ipxe, lldp, nads, genders]
---

# Network Services

Three plugins turn mPCC inventory data into the network layer of bare-metal provisioning: `dnsmasq` (DNS + DHCP + TFTP/PXE boot files), `nads` (node auto-detection via LLDP), and `libgenders` (cluster host catalog). All follow the [Plugin Framework](../architecture/plugin-framework.md) contract.

## `dnsmasq` — DNS / DHCP / PXE config generation

`src/mprov_jobserver/plugins/dnsmasq.py` is an **umbrella plugin covering six job modules**: `pxe-update`, `pxe-delete`, `dns-update`, `dns-delete`, `dhcp-update`, `dhcp-delete`. `handle_jobs()` claims DNS/DHCP jobs and fans work out to three subplugins in `dnsmasq_mod/`, then always runs the config/lifecycle subplugin:

- **`dnsmasq_mod/dns.py` (`DnsmasqDNSConfig`)** — for each mPCC `networks/?managedns=True`, aggregates A/AAAA records from `networkinterfaces/`, `systembmcs/`, `switches/`, and the jobserver's own addresses on that network, renders `templates/dnsmasq/dns.conf.j2` plus per-network `<slug>-dns.conf` (`dns_host.conf.j2`) into `dnsmasqConfDir`, then SIGKILLs dnsmasq so the config subplugin restarts it.
- **`dnsmasq_mod/dhcp.py` (`DnsmasqDHCPConfig`)** — same pattern for `networks/?isdhcp=True`: renders `dhcp.conf` and per-network `<slug>-dhcp.conf` (`dhcp_host.conf.j2`), including fixed-address host entries tagged with the network slug (`set:<slug>`) and the TFTP root for PXE. Both dns.py and dhcp.py merge switch records into their host lists via the shared `host_list()` helper, which turns the mPCC's 404-with-null for a network with no switches into an empty list (#135). Also SIGKILLs dnsmasq.
- **`dnsmasq_mod/config.py` (`DnsmasqConfig`)** — owns the dnsmasq process lifecycle: renders `ipxe.conf.j2` into the confdir, resolves its own non-link-local IPv6 address for `bootserver6`, starts `/usr/sbin/dnsmasq --log-facility=-` as the configured `dnsmasqUser` if not running, writes `ipversionrouter.ipxe` and `menu.ipxe` boot files into the tftproot, and finally marks all dns/pxe/dhcp jobs in state 2 as complete.

So dns/dhcp threads write configs and *kill* dnsmasq; the config thread *restarts* it and closes out their job statuses — they are designed to run together in one plugin.

**iPXE binary build**: on first load, if `undionly.kpxe`, `snponly.efi`, or `snponly_ipv4.efi` are missing from `tftproot`, the plugin clones `github.com/ipxe/ipxe` at a pinned commit, patches `config/defaults/efi.h` to enable `DOWNLOAD_PROTO_HTTPS`, builds `undionly.kpxe` + `snponly.efi`, then rebuilds EFI with IPv6 disabled as `snponly_ipv4.efi`. `menu.ipxe`/`ipversionrouter.ipxe` templates route BIOS vs UEFI (and IPv4-only UEFI) clients to image boot entries built by the [Image & Repo Provisioning](image-provisioning.md) pipeline (menu uses `mprov_url` to fetch kernels from the mprov-webserver).

Config keys (`plugins/dnsmasq.yaml`): `enableDNS`, `enableDHCP`, `enableTFTP`, `dnsmasqConfDir` (`/etc/dnsmasq.d/`), `mprovDnsmasqDir`, `tftproot` (`/export/tftpboot` — moved to persistent storage in the container), `undionlyImg`, `dnsmasqUser`, `dnsmasqBinary`.

## `nads` — Node Auto Detection System

`src/mprov_jobserver/plugins/nads.py` answers "which switch port is this unknown node plugged into?" and is meant to run **runonce** on a freshly-installed node image (mode `jobserver -m nads`). Flow:

1. Disable Intel i40e firmware LLDP interception (`/sys/kernel/debug/i40e/*/command`), re-enabled at the end.
2. For each operstate-UP interface (pyroute2/netifaces), open a raw AF_PACKET socket in promisc mode and hand-parse LLDP TLVs (code adapted from lldp.discovery) up to `maxLLDPWait` seconds, extracting the neighbor System Name (switch) and Port ID.
3. POST `/systems/register` with `{switch, port, vendor, model (dmidecode), mac}`; optionally `/sbin/reboot` when `reboot: True`.

Config (`nads.yaml`): `provIntf`, `maxLLDPWait: 60`, `reboot: False`. The mPCC then knows the node's exact port and can drive PXE provisioning of it. The public `nads` systemimage is also the script-runner fallback entity (see [Script Execution](../workflows/script-execution.md)).

## `libgenders` — /etc/genders generation

`src/mprov_jobserver/plugins/libgenders.py` is job-driven: on a claimed job it GETs `systems/`, `systemgroups/`, `systemimages/`, `distros/` and writes `/etc/genders` lines `hostname os=<distro-slug>,image=<slug>,<groups...>,all` for pdsh/Conflib-style tools. Every name written is slugified (whitespace/`,`/`=` removed, underscores kept so mPCC slugs pass through), group attributes are lowercase, and `osdistro` is accepted as either an ID or a nested object so flat `systemimages/` lists work (#117). The file is mProv-managed: the plugin appends the contents of `/etc/genders.include` (a configurable `gendersInclude` path) after the generated entries, so user additions belong there; an unreadable include is skipped with a warning comment rather than failing the job (#136). Per-node errors become commented lines instead of aborting the job. There is no `libgenders.yaml`; `gendersFile` and `gendersInclude` are class defaults.

## Where to start when changing this area

- New per-network DHCP/DNS options → the Jinja templates in `src/mprov_jobserver/templates/dnsmasq/` plus the context built in the matching `dnsmasq_mod` subplugin.
- dnsmasq restart behavior is intentionally kill-then-supervise; changing it requires coordinating dns.py, dhcp.py, and config.py together.
- nads changes need real hardware or LLDP-simulated switches to test; the TLV parsing is low-level and partially borrowed code — treat it as vendored.

Source map: `src/mprov_jobserver/plugins/dnsmasq.py`, `dnsmasq_mod/{config,dns,dhcp}.py`, `templates/dnsmasq/*.j2`, `plugins/nads.py`, `plugins/libgenders.py`.
