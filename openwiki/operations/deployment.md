---
type: Playbook
title: Operations and Deployment
description: How to install, configure, containerize, and run mprov_jobserver — installer script, systemd units, Docker/podman container with env-var config templating, dev compose stack, and CI workflows.
tags: [mprov, jobserver, deployment, docker, systemd, install, ci]
---

# Operations & Deployment

## Bare-metal install

`install_mprov_jobserver.sh` (repo root; README recommends `wget ... | bash` as root):

1. Verifies Python ≥ 3.8 via `python3 --version` and alternatives.
2. On RHEL-family: enables `powertools` (<9) or `crb` (≥9), installs EPEL.
3. `dnf install python3 python3-pip git wget iproute dnsmasq tcpdump ipmitool which parted-devel`, then `pip3 install mprov_jobserver pyyaml requests jinja2`.

Runtime dependencies are in `requirements.txt` (pyyaml, jinja2, requests, python-slugify, py-dmidecode, netifaces, pyparted, sh, pyroute2 0.8.1 pinned for py3.8 compat, dnspython, distro, psutil). Packaging metadata: `setup.cfg` / `setup.py`; package data ships all plugin YAMLs and `templates/*/*`.

First run of the `mprov_jobserver` binary with no `/etc/mprov/jobserver.yaml` copies template configs into `/etc/mprov/` and exits for editing (see [Architecture Overview](../architecture/overview.md)). Service units: `mprov_jobserver.service` (systemd), `mprov_jobserver_postboot.service` (runs post-boot scripting via script-runner, see [Script Execution](../workflows/script-execution.md)), `mprov_jobserver.conf` (service drop-in), `systemd-system.conf` (tuning sample).

## Container image

`Dockerfile` / `Containerfile` (identical): Rocky Linux 8.8 base with EPEL + powertools + Development Tools, python38 and python3.11, dnsmasq/ipmitool/tcpdump/iproute, then installs `mprov_jobserver` from a wheel in `dist/` if present (dev builds) or from PyPI otherwise. Exposes `69/udp` (TFTP), `67/udp` (DHCP), `53/udp` (DNS), `8080/tcp` (webserver) — matching the dnsmasq and mprov-webserver plugins in [Network Services](../workflows/network-services.md) and [Image & Repo Provisioning](../workflows/image-provisioning.md).

**Env-var config templating**: the image ships `jobserver.yaml.j2`; `start_server.sh` sources `/etc/mprov/apikey` if present (written by an mPCC container), renders the template with inline Jinja over env vars, and execs the job server. Variables: `MPROV_URL`, `MPROV_APIKEY`, `MPROV_ADDRESS`, `MPROV_MODS` (space-separated module list), `MPROV_ARGS` (extra CLI args).

## Dev stack

`dev-docker-compose.yml` runs the dev image with `network_mode: host`, `privileged: true`, systemd init, volumes for `/etc/mprov` config and `/export` (images/repos/tftpboot persistence — `/tftpboot` was deliberately moved under `/export/tftpboot` for persistence), env from `stack.env` (copied from `stack.env.example`; git ignores `stack.env`), and `wait-for-it.sh` gating on the local mPCC at `localhost:80`.

## CI

`.github/workflows/`: `ci_build.yml` (build/publish), `dockerimage-dev.yml` (dev container image → `ghcr.io/mprov-ng/mprov-jobserver`, versioned `X.Y.Z.devN` from `VERSION` on pushes to main), `update_website.yml`, and `openwiki-update.yml` (this documentation). Pull requests run the dev/release workflows as dry runs that stop after the tests; `.github/dependabot.yml` keeps Actions deps current. The **pytest suite in `tests/`** runs inside the just-built jobserver image via `tests/Dockerfile` (adds pytest + `genders` for `nodeattr` checks), so tests use the same Rocky build, Python, and dnsmasq that ship. `gen_changelog.sh` generates release notes from git history; `VERSION` is the source of truth for releases.

## Operational checklists

- **Changing plugin config keys**: update both the plugin class default and its YAML in `src/mprov_jobserver/plugins/`; existing `/etc/mprov/plugins/*.yaml` on deployed hosts are not auto-migrated.
- **New Python dependency**: add to `requirements.txt` *and* the Dockerfile install line if it has system build deps (e.g. pyparted needs parted-devel).
- **Container**: any new listening port needs an `EXPOSE` update; anything stateful belongs under `/export` or the `mprov-conf` volume.
- **Changing plugin logic**: add or update a pytest case in `tests/` (the fixtures in `tests/conftest.py` serve canned mPCC responses and stub out host side effects); CI runs the suite in the shipped image. For live smoke-testing use `-r -d -m <module> -c <config>` against a dev mPCC.

Source map: `install_mprov_jobserver.sh`, `Dockerfile`, `start_server.sh`, `jobserver.yaml.j2`, `dev-docker-compose.yml`, `stack.env.example`, `mprov_jobserver*.service`, `setup.cfg`, `requirements.txt`, `.github/workflows/`.
