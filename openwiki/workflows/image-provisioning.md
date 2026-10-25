---
type: Reference
title: Image and Repo Provisioning Workflow
description: How mprov_jobserver mirrors package repos, builds RHEL-family bare-metal images in chroots, syncs artifacts between jobservers, serves them over HTTP, and deletes images/repos.
tags: [mprov, jobserver, image-build, reposync, dnf, chroot, pxe, webserver]
---

# Image & Repo Provisioning

This is mProv's core pipeline: mirror upstream repos → build a bootable image rootfs from them → pack and serve the image + kernel + initramfs for PXE boot → propagate to other jobservers. All modules here plug in through the [Plugin Framework](../architecture/plugin-framework.md).

## Flow summary

```
upstream repos ──repo-update (dnf reposync)──▶ /export/repos ──served by mprov-webserver──▶ /osrepos/<id>/
                                                     │
                              image-update (dnf --installroot + dracut in chroot)
                                                     ▼
        /export/images/<slug>/ (.img tarball, <slug>.vmlinuz, <slug>.initramfs)
                                                     │  served by mprov-webserver at /images/
                            image-sync / repo-sync pull from peers on version mismatch
```

## Repos: `repo-update` and `repo_update_mod/rhel`

`repo_update.py` claims pending `repo-update` jobs, fetches each `repos/<id>/` from the mPCC, and dispatches on the repo's `ostype.slug` to a submodule found by globbing `repo_update_mod/*.py`. `repo_update_mod/rhel.py` (class `UpdateRepo`) registers the `rhel` ostype with the mPCC (`POST /ostypes/`) on config load, then mirrors with:

```
dnf reposync --repofrompath <id>,"<url>" --repo <id> --destdir=<repoDir>/ \
             --download-metadata --installroot=<repoDir>/<id>
```

The `--installroot` trick sidesteps dnf's download lock so repos sync in parallel, and `--destdir` is an absolute path so the plugin never `os.chdir()`s — plugins are threads of one process, and a global chdir would move every other plugin's working directory (#130). On success it PATCHes `repos/<id>/` with `hosted_by=[js.id]` and `update: False`. Caveat: the mprov-webserver requirement check present in image plugins is commented out here.

`repo-delete` is a stub — its class body is `pass`, so it inherits the base simulated `handle_jobs()` (sleep 15s, mark success).

## Images: `image-update` and `image_update_mod/rhel`

`image_update.py` is the orchestrator. On start it hard-requires `mprov-webserver` (source nodes must serve their builds), and it blocks while any `repo-update` job is pending/running because image builds consume the mirrored repos. Per claimed job:

1. Resolve the image's distro/baserepo/extrarepos from the mPCC and dispatch on `osdistro.baserepo.ostype.slug` to `image_update_mod/<ostype>.py`.
2. Post-build steps: copy `resolv.conf`, upgrade `mprov_jobserver` inside the image, inject distro root SSH keys and password — the password reaches `chroot <imgDir> passwd root` only over the subprocess's stdin pipe and is redacted in debug prints, never placed in a shell command line or argv where `/proc` would expose it (#168) — run image-gen scripts by rendering `templates/image-update/script-runner.sh` and chrooting a runonce jobserver (see [Script Execution](../workflows/script-execution.md)), fetch a static busybox, call the submodule's `updateBootFiles()` if present, then pack the rootfs: `busybox tar | gzip -1` → `<slug>.img`.
3. PATCH `systemimages/<slug>/?addjs` to register this server as a host and bump the local `image-versions` JSON tracker in `imageDir`.

`image_update_mod/rhel.py` (class `UpdateImage`) builds the RHEL-family rootfs — **a directory chroot, not kickstart**:

- Writes `mprov`-managed `.repo` files pointing at `{mprovURL}/osrepos/<id>/` (gpgcheck=0), disables stock repos (baseos/appstream/epel/extras/powertools/crb).
- `dnf --installroot=<imgDir> groupinstall 'Minimal Install'` with a fallback that retries via `chroot imgDir dnf ...`; installs a fixed package set (kernel, grub2 + EFI modules, mdadm, ipmitool, python stack — version-split at RHEL 9) and freezes the kernel with `dnf versionlock kernel*`. Retries once after `rpm --rebuilddb` on failure.
- Symlinks newest non-rescue kernel/initramfs out as `<slug>.vmlinuz` / `<slug>.initramfs`, `mknod`s `/dev/ram0`+`/dev/initrd`, and regenerates initramfs with `dracut --regenerate-all -f --mdadmconf --force-add mdraid --add-drivers sd_mod ...` (repeated in `updateBootFiles()` after scripts run, so the final initramfs includes script changes).
- No subscription-manager flow exists; everything is `--nogpgcheck` plus mProv-generated repo files.

Caveats: heavy `os.system` string interpolation and chroot of the half-built image are by design (root daemon, trusted mPCC input).

## Serving: `mprov-webserver`

`mprov_webserver.py` is a `ThreadingHTTPServer` (default port 8080, `rootDir: /export/`) that serves repos at `/osrepos/...` and images at `/images/...` for PXE downloads and peer pulls. Load shedding: `/images/*` GETs are refused with 404 when the 1-minute load ≥ CPU count and `loadmon` is enabled, so clients retry another jobserver. Caveat: `maxConn` counters are hardcoded class attributes and part of `checkFileSize()` is dead code.

## Propagation & teardown: `image-sync`, `repo-sync`, `image-delete`

- `image-sync` (requires `mprov-webserver`) runs every heartbeat, not job-driven: compares each image's mPCC `version` against a local `image-versions` JSON file, downloads `.img`/`.initramfs`/`.vmlinuz` from a peer when they differ, registers itself with `?addjs`, and sweeps directories under `imageDir` not in the configured list. `repo-sync` mirrors the same pattern with `wget --mirror` into `repoDir` (note: no `repo-sync.yaml` ships, so it runs on empty defaults unless configured inline).
- **Sweep safety (`dataRoot` + `.mprov-managed` marker, #171)**: because the sweep `rmtree`s unlisted directories, both sync plugins refuse to load (`sys.exit(1)` in `load_config`) unless the configured `imageDir`/`repoDir` sits inside a declared `dataRoot` (helpers `path_is_under`/`mark_mprov_dir`/`is_mprov_marked_dir` in `plugins/plugin.py`, realpath-checked so symlinks or `..` can't escape). Each directory a sync plugin creates gets a `.mprov-managed` marker file, and the sweep deletes only marked directories — unowned directories are logged and left alone, so a mistyped sync directory can never wipe operator data.
- `image-delete` (config `image-delete.yaml`) is job-driven: claims `image-delete` jobs scoped to this server and deletes `imageDir/<imageId>` directories it hosts. It shares the `imageDir`/`image-versions` convention with image-update but does not prune the versions file.

The mPCC `jobservers`/`hosted_by` lists plus the `?addjs` PATCH convention are how the control plane knows which server can serve which artifact — all image/repo modules participate in this **self-registration dance**.

## Ad-hoc tooling

`repack_image` (repo root) re-packs an unpacked image directory with the same busybox-tar pipeline for manual fixes.

Source map: `src/mprov_jobserver/plugins/image_update.py`, `image_update_mod/rhel.py`, `repo_update.py`, `repo_update_mod/rhel.py`, `image_sync.py`, `repo_sync.py`, `image_delete.py`, `repo_delete.py`, `mprov_webserver.py`, `templates/image-update/`, `repack_image`.
