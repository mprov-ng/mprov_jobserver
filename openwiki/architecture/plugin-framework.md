---
type: Reference
title: Job Module Plugin Framework
description: How mprov_jobserver job modules are structured as JobServerPlugin threads, discovered from the plugins package, configured from merged YAML, and supervised by the JobServer heartbeat loop, plus how to add a new plugin.
tags: [mprov, jobserver, plugins, job-modules, extensibility]
---

# Plugin Framework

Every unit of work in the job server is a **job module plugin**: a class in the `mprov_jobserver.plugins` package that subclasses `JobServerPlugin` (`src/mprov_jobserver/plugins/plugin.py`), itself a `threading.Thread`. The [Architecture Overview](overview.md) covers the supervisor loop; this page covers the plugin contract.

## Contract

A plugin defines:

- `jobModule` — the slug the mPCC uses to queue jobs for it (e.g. `image-update`). It must match the filename with hyphens (`image_update.py` → class `image_update` → module `image-update`).
- `load_config(self) -> bool` — default implementation iterates the keys under `config_data[self.jobModule]` (populated from the plugin's YAML via `!include`, see [Architecture Overview](overview.md)) and `setattr`s each onto the instance if the attribute already exists. Unknown keys print an error but are skipped, so **class attribute defaults are the config declarations**. Return `False` to skip `handle_jobs`.
- `handle_jobs(self)` — called once per thread start. Canonical shape:

  ```python
  if not self.set_job_running():   # claim pending jobs of my module; False = nothing to do
      return
  ... do work ...
  self.set_job_success()           # or set_job_failure()
  ```

  The base class ships a simulated version (sleep 15s, mark success) — which is exactly what the stub `repo_delete.py` still runs (see [Image & Repo Provisioning](../workflows/image-provisioning.md)).
- `self.js` — back-reference to the `JobServer`, giving `js.session` (authenticated mPCC requests), `js.mprovURL`, `js.id`, `js.update_job_status()`, and `js.running`.
- `set_job_running/success/failure` — thin wrappers around the job status state machine documented in [Architecture Overview](overview.md). `set_job_failure()` also sets `self.threadOk = False`.

Plugins may override `load_config`/`set_job_*` when they claim specific job IDs rather than whole modules (dnsmasq and image-update both do).

## Discovery and supervision

- **Import**: `JobServer.load_plugins()` (`src/mprov_jobserver/app.py`) does `getattr(mprov_jobserver.plugins, mod.replace('-','_'))` and checks it's a `JobServerPlugin` subclass. Availability therefore also depends on the package `__init__` exporting the module.
- **Activation**: only modules listed under `global.jobmodules` in `jobserver.yaml` are ever started.
- **Reload-per-start**: each heartbeat, the supervisor `importlib.reload()`s the plugin module before instantiating it, so a running daemon picks up edited plugin code.
- **One thread at a time per module**; a thread is restarted on the next heartbeat after it exits. A plugin raising `SystemExit`/an unhandled exception kills the daemon process — several plugins deliberately `sys.exit(1)` on misconfiguration (e.g. image plugins requiring `mprov-webserver`).

## Cross-plugin dependencies (enforced in code)

- `image-update`, `image-sync`, `repo-sync` call `sys.exit(1)` in `load_config` unless `mprov-webserver` is in `jobmodules` — source nodes must be able to serve artifacts.
- `image-update` declares `depMods = ['repo-sync']` and blocks while any `repo-update` job is pending/running.
- `image-update` shells into the built image to run `script-runner` (see [Script Execution](../workflows/script-execution.md)).
- `nads` is runonce-only; `script-runner` is runonce-only via `-r -d`.

## Adding a new job module

1. Create `src/mprov_jobserver/plugins/<name>.py` with `class <name>(JobServerPlugin)` and set `jobModule = '<name-with-hyphens>'`; ensure the name is importable from the `mprov_jobserver.plugins` package.
2. Add `src/mprov_jobserver/plugins/<name>.yaml` with a top-level `- <name>:` block of config defaults (auto-included via `!include plugins/*.yaml`, and copied to `/etc/mprov/plugins/` on first run by `main.py`).
3. List the module under `global.jobmodules` in the deployment config.
4. Follow the `set_job_running → work → set_job_success/failure` shape; use `self.js.session` for all mPCC access rather than building new sessions.

Source map: `src/mprov_jobserver/plugins/plugin.py`, `src/mprov_jobserver/app.py` (`load_plugins`, `start`), example plugins `image_delete.py`, `libgenders.py`.
