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
3. **Starts a session** — `startSession()` sets `Authorization: Api-Key <apikey>` on a `requests.Session`, installs the API version lock (`version.install()`, below) on it, GETs `mprovURL` to validate, and determines `ip_address` from `myaddress` (or socket autodetect as fallback). For an https `mprovURL`, `tls.ca_bundle()` (`src/mprov_jobserver/tls.py`) picks the CA to verify against: the global `caBundle` file if set (a missing file is fatal), else the OS bundle (`/etc/pki/tls/certs/ca-bundle.crt` / `/etc/ssl/certs/ca-certificates.crt`), else requests' certifi default — PyPI `requests` ignored site/self-signed CAs added via `update-ca-trust` (#102). A failed connection logs the specific reason (DNS, refused, certificate) and, for an SSLError, points at `caBundle`; the reason is then said **only once** until it changes, since the retry runs every few seconds and the cause rarely changes (#293).
4. **Loads plugins** — `load_plugins()` resolves each name in `jobmodules` to a class in the `mprov_jobserver.plugins` package (hyphens → underscores) that subclasses `JobServerPlugin`. See [Plugin Framework](plugin-framework.md).
5. **Registers** — `register_server()` POSTs to `jobservers/` with hostname, address, module list, `one_minute_load`, the webserver port if `mprov-webserver` is configured, and — when the mPCC advertises the `host-id` feature — a stable `host_id` (#113): the global `hostId` if set, else the id kept in `host-id` next to the config file, created from a random uuid4 on first use (deliberately *not* `/etc/machine-id`, which is identical across containers built from one image). It **never stops heartbeating under load** (#147): high load (1-minute load ≥ CPU count with `loadmon` enabled) only logs a warning, because the mPCC deletes a job server that goes quiet for a minute — it would drop out of its images' `hosted_by` and its jobs would lose their server. The mPCC steers work using the reported load instead. The response's `pk` becomes `self.id`, used everywhere to claim work and self-attach as image/repo host, and its `features` list becomes `self.features` — re-read on every heartbeat so a downgraded mPCC stops being sent what it no longer understands (#248). `register_server` accepts the mPCC reply either as a JSON object or double-encoded as a JSON string holding the object, via `JobServer.reply_object()` (#155).

`start()` is the main loop: once per `heartbeatInterval` seconds it re-registers (unless runonce; registration is skipped entirely in `__init__` until a session actually started, so a job server that never reached the mPCC doesn't leave an unused row on it, #293) and starts a thread for each enabled module that isn't already running, calling `importlib.reload()` on the plugin module first so plugin code edits are picked up without restart. Modules wait while the server is still unregistered (a failed first registration is retried each heartbeat rather than exiting, and without an id plugins would claim jobs for job server -1, #159), and the first loop pass sweeps orphaned jobs (see below). Dead threads are reaped; SIGTERM sets `running=False` and the loop `join()`s all threads before exiting.

## Job status protocol

`JobServer.update_job_status(job_module, status, jobid=None, jobquery="")` is the shared job state machine helper:

- `1` pending → `2` running (sets `start_time`) → `4` success / `3` failure (sets `end_time`). Times go as UTC with an explicit offset — a bare local clock was read in the mPCC's `TIME_ZONE` and came out wrong on job servers in other zones (#186).
- **Claiming** (`status=2`) GETs only this module's *pending* jobs (`jobs/?module=<mod>&status=1`); **finishing** GETs only this server's running jobs (`jobs/?jobserver=<id>&module=<mod>&status=2`). The old `search=` matched any status and any job server, so a module took over jobs other servers were running and fetched the module's whole history (#112). When the mPCC advertises the `job-claim` feature, claiming is atomic: `POST jobs/<id>/claim/` with our id, and a 4xx (claimed elsewhere) quietly skips the job (#112).
- Jobs already in state 3/4 are never touched; `jobserver` is set to `self.id` on update. Each transition logs one fixed-format line for log search (Loki/Grafana).
- `fail_orphaned_jobs()` (#94, #180) fails jobs the mPCC still shows RUNNING for this server when it has nothing running: once before modules start (the server stopped mid-job) and whenever a module thread ends while still holding claimed jobs (it crashed or forgot one). Such completions pass `orphaned=True` down to `log_job()`, which logs "last seen RUNNING Ns ago" instead of a run-time in parentheses — the gap from `start_time` includes however long the server was down, so a dashboard must not read it as a multi-hour build (#305).
- Returns the number of jobs updated — plugins treat `0` as "no work for me, return".

Plugin convenience wrappers `set_job_running/success/failure` live in `src/mprov_jobserver/plugins/plugin.py`.

## Configuration system

- Default file `/etc/mprov/jobserver.yaml`; fallback `./jobserver.yaml`. Structure: a list whose first item has a `global:` key, followed by `- !include plugins/*.yaml` (see `src/mprov_jobserver/jobserver.yaml`).
- A custom `!include` YAML constructor (`JobServer.yaml_include`) globs the given path relative to the including file and merges each plugin YAML's first list entry, so each plugin gets a top-level key named after its job module (e.g. `dnsmasq:`, `mprov-webserver:`).
- The list of config entries is flattened into one dict on `JobServer.config_data` by `merge_config()` (`src/mprov_jobserver/config.py`, #301): a later document's section replaces an earlier one of the same name, **except** `global:`, whose settings merge key by key — so a plugin file can add or change a single global setting (e.g. `caBundle`, `logLevel`) without wiping the `mprovURL`/`apikey` the container renders from its environment. `!include`d files are iterated in sorted order so which one wins is deterministic. `script.py` flattens its config the same way. `global` keys are then mapped onto `JobServer` attributes by name (unknown global keys are fatal; `mprovURL` gets a trailing `/` added).
- Global keys: `mprovURL`, `apikey`, `heartbeatInterval`, `myaddress`, `addresses` (per-network registration addresses, below), `jobmodules`, `runonce`, `loadmon` (load-shedding toggle used here and by [Image & Repo Provisioning](../workflows/image-provisioning.md) webserver), `caBundle` (https CA file, above), `hostId` (stable registration id, above), `logLevel` and `logTimestamps` (below).
- Plugin classes receive their config the same way by name-matching (`plugins/plugin.py::load_config`).

## Multi-network addresses

A job server on several networks registers one address per network so the mPCC sends clients on each network to the matching address (#114). If the global `addresses` list is set (each entry an address, or a mapping of `address`, optional mPCC `network` id, optional per-network `mprovURL`), those are registered; otherwise the addresses come from the interfaces that are up (`interface_addresses()` in `app.py`, which skips loopback, link-local, and container/VM bridges). Registration carries them only when the mPCC advertises both the `jobserver-addresses` and `jobserver-address-heartbeat` features. Each registered address then gets its **own heartbeat** every loop, sent from that address through a source-bound `requests.Session` (`SourceAddressAdapter`) to its `mprovURL`, with a 5s timeout so a dead network can't stall the main loop; a failing address is logged once until it recovers, and the mPCC stops steering clients to it. Addresses whose family the target mPCC URL lacks (e.g. IPv6-only when the mPCC is IPv4-only) are **not registered at all** — binding a heartbeat to them could never succeed, so the mPCC would send clients to an address it never hears from (#292); the skip is logged once and undone when reachability returns.

## API version lock

`src/mprov_jobserver/version.py` (#287) version-matches the job server to its mPCC. Every mPCC session (including the per-address sessions above) sends an `mProv-Version` header carrying the job server's major.minor (from package metadata, else the repo `VERSION` file) and checks the header on every reply through a `requests` response hook; a mismatch (or an mPCC old enough to send no header, #452) is logged once per version pair. `MPROV_SKIP_VERSION_CHECK=1` in the process environment turns the check off for dev/CI only.

How a mismatch is handled depends on **`version.install(session, enforce=...)`**, which `app.py` passes as `enforce=self.register`:

- **Registering job server (`enforce=True`, the default)** — the mismatch raises `VersionMismatch`, a `RequestException` subclass so existing error handling treats it as a failed request. This is unchanged.
- **Script runner (`register` is `False` — `-d`, and `-m`, which is what `image_update` and a node's post-boot run use)** — `warn_response()` logs the same mismatch at warning level and lets the reply through. A runner is a short-lived *client* of whatever mPCC the site already has, not a job server registering with it, and refusing its replies doesn't fail it but **hangs** it: `runonce` only clears `self.running` after the job modules run, which needs a working session. That is how a 0.6 runner built into an image against a pre-0.6 mPCC left build jobs stuck in `RUNNING` and nodes polling every heartbeat instead of running post-boot scripts (#303). Warning instead of refusing covers any version problem, so a future 0.6 runner meeting a 0.7 mPCC doesn't hang the same way.

`install()` swaps the two hooks on a session rather than appending both, so a session that is reinstalled with different `enforce` ends up with exactly one. Scripts themselves still do not enforce the lock: `MProvScript` can optionally report its own version but never checks the mPCC's, since scripts are baked into images that can't be rebuilt per mPCC release (see [Script Execution](../workflows/script-execution.md)).

## Logging

`src/mprov_jobserver/log.py` provides every module's logger (#104, #103). Each line is `[E] image-update: message` with an E/W/I/D level tag and the short module name derived from the logger name (`plugins.image_update_mod.rhel` → `image-update/rhel`), so plain files, `docker logs` and Loki line filters can pick out problems. When stdout is the journald stream (`JOURNAL_STREAM` device:inode match), lines also carry the sd-daemon priority prefix so `journalctl -p warning` works. Output is flushed per line so it stays ordered with the dnf/wget output the daemon writes to the same stdout. `logLevel` (error/warning/info/debug, default info) and optional `logTimestamps` are global config; an invalid level exits at startup.

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

Source map: `src/mprov_jobserver/main.py`, `src/mprov_jobserver/app.py`, `src/mprov_jobserver/version.py`, `src/mprov_jobserver/jobserver.yaml`.
