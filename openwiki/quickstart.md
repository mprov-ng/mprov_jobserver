---
type: Reference
title: mprov_jobserver Quickstart
description: Entry point for the mProv Job Server documentation — what it is, how it fits into the mProv control center (mPCC), how it is run, and where to go next in the wiki.
tags: [mprov, jobserver, provisioning, quickstart, bare-metal]
---

# mprov_jobserver Quickstart

`mprov_jobserver` is the worker component of **mProv** (mprov-ng), a bare-metal provisioning system. It is a Python daemon that connects to the **mProv Control Center (mPCC)** over its REST API, polls for queued jobs, and executes them: mirroring package repositories, building bootable OS images, serving images over HTTP for PXE boot, generating dnsmasq DNS/DHCP/PXE configuration, running user scripts against images or booted hosts, and auto-discovering unknown nodes via LLDP.

It ships as the PyPI package `mprov_jobserver` (see `setup.cfg`), runs as root on RHEL-family Linux, and is also packaged as a container (`Dockerfile`, `ghcr.io/mprov-ng/mprov-jobserver`).

## How it works in one paragraph

The `JobServer` class (`src/mprov_jobserver/app.py`) authenticates to the mPCC with an API key, registers itself, then runs a heartbeat loop that starts one thread per enabled **job module plugin**. Plugins poll the mPCC for pending jobs of their module, do the work (dnf reposync, chroot image builds, config template rendering, ...), and PATCH job status back (pending → running → success/failure). See [Architecture Overview](architecture/overview.md) and the [Plugin Framework](architecture/plugin-framework.md).

## Major domains

| Domain | Job modules | Documented in |
|---|---|---|
| Core daemon, config, mPCC session | — | [Architecture Overview](architecture/overview.md) |
| Plugin framework | — | [Plugin Framework](architecture/plugin-framework.md) |
| Repo mirroring & image build/serve/delete | `repo-update`, `repo-sync`, `image-update`, `image-sync`, `image-delete`, `repo-delete`, `mprov-webserver` | [Image & Repo Provisioning](workflows/image-provisioning.md) |
| DNS / DHCP / PXE / node discovery / genders | `dnsmasq` (pxe/dns/dhcp ±update/delete), `nads`, `libgenders` | [Network Services](workflows/network-services.md) |
| Script execution on images and hosts | `script-runner` | [Script Execution](workflows/script-execution.md) |
| Packaging, container, systemd, CI | — | [Operations & Deployment](operations/deployment.md) |

## Quickstart: running it

1. Install (RHEL-family, as root, Python ≥ 3.8):
   `wget https://raw.githubusercontent.com/mprov-ng/mprov_jobserver/main/install_mprov_jobserver.sh -O - | bash`
2. Create an API key in the mPCC and edit `/etc/mprov/jobserver.yaml`: set `mprovURL` (use an IP or internal name, not localhost), `apikey`, `myaddress`, and enable the `jobmodules` you want. A template is copied to `/etc/mprov/` on first run (`src/mprov_jobserver/main.py`).
3. `systemctl enable --now mprov_jobserver`. On first connect the job server starts a repo sync, which is required before images can be built.

Useful CLI flags (global): `-c <configfile>`, `-r` runonce mode, `-d` do not register with the mPCC, `-m mod1,mod2` run specific modules once. See `README.md` and [Operations & Deployment](operations/deployment.md).

## Things to watch out for

- **Plugin coupling is enforced at runtime**: `image-update`, `image-delete` peers, `image-sync` and `repo-sync` hard-exit unless `mprov-webserver` is also enabled (`sys.exit` in `load_config`).
- Plugins are **threads, not processes**; a crash in one module (many call `sys.exit(1)`) takes down the whole daemon.
- Config keys are mapped onto plugin class attributes by name; unknown keys print an error but do not stop startup (`src/mprov_jobserver/plugins/plugin.py`).
- The image build path uses heavy `os.system`/chroot of the partially built image by design — treat the job server as requiring root and trusted mPCC input.

## Backlog

- **nads LLDP TLV parsing internals** — `src/mprov_jobserver/plugins/nads.py` implements a hand-rolled LLDP decoder (borrowed from lldp.discovery); only its role is documented in [Network Services](workflows/network-services.md).
- **libgenders output format** — `src/mprov_jobserver/plugins/libgenders.py` generates `/etc/genders`; covered briefly, not in depth.
- **mPCC side of the API contract** — the mPCC (separate repo `mprov-ng/mpcc`) defines the REST serializers; field semantics here are inferred from client usage.
