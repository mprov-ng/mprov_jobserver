---
type: Playbook
title: Operations and Deployment
description: How to install, configure, containerize, and run mprov_jobserver — installer script, systemd units, Docker/podman container with env-var config templating, dev compose stack, and CI workflows.
tags: [mprov, jobserver, deployment, docker, systemd, install, ci]
---

# Operations & Deployment

## Supported deployment: container

The job server runs as a container; **bare-metal install is no longer supported or documented** (#300). `install_mprov_jobserver.sh` and the systemd unit files (`src/mprov_jobserver/systemd/`) are still in the repo, but nothing here documents or tests running the job server as a system service. The PyPI package stays published because `image-update` pip-installs it into every built image so nodes get `script-runner` and `MProvScript` (see [Script Execution](../workflows/script-execution.md)) — not because job servers install from it.

Runtime dependencies are in `requirements.txt` (pyyaml, jinja2, requests, python-slugify, py-dmidecode, netifaces, pyparted, sh, pyroute2 0.8.1 pinned for py3.8 compat, dnspython, distro, psutil). Packaging metadata: `setup.cfg` / `setup.py`; package data ships all plugin YAMLs and `templates/*/*`.

First run of the `mprov_jobserver` binary with no `/etc/mprov/jobserver.yaml` copies template configs into `/etc/mprov/` and exits for editing (see [Architecture Overview](../architecture/overview.md)) — in the container this doesn't apply, since the entrypoint always renders the config from the environment.

## Container image

`Dockerfile` / `Containerfile` (identical): Rocky Linux 8.8 base with EPEL + powertools + Development Tools, python38 and python3.11, dnsmasq/ipmitool/tcpdump/iproute, then installs `mprov_jobserver` from a wheel in `dist/` if present (dev builds) or from PyPI otherwise. Exposes `69/udp` (TFTP), `67/udp` (DHCP), `53/udp` (DNS), `8080/tcp` (webserver) — matching the dnsmasq and mprov-webserver plugins in [Network Services](../workflows/network-services.md) and [Image & Repo Provisioning](../workflows/image-provisioning.md).

**Env-var config templating**: the image ships `jobserver.yaml.j2`; `start_server.sh` sources `/etc/mprov/apikey` if present (written by an mPCC container), renders the template with inline Jinja over env vars, and execs the job server. Variables: `MPROV_URL`, `MPROV_APIKEY`, `MPROV_ADDRESS`, `MPROV_MODS` (space-separated module list), `MPROV_ARGS` (extra CLI args), `TZ`, and the dev-only `MPROV_SKIP_VERSION_CHECK` (see [Architecture Overview](../architecture/overview.md)). Because the file is regenerated on every start, per-module settings belong in `/etc/mprov/plugins/` on the config volume, and globals with no env var in a user-supplied config file pointed at via `MPROV_ARGS`. The `ENTRYPOINT` is exec-form `/start_server.sh` (no shell-expanded argument — `"${MPROV_ARGS}"` used to reach the launcher as a literal string), and both `start_server.sh` and the `mprov_jobserver` launcher `exec` the job server so a container SIGTERM reaches it instead of orphaning it (#294). The dead `mprov_jobserver.conf` systemd drop-in was dropped from the image — the unit it configured isn't installed there (#295).

## Dev stack

`dev-docker-compose.yml` runs the dev image with `network_mode: host`, `privileged: true`, systemd init, volumes for `/etc/mprov` config and `/export` (images/repos/tftpboot persistence — `/tftpboot` was deliberately moved under `/export/tftpboot` for persistence), env from `stack.env` (copied from `stack.env.example`; git ignores `stack.env`), and `wait-for-it.sh` gating on the local mPCC at `localhost:80`.

## CI

`.github/workflows/`: `ci_build.yml` (build/publish), `dockerimage-dev.yml` (dev container image → `ghcr.io/mprov-ng/mprov-jobserver`, versioned `X.Y.Z.devN` from `VERSION` on pushes to main, with concurrency so only the newest push publishes, #247), `lint.yml` (ruff errors-only + ShellCheck on every PR/push, independent of the image build, #187), `update_website.yml`, and `openwiki-update.yml` (this documentation). Pull requests run the dev/release workflows as dry runs that stop after the tests; `.github/dependabot.yml` keeps Actions deps current. The **pytest suite in `tests/`** runs inside the just-built jobserver image via `tests/Dockerfile` (adds pytest + `genders` for `nodeattr` checks), so tests use the same Rocky build, Python, and dnsmasq that ship. `gen_changelog.sh` generates release notes from git history; `VERSION` is the source of truth for releases.

## Operational checklists

- **Changing plugin config keys**: update both the plugin class default and its YAML in `src/mprov_jobserver/plugins/`; existing `/etc/mprov/plugins/*.yaml` on deployed hosts are not auto-migrated.
- **New Python dependency**: add to `requirements.txt` *and* the Dockerfile install line if it has system build deps (e.g. pyparted needs parted-devel).
- **Container**: any new listening port needs an `EXPOSE` update; anything stateful belongs under `/export` or the `mprov-conf` volume.
- **Changing plugin logic**: add or update a pytest case in `tests/` (the fixtures in `tests/conftest.py` serve canned mPCC responses and stub out host side effects); CI runs the suite in the shipped image. For live smoke-testing use `-r -d -m <module> -c <config>` against a dev mPCC.

Source map: `install_mprov_jobserver.sh`, `Dockerfile`, `start_server.sh`, `jobserver.yaml.j2`, `dev-docker-compose.yml`, `stack.env.example`, `src/mprov_jobserver/systemd/`, `setup.cfg`, `requirements.txt`, `.github/workflows/`.
