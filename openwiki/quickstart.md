---
type: Reference
title: mprov_jobserver Quickstart
description: Entry point for the mProv Job Server documentation — what it is, how it fits into the mProv control center (mPCC), how it is run, and where to go next in the wiki.
tags: [mprov, jobserver, provisioning, quickstart, bare-metal]
---

# mprov_jobserver Quickstart

`mprov_jobserver` is the worker component of **mProv** (mprov-ng), a bare-metal provisioning system. It is a Python daemon that connects to the **mProv Control Center (mPCC)** over its REST API, polls for queued jobs, and executes them: mirroring package repositories, building bootable OS images (placing the mPCC's managed files into them along the way), serving images over HTTP for PXE boot, generating dnsmasq DNS/DHCP/PXE configuration, running user scripts against images or booted hosts, rotating/checking BMC credentials over Redfish/IPMI, and auto-discovering unknown nodes via LLDP.

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
| BMC credential change/check/recovery | `bmc-rotate` | [BMC Credential Rotation](workflows/bmc-rotation.md) |
| Packaging, container, systemd, CI | — | [Operations & Deployment](operations/deployment.md) |

## Quickstart: running it

The job server runs as a container (`ghcr.io/mprov-ng/mprov-jobserver`); bare-metal installation is no longer supported or documented (`README.md`, #300). The supported way is [mprov-compose](https://github.com/mprov-ng/mprov-compose), which brings up the mPCC, its database, and a job server together; run the image alone against a remote mPCC with the same environment.

1. Give the container its settings **in the environment**, not by editing a config file: `MPROV_URL` (mPCC URL — from 0.7.0 an `https://` address or internal name the mPCC's certificate is for, not localhost), `MPROV_ENROLL_TOKEN` (or `/etc/mprov/enroll_token`) and `MPROV_CA_FINGERPRINT` for first enrollment, `MPROV_TLS_DIR` (where the certificate is kept — must survive container recreation), `MPROV_ADDRESS`, `MPROV_MODS` (space-separated module list), `MPROV_ARGS`, `TZ`, and optionally `MPROV_CA_BUNDLE` / `MPROV_LOG_LEVEL`. `MPROV_APIKEY` is a deprecated fallback only (dropped in 0.8.0). `/etc/mprov/jobserver.yaml` is rendered from these on every start, so edits to it don't survive a restart; per-module settings go in `/etc/mprov/plugins/`, and since #301 a small `global:` section in one of those files merges into the rendered globals (see [Architecture Overview](architecture/overview.md)) — a full custom config via `MPROV_ARGS=-c <file>` still works but is rarely needed.
2. Start the stack and watch the log: on first connect the job server enrolls for its certificate (see [Identity and Certificate Enrollment](architecture/identity.md)) and starts a repo sync, which is required before images can be built. A job server that can't reach the mPCC, or is on a different API version, says so once and keeps retrying without registering (see [Architecture Overview](architecture/overview.md)).

The PyPI package is still published and supported — but for **nodes**, not job servers: `image-update` unpacks the job server into every built image as a portable Python bundle at `/usr/libexec/mprov` (the PyPI package needs Python ≥ 3.11; node images don't need any particular Python, #309/#311), so provisioned hosts get `script-runner` and the `MProvScript` class (see [Script Execution](workflows/script-execution.md)).

Useful CLI flags (global): `-c <configfile>`, `-r` runonce mode, `-d` do not register with the mPCC, `-m mod1,mod2` run specific modules once. See `README.md` and [Operations & Deployment](operations/deployment.md).

## Things to watch out for

- **Plugin coupling is enforced at runtime**: `image-update`, `image-delete` peers, `image-sync` and `repo-sync` hard-exit unless `mprov-webserver` is also enabled (`sys.exit` in `load_config`). `image-sync`/`repo-sync` also require their sync directory to sit inside a declared `dataRoot` so the cleanup sweep can only touch mprov-owned space.
- Plugins are **threads, not processes**; a crash in one module (many call `sys.exit(1)`) takes down the whole daemon.
- Config keys are mapped onto plugin class attributes by name; unknown keys print an error but do not stop startup (`src/mprov_jobserver/plugins/plugin.py`).
- The image build path uses heavy `os.system`/chroot of the partially built image by design — treat the job server as requiring root and trusted mPCC input.

## Backlog

- **nads LLDP TLV parsing internals** — `src/mprov_jobserver/plugins/nads.py` implements a hand-rolled LLDP decoder (borrowed from lldp.discovery); only its role is documented in [Network Services](workflows/network-services.md).
- **libgenders output format** — `src/mprov_jobserver/plugins/libgenders.py` generates `/etc/genders`; covered briefly, not in depth.
- **mPCC side of the API contract** — the mPCC (separate repo `mprov-ng/mpcc`) defines the REST serializers; field semantics here are inferred from client usage.
 defines the REST serializers; field semantics here are inferred from client usage.
