---
type: Reference
title: Script Execution Workflow (script-runner)
description: How the mprov_jobserver script-runner module fetches, orders, and runs user scripts against system images (image-gen) or booted hosts (post-boot), the dependency graph and config merging rules, and the MProvScript SDK in script.py.
tags: [mprov, jobserver, script-runner, scripts, image-gen, post-boot, sdk]
---

# Script Execution (`script-runner`)

`src/mprov_jobserver/plugins/script_runner.py` runs user-authored scripts that the mPCC attaches to distros, system groups, and individual entities (images or hosts). It is the mechanism by which users customize images during build and configure hosts after boot. It only runs in **runonce** mode (`-r`), typically with `-d` so the process never registers as a jobserver.

## Modes and invocation

- **image-gen** (default): `mprov_jobserver -r -d -m script-runner -i <systemimage-ID>` — run scripts against an image. `image-update` invokes exactly this from inside the freshly built image chroot via `templates/image-update/script-runner.sh` (bind-mounts `/dev /proc /run /sys`, runs the jobserver with `-r -d -i $IMAGE`, unmounts after). See [Image & Repo Provisioning](image-provisioning.md).
- **post-boot**: add `-b` — run scripts against the local host after it boots a provisioned image; scripts are looked up for the host via `systems/?self`.
- The `script-runner` CLI args are documented in `README.md` (`-i`/`-s`/`-b`, plus global `-r -d`).

## What it does per run

1. **Lock the console**: creates `/etc/nologin` at start and removes it at the end (f0d33a6) so users can't interfere with post-boot scripting.
2. **Resolve the entity**: the image (`systemimages/<id>/`) or the local system (`systems/?self`); on lookup failure it falls back to the public `nads` systemimage entity, and blanks the API key in the entity file for `nads` so public boot media don't carry credentials.
3. **Collect scripts** from three scopes — distro, systemgroups, entity — and merge them into a dependency graph. Each script declares `dependsOn`; `depResolve()` performs a topological sort and reports cyclic/unresolvable dependencies.
4. **Merge config params**: `config_params` YAML blocks from distro → groups → entity are flattened in that precedence order (entity wins).
5. **Expose context**: writes `entity.json` (entity data + merged config params + `mprovURL` + `apikey` + `jobserverId`) into `scriptTmpDir` (default `/tmp/mprov`, `script-runner.yaml`).
6. **Execute** each dependency "step": scripts are downloaded from the mPCC, chmod 0755, and run via `subprocess.run(shell=True)`; scripts within a step run as parallel threads. `entity.json` is removed afterwards.

Caveat: group-level `dependsOn` entries are appended raw while distro/entity entries use `dep['slug']` — keep dependency declarations consistent when authoring scripts. Errors generally `exit(1)` the process.

## Writing scripts: the `MProvScript` SDK

`src/mprov_jobserver/script.py` provides the `MProvScript` base class users can subclass in their scripts (they run inside the image chroot or on the booted host, where `mprov_jobserver` is installed — `image-update` pip-upgrades it inside the image before scripts run). It:

- loads `/etc/mprov/script-runner.yaml`, maps `global:` keys onto class attributes, and disables `!include` handling,
- starts an API-key `requests.Session` against the mPCC and records the host's local `ip_address`,
- expects subclasses to override `run()`.

Scripts can therefore call back into mPCC endpoints using the same session/auth pattern as the [Plugin Framework](../architecture/plugin-framework.md) plugins.

## Where to start when changing this area

- Dependency ordering bugs → `depResolve()` in `script_runner.py`; test with `-r -d -m script-runner -i <slug> -c <config>`.
- The chroot handoff between image builds and scripts lives in `src/mprov_jobserver/templates/image-update/` (`script-runner.sh`, `script-runner.yaml.j2`).
- Post-boot runs execute as root on production nodes; script failure semantics (exit code handling, nologin cleanup) deserve extra scrutiny.

Source map: `src/mprov_jobserver/plugins/script_runner.py`, `plugins/script-runner.yaml`, `script.py`, `templates/image-update/script-runner.sh`, `templates/image-update/script-runner.yaml.j2`.
