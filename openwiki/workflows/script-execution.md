---
type: Reference
title: Script Execution Workflow (script-runner)
description: How the mprov_jobserver script-runner module fetches, orders, and runs user scripts against system images (image-gen) or booted hosts (post-boot), the mPCC merged-config and dependency-graph rules, nologin protection, failure exit codes, and the MProvScript SDK in script.py.
tags: [mprov, jobserver, script-runner, scripts, image-gen, post-boot, sdk]
---

# Script Execution (`script-runner`)

`src/mprov_jobserver/plugins/script_runner.py` runs user-authored scripts that the mPCC attaches to distros, system groups, and individual entities (images or hosts). It is the mechanism by which users customize images during build and configure hosts after boot. It only runs in **runonce** mode (`-r`), typically with `-d` so the process never registers as a jobserver.

## Modes and invocation

- **image-gen** (default): `mprov_jobserver -r -d -m script-runner -i <systemimage-ID>` — run scripts against an image. `image-update` invokes exactly this from inside the freshly built image chroot via `templates/image-update/script-runner.sh` (bind-mounts `/dev /proc /run /sys`, runs the jobserver with `-r -d -i $IMAGE`, unmounts after). See [Image & Repo Provisioning](image-provisioning.md).
- **post-boot**: add `-b` — run scripts against the local host after it boots a provisioned image; scripts are looked up for the host via `systems/?self`.
- The `script-runner` CLI args are documented in `README.md` (`-i`/`-s`/`-b`, plus global `-r -d`).

## What it does per run

1. **Resolve the entity**: the image (`systemimages/<id>/`) or the local system (`systems/?self`); on lookup failure it falls back to the public `nads` systemimage entity, and blanks the API key in the entity file for `nads` so public boot media don't carry credentials.
2. **Collect scripts and config**: `mpccMerge()` prefers the merged `config` the mPCC itself serves on the entity (mPCC #269): `config['config_params']` merged by top-level key, and `config['scripts']` as an ordered list of slugs merged distro → system groups (a system's own plus its image's) → image → system. Each slug is looked up in the entity's layers — including the system's image and the image's groups — for its file, type and `dependsOn` (objects or bare slugs; both accepted). A slug in `config` that isn't in any layer is reported and fails the run. Against an mPCC from before #269 the fallback `layerMerge()` runs the old local merge (distro → groups → entity only — it skips the image layer, so pre-#269 systems missed their image's post-boot scripts). Either path feeds `depResolve()`, a topological sort that reports cyclic/unresolvable dependencies.
3. **Lock the console**: after all argument and entity validation passes, creates `/etc/nologin` so users can't interfere with scripting. A bad invocation (`-h`, `-i` with `-s`, failed entity load) never creates it, and the script region is wrapped in `try/finally` so the file is lifted however the run exits (#173).
4. **Expose context**: writes `entity.json` (entity data + merged config params + `mprovURL` + `apikey` + `jobserverId`) into `scriptTmpDir` (default `/tmp/mprov`, `script-runner.yaml`).
5. **Execute** each dependency "step": scripts are downloaded from the mPCC, chmod 0755, and run via `subprocess.run(shell=True)`; scripts within a step run as parallel threads. `entity.json` is removed afterwards.
6. **Report failure**: every script's subprocess return code is recorded in a shared dict (a script thread that dies before its subprocess starts records an error string instead). After joining each step's threads, any nonzero result sets the plugin's `failed` flag (defined on the [`JobServerPlugin` base](../architecture/plugin-framework.md)), calls `set_job_failure()` on the mPCC, and in runonce mode makes `JobServer.start()` exit 1 — so image-build and post-boot pipelines fail loudly instead of exiting 0 (#174).

Caveat: in the `layerMerge()` fallback, group-level `dependsOn` entries are appended raw while distro/entity entries use `dep['slug']` — keep dependency declarations consistent when authoring scripts for older mPCCs. Entity-load errors still `exit(1)` the process.

## Writing scripts: the `MProvScript` SDK

`src/mprov_jobserver/script.py` provides the `MProvScript` base class users can subclass in their scripts (they run inside the image chroot or on the booted host, where `mprov_jobserver` is installed — `image-update` pip-upgrades it inside the image before scripts run). It:

- loads `/etc/mprov/script-runner.yaml`, maps `global:` keys onto class attributes, and disables `!include` handling,
- starts an API-key `requests.Session` against the mPCC and records the host's local `ip_address`,
- expects subclasses to override `run()`.

Scripts can therefore call back into mPCC endpoints using the same session/auth pattern as the [Plugin Framework](../architecture/plugin-framework.md) plugins.

## Where to start when changing this area

- Dependency ordering bugs → `depResolve()` in `script_runner.py`; script-set/config-set differences between mPCC versions → `mpccMerge()` vs `layerMerge()`; test with `-r -d -m script-runner -i <slug> -c <config>`.
- The chroot handoff between image builds and scripts lives in `src/mprov_jobserver/templates/image-update/` (`script-runner.sh`, `script-runner.yaml.j2`).
- Post-boot runs execute as root on production nodes; changes to the `/etc/nologin` region and the `scriptResults`/`failed` failure-propagation path (#173, #174) deserve extra scrutiny — `tests/test_script_runner.py` and `tests/test_app.py` cover both.

Source map: `src/mprov_jobserver/plugins/script_runner.py`, `plugins/script-runner.yaml`, `script.py`, `templates/image-update/script-runner.sh`, `templates/image-update/script-runner.yaml.j2`.
