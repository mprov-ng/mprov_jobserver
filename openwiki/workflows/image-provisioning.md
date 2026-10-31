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
             --download-metadata [--newest-only] --installroot=<repoDir>/<id>
```

With `newestOnly` the mirror fetches only the newest version of each package instead of the whole history (#95). A repo's own `newest_only` flag in the mPCC wins over the plugin's `newestOnly` default when set; versions already mirrored earlier stay on disk, since `reposync` deletes nothing without `--delete`.

The `--installroot` trick sidesteps dnf's download lock so repos sync in parallel, and `--destdir` is an absolute path so the plugin never `os.chdir()`s — plugins are threads of one process, and a global chdir would move every other plugin's working directory (#130). On success it PATCHes `repos/<id>/` with only `id` and `hosted_by=[js.id]` — and **fails the job** if the mPCC refuses that PATCH, because a mirrored repo with no host serves nothing (#274). The old `update: False` key that cleared the repo's re-sync flag is gone, because mPCC mprov-ng/mpcc#358 renamed that field to `resync` and clears it when the repo-update job is queued (#204). Upgrade the mPCC first or together: against an older mPCC nothing clears the flag after a mirror, so a later save of the repo queues another sync. Caveat: the mprov-webserver requirement check present in image plugins is commented out here.

`repo-delete` is now implemented (#161): the mPCC queues one paramless "Delete Repos" job whenever a repo is deleted, so on each claimed job the plugin lists `repoDir` (defaulting to `repo-update`'s `repoDir` unless its own is set) and `rmtree`s the mirror directories named purely by a repo id that the mPCC no longer has — anything else in `repoDir` (repo-versions, an operator's files, symlinks) is left alone, since the delete runs as root.

## Images: `image-update` and `image_update_mod/rhel`

`image_update.py` is the orchestrator. On start it hard-requires `mprov-webserver` (source nodes must serve their builds), and it blocks while any `repo-update` job is pending/running because image builds consume the mirrored repos. Per claimed job:

1. Resolve the image's distro/baserepo/extrarepos from the mPCC and dispatch on `osdistro.baserepo.ostype.slug` to `image_update_mod/<ostype>.py`.
2. Post-build steps: copy `resolv.conf`, upgrade `mprov_jobserver` inside the image, inject distro root SSH keys and password — the keys land in the **image tree** (`<imgDir>/root/.ssh/authorized_keys`, 0700/0600), never on the build host (#175), and the password reaches `chroot <imgDir> chpasswd` only over the subprocess's stdin pipe (chpasswd is non-interactive, and the job fails if the password doesn't take, #229), redacted in debug prints, never placed in a shell command line or argv where `/proc` would expose it (#168) — run image-gen scripts by rendering `templates/image-update/script-runner.sh` and chrooting a runonce jobserver (see [Script Execution](../workflows/script-execution.md)); the wrapper now preserves the chrooted jobserver's exit code, so a failed image-gen script fails the job and no image is packed (#221). The runonce config the build writes under the image (`<imgDir>/tmp/mprov/jobserver.yaml`) holds the fleet API key, so its directory is 0700, owned by the build uid (refused otherwise), and the file is created `O_NOFOLLOW` at 0600 and unlinked after the scripts run (#169). Then fetch a static busybox into a 0700 root-only `<imageDir>/.busybox` and refuse to run it unless the download starts with the ELF magic — a fixed world-writable `/tmp` path let another user plant a binary for the root daemon (#170) — call the submodule's `updateBootFiles()` if present (its failure fails the job and skips packing, #144), then pack the rootfs: `busybox tar | gzip -1` into a temp file `os.replace`d over `<slug>.img` only once tar and gzip both succeed, so a failed pack can never destroy the previous image (#216, #232).
3. On success only, PATCH `systemimages/<slug>/?addjs` with `needs_rebuild: False` and `jobservers: [js.id]`, and bump the local `image-versions` JSON tracker in `imageDir`.

Every error path in the per-image loop fails **only that job** — `update_job_status(..., status=3, jobid=jid)` — and `continue`s to the next image (#143, #144, #220); a module import failure, a chroot pip failure, a failed image-gen script, a bad busybox download, or a failed pack never stop the other images' builds. Jobs are claimed per configured image via `jobs/?module=image-update&params=<{"imageId":...}>` — the mPCC matches `params` key by key, and pending jobs have no jobserver yet so the claim can't filter on it (#172, #144).

`image_update_mod/rhel.py` (class `UpdateImage`) builds the RHEL-family rootfs — **a directory chroot, not kickstart**:

- **Base image path (#92)**: if the mPCC's distro has a `base_image_url`, `plugins/base_image.py` downloads it (SHA256-verified against `base_image_checksum`, BSD/GNU checksum formats or a bare digest; OCI/registry or direct-archive URLs), unpacks it into the fresh image dir, unmasks the systemd units container base images mask, and records an `/etc/mprov-base-image` marker so a changed base is noticed next build. `strip_base_image()` then removes what a cloud base must not carry into a bare-metal image (cloud-init, the base's own kernel), and the distro packages install on top. Without `base_image_url` the bootstrap below runs as before.
- Writes `mprov`-managed `.repo` files pointing at `{mprovURL}/osrepos/<id>/` (gpgcheck=0), disables stock repos (baseos/appstream/epel/extras/powertools/crb). Before any chroot `dnf` runs — including the first package install — the image gets a copy of the host's `/etc/resolv.conf`, or glibc resolves repo hostnames against `127.0.0.1` and the first build fails on a host whose resolver is loopback-only (#281).
- `dnf --installroot=<imgDir> groupinstall 'Minimal Install'` with a fallback that retries via `chroot imgDir dnf ...`; installs a fixed package set (kernel, grub2 + EFI modules, mdadm, ipmitool, python stack — version-split at RHEL 9) and freezes the kernel with `dnf versionlock kernel*`. Retries once after `rpm --rebuilddb` on failure.
- Symlinks newest non-rescue kernel/initramfs out as `<slug>.vmlinuz` / `<slug>.initramfs`, `mknod`s `/dev/ram0`+`/dev/initrd`, and regenerates initramfs with `dracut --regenerate-all -f --mdadmconf --force-add mdraid --add-drivers sd_mod ...` (repeated in `updateBootFiles()` after scripts run, so the final initramfs includes script changes). The distro's `initial_mods` list is first filtered by `realModules()`, which keeps only names that `modinfo -k` confirms as modules of every kernel in the image: dracut-install stops at a built-in or unknown module and silently drops every driver listed after it (#206).
- No subscription-manager flow exists; everything is `--nogpgcheck` plus mProv-generated repo files.
- When a global `caBundle` is configured, `mpcc_ca()` copies the mPCC's CA into the image twice — at the same absolute path (the host's dnf reads `sslcacert` from the image's repo files as a *host* path during the install passes) and into `/etc/pki/ca-trust/source/anchors/mprov-mpcc.pem` — then runs `chroot <img> update-ca-trust` before packing, so the chroot's dnf and the booted nodes trust a site/self-signed mPCC too (#267).

Caveats: heavy `os.system` string interpolation and chroot of the half-built image are by design (root daemon, trusted mPCC input).

## Serving: `mprov-webserver`

`mprov_webserver.py` is a `ThreadingHTTPServer` (default port 8080, `rootDir: /export/`) that serves repos at `/osrepos/...` and images at `/images/...` for PXE downloads and peer pulls. Three guards protect it:

- **Root safety (#214)**: `safe_root()` refuses to start (or to accept a request) when `rootDir` is empty, `/`, or not a directory — an empty `rootDir` means the working directory, which is `/` under systemd, so the whole filesystem would be exposed. The server passes its root into each handler so the fallback never applies.
- **Load shedding**: `/images/*` GETs are refused with 404 when the **5-minute** load average (`os.getloadavg()[1]`) ≥ CPU count and `loadmon` is enabled, reading load and the job server handle from the server object, not the handler (#147). The 404 makes clients retry another jobserver — the mPCC HEADs each server before redirecting and skips one that refuses — while the daemon keeps heartbeating so a busy server is never deleted from the mPCC (#148, see [Architecture Overview](../architecture/overview.md)).
- **Connection limit (#148, #185)**: the `maxConn` (10) counter applies only to *image* GETs whose file is ≥ `maxConnFileSize` (default 0 = every image file); the path is classified via `translate_path` so `//images/` and `/x/../images/` count too, the file is stat'd without holding a descriptor, and non-image or missing files go straight to `SimpleHTTPRequestHandler`. HEAD never takes a slot (it's the mPCC's per-redirect probe), and slots are released in a `finally` so aborted downloads don't leak.

Caveat: `maxConn` remains a hardcoded class attribute (TODO in source); it is now live code on the image path, not dead.

**Provisioning metric (#105)**: every image GET or refusal logs one line to a separate `provisioning` logger — `node (ip) requested/turned away from image <slug>` — so a time-to-provision metric can be built from the boot-file requests the webserver already sees.

## Propagation & teardown: `image-sync`, `repo-sync`, `image-delete`

- `image-sync` (requires `mprov-webserver`) runs every heartbeat, not job-driven: compares each image's mPCC `version` against a local `image-versions` JSON file, downloads `.img`/`.initramfs`/`.vmlinuz` from a peer when they differ, registers itself with `?addjs`, and sweeps directories under `imageDir` not in the configured list. Its `?addjs` PATCH carries only `slug` + `jobservers` — it deliberately omits `needs_rebuild`, because sending `False` cleared a rebuild the operator asked for since the plugin's GET (#226). `repo-sync` mirrors the same pattern with `wget --mirror` into `repoDir` (note: no `repo-sync.yaml` ships, so it runs on empty defaults unless configured inline).
- `image-update`'s heartbeat pre-pass (`updateImageJobservers`) participates in the same pull pattern: for configured images it doesn't yet host and whose version differs, it pulls from a peer **through the mPCC redirect** via `image_update.py::pullImage` — each artifact downloads to a dotfile temp and is `os.replace`d into place only once all three succeed, so a failed pull (e.g. every host full and answering 404) keeps the previous image; on success it records the version and adds itself as host (#142). It skips images with `customIPXE` set or `needs_rebuild` requested.
- **Sweep safety (`dataRoot` + `.mprov-managed` marker, #171)**: because the sweep `rmtree`s unlisted directories, both sync plugins refuse to load (`sys.exit(1)` in `load_config`) unless the configured `imageDir`/`repoDir` sits inside a declared `dataRoot` (helpers `path_is_under`/`mark_mprov_dir`/`is_mprov_marked_dir` in `plugins/plugin.py`, realpath-checked so symlinks or `..` can't escape). Each directory a sync plugin creates gets a `.mprov-managed` marker file, and the sweep deletes only marked directories — unowned directories are logged and left alone, so a mistyped sync directory can never wipe operator data.
- `image-delete` (config `image-delete.yaml`) is job-driven: claims `image-delete` jobs scoped to this server and deletes `imageDir/<imageId>` directories it hosts. It shares the `imageDir`/`image-versions` convention with image-update but does not prune the versions file.

The mPCC `jobservers`/`hosted_by` lists plus the `?addjs` PATCH convention are how the control plane knows which server can serve which artifact — all image/repo modules participate in this **self-registration dance**. An `?addjs` PATCH carries only our own id (never a list read from a possibly-stale GET, which could drop other hosts, #248) and, when the mPCC advertises the `addjs-version` feature, our local image `version` — a 409 means the image has moved past us, so the plugin fetches the mPCC's fresh version onto the image instead of silently claiming hosting (#248, part 2; `addjs_version()` in `plugins/plugin.py`).

## Ad-hoc tooling

`repack_image` (repo root) re-packs an unpacked image directory with the same busybox-tar pipeline for manual fixes. It parks the existing `.img` outside the tarred directory and swaps the new one in with a same-filesystem `mv` only after tar+gzip succeed, restoring the parked image on any failure — a failed repack can never destroy the image it repacks (busybox tar has no `--exclude`).

Source map: `src/mprov_jobserver/plugins/image_update.py`, `image_update_mod/rhel.py`, `base_image.py`, `repo_update.py`, `repo_update_mod/rhel.py`, `image_sync.py`, `repo_sync.py`, `image_delete.py`, `repo_delete.py`, `mprov_webserver.py`, `templates/image-update/`, `repack_image`.
