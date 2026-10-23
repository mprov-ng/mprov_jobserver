---
type: Reference
title: Job Server Architecture Overview
description: How the mprov_jobserver daemon boots, loads YAML config with !include, authenticates to the mProv Control Center over an API-key session, registers with heartbeat, and drives job-module plugin threads and job status updates.
tags: [mprov, jobserver, architecture, macc-session, heartbeat, config]
---

# Architecture Overview

The job server is a single Python process made of three parts: the `JobServer` orchestrator, a YAML configuration tree, and a set of job-module plugin threads that talk to the mProv Control Center (mPCC) REST API.

## Process lifecycle

Entry point: `src/mprov_jobserver/main.py` (installed as the `mprov_jobserver` script, see `setup.cfg`). It installs SIGINT/SIGTERM handlers that call `JobServer.stop()`, parses `-r` (runonce) and `-c <configfile>`, and — if no config exists at `/etc/mprov/jobserver.yaml` — copies the package's template YAMLs (`src/mprov_jobserver/jobserver.yaml` plus `plugins/*.yaml`) into `/etc/mprov/`, tells the user to edit them, and exits.

`JobServer.__init__` (`src/mprov_jobserver/app.py`) then:

1. **Loads config** — `load_config()` below.
2. **Applies CLI overrides** — `-d` disables registration; `-m mod1,mod2` forces a module list, implies runonce, and disables registration (used by the image-build chroot flow, see [Script Execution](../workflows/script-execution.md)).
3. **Starts a session** — `startSession()` sets `Authorization: Api-Key <apikey>` on a `requests.Session`, GETs `mprovURL` to validate, and determines `ip_address` from `myaddress` (or socket autodetect as fallback).
4. **Loads plugins** — `load_plugins()` resolves each name in `jobmodules` to a class in the `mprov_jobserver.plugins` package (hyphens → underscores) that subclasses `JobServerPlugin`. See [Plugin Framework](plugin-framework.md).
5. **Registers** — `register_server()` POSTs to `jobservers/` with hostname, address, module list, `one_minute_load`, and the webserver port if `mprov-webserver` is configured. High load (≥ CPU count, with `loadmon` enabled) skips registration so the mPCC routes work elsewhere. The response's `pk` becomes `self.id`, used everywhere to claim work and self-attach as image/repo host.

`start()` is the main loop: once per `heartbeatInterval` seconds it re-registers (unless runonce) and starts a thread for each enabled module that isn't already running, calling `importlib.reload()` on the plugin module first so plugin code edits are picked up without restart. Dead threads are reaped; SIGTERM sets `running=False` and the loop `join()`s all threads before exiting.

## Job status protocol

`JobServer.update_job_status(job_module, status, jobid=None, jobquery="")` is the shared job state machine helper. It GETs matching jobs (`jobs/?search=<module>`, a custom query, or one job by id), then PATCHes each job that isn't already running/terminal:

- `1` pending → `2` running (sets `start_time`) → `4` success / `3` failure (sets `end_time`).
- Jobs already in state 3/4 are never touched; `jobserver` is set to `self.id` on update.
- Returns the number of jobs updated — plugins treat `0` as "no work for me, return".

Plugin convenience wrappers `set_job_running/success/failure` live in `src/mprov_jobserver/plugins/plugin.py`.

## Configuration system

- Default file `/etc/mprov/jobserver.yaml`; fallback `./jobserver.yaml`. Structure: a list whose first item has a `global:` key, followed by `- !include plugins/*.yaml` (see `src/mprov_jobserver/jobserver.yaml`).
- A custom `!include` YAML constructor (`JobServer.yaml_include`) globs the given path relative to the including file and merges each plugin YAML's first list entry, so each plugin gets a top-level key named after its job module (e.g. `dnsmasq:`, `mprov-webserver:`).
- The list of config entries is flattened into one dict on `JobServer.config_data`; `global` keys are mapped onto `JobServer` attributes by name (unknown global keys are fatal; `mprovURL` gets a trailing `/` added).
- Global keys: `mprovURL`, `apikey`, `heartbeatInterval`, `myaddress`, `jobmodules`, `runonce`, `loadmon` (load-shedding toggle used here and by [Image & Repo Provisioning](../workflows/image-provisioning.md) webserver).
- Plugin classes receive their config the same way by name-matching (`plugins/plugin.py::load_config`).

## mPCC REST surface used by the job server

The mPCC (separate project) exposes a Django/DRF-style API. Endpoints consumed by this repo, grounded in call sites:

| Endpoint | Used by / for |
|---|---|
| `jobs/`, `jobs/<id>/` | job claim & status PATCH (`app.py`) |
| `jobservers/` | registration/heartbeat (`app.py`) |
| `images/<slug>[.img|.initramfs|.vmlinuz]` | binary artifact download & serving |
| `systemimages/<slug>/` (+`?addjs`) | image metadata, version checks, host self-registration |
| `repos/<id>/` (+`?addjs`) | repo metadata, `hosted_by`, version checks |
| `ostypes/` | self-registration of the `rhel` os type by update submodules |
| `networks/`, `networkinterfaces/`, `systembmcs/`, `switches/` | dnsmasq DNS/DHCP config generation |
| `systems/`, `systems/register`, `systemgroups/`, `distros/` | script-runner, nads, libgenders, genders |

## Where to start when changing this area

- New global config option: add the attribute to `JobServer` in `app.py` (name-matching requires it to exist) and document it in `src/mprov_jobserver/jobserver.yaml`.
- Session/auth or registration changes affect every plugin; test with `-r -d` runonce flows and the container dev stack (`dev-docker-compose.yml`, see [Operations & Deployment](../operations/deployment.md)).
- A pytest suite lives in `tests/` (fake mPCC session, plus `test_syntax.py` which compiles every module with `SyntaxWarning` as an error); it runs in CI inside the shipped container image via `tests/Dockerfile` — see [Operations & Deployment](../operations/deployment.md). Manual smoke-testing of a single module is still `-m <module> -c <config>` against a dev mPCC.

Source map: `src/mprov_jobserver/main.py`, `src/mprov_jobserver/app.py`, `src/mprov_jobserver/jobserver.yaml`.
