# mProv Job Server
![Build Status](https://img.shields.io/github/actions/workflow/status/mprov-ng/mprov_jobserver/dockerimage-dev.yml?style=plastic)
![Latest Version](https://img.shields.io/pypi/v/mprov-jobserver.svg)
![Supported Python](https://img.shields.io/pypi/pyversions/mprov-jobserver.svg)
![Wheel Status](https://img.shields.io/pypi/wheel/mprov-jobserver.svg)
![License](https://img.shields.io/pypi/l/mprov-jobserver.svg)

The mProv Job server is the work horse behind the scenes of the mPCC.  The job server connects to the mPCC and will run jobs that are queued on the mPCC.  The job server is also responsible for serving images created through the mPCC, as well as running scripts on the images and hosts when they come up.

## Requirements

- A container runtime. We recommend docker-ce.
- A host the job server can serve images and repos from, with room for them
  under `/export`.
- Root, or a user that can run privileged containers: the job server manages
  services such as dnsmasq and builds image trees.

## Installation

The job server runs as a container. The image is published to GitHub's
container registry:

```
ghcr.io/mprov-ng/mprov-jobserver:latest    # the current release
ghcr.io/mprov-ng/mprov-jobserver:v0.6.0    # a specific release, to pin
ghcr.io/mprov-ng/mprov-jobserver:dev       # built from main, for testing
```

The supported way to run it is with
[mprov-compose](https://github.com/mprov-ng/mprov-compose), which brings up
the mPCC, its database and a job server together on one host:

```
git clone https://github.com/mprov-ng/mprov-compose.git
cd mprov-compose
cp stack.env.dist .env
$EDITOR .env
docker compose --env-file .env up -d
```

See that repository for the full instructions, including the database
passwords it refuses to start without and how to find the mPCC's generated
`admin` password.

To run a job server on its own — against an mPCC somewhere else — run the
image with the same environment described below. The compose file in
mprov-compose is the reference for what the container needs: host
networking, `/etc/mprov` on a volume so the configuration and API key
persist, `/export` for images and repo mirrors, `/etc/genders`, and cgroup
access.

> **Installing on bare metal is not supported.** The package is still on
> PyPI and `pip install mprov_jobserver` works, but nothing here documents
> or tests running the job server as a system service.
>
> From 0.7.0 the package needs **Python 3.11 or newer**: the container image
> has it, and a host installing with pip needs it too. Node images don't
> need any particular Python for the job server, which goes into them with
> its own (see below), so any EL8 or EL9 image works.

### How nodes get the job server

Every system image built by `image-update` gets a copy of the job server,
for `script-runner` and the `MProvScript` class that the node scripts in
[mprov_scripts](https://github.com/mprov-ng/mprov_scripts) are built on.
Since 0.7.0 it goes in as a **bundle**: a self-contained Python 3.11 with
the job server and its dependencies installed in it, unpacked at
`/usr/libexec/mprov` in the image, with `/usr/local/bin/mprov_jobserver` a link
to it. The image's own Python isn't used or changed, an image build needs
no PyPI access, and the image always gets exactly the version of the job
server that built it.

The container image carries the bundle for its own version, in
`/usr/share/mprov/`. A job server installed with pip has none built in:
download `mprov-jobserver-<version>-x86_64.tar.gz` for its version from the
[release](https://github.com/mprov-ng/mprov_jobserver/releases) and point
`image-update` at it, or image builds fail with a message saying so:

```
- image-update:
    pythonBundle: '/srv/mprov/mprov-jobserver-0.7.0-x86_64.tar.gz'
```

Images built before 0.7.0 have the job server pip-installed onto their own
Python. Rebuilding one with 0.7.0 removes that copy and puts the bundle in
its place.

### Managed files

Files uploaded to the mPCC with a "Destination in the Image" are placed into
every image they're attached to, through its OS distro, system groups or the
image itself (#96, mprov_control_center#519). This happens after the base OS
is installed and before the image-gen scripts (and Ansible) run, so those can
use them.

- **Templates and uploads:** a template is the copy the mPCC rendered for that
  image, with its Configuration Parameters. Any other file is copied as
  uploaded.
- **Owner, group and mode:** each file gets the owner and group named in the
  mPCC, resolved inside the image, and its mode. Missing parent directories
  are made root:root 0755.
- **Logging:** the log names each file and its version, never its contents.
- **What fails the build:** a file that can't be placed fails the build,
  including:
  - a destination outside the image or under `/proc`, `/sys`, `/dev`, `/run`
    or `/tmp/mprov`, or through a symlink;
  - an existing directory at the destination;
  - an owner or group the image doesn't have;
  - a template that doesn't render;
  - more than 10 MiB.
- **Setuid and setgid:** these modes are refused unless the image-update
  setting `allowSetuidFiles` is on.

What was placed is recorded in `/var/lib/mprov/placed-files.json` in the
image. At the next build, a file that's no longer attached is removed. If the
path belonged to a package before it was placed, the package is reinstalled
to bring its own file back instead (a warning, and the placed copy stays, if
that fails). A changed file reaches an image only when it's rebuilt; the mPCC
shows which images need it.

### Building images from a published base image

An OS distro on the mPCC can name a base image (`base_image_url`, with
its `base_image_checksum`). New images for that distro start from it
instead of being bootstrapped with `dnf --installroot`. The base can be:

- a container image archive (OCI layout, e.g. Rocky's `Container-Base`)
  or a root filesystem tarball;
- since 0.7.0, a cloud disk image: qcow2 (e.g. Rocky's `GenericCloud`) or
  raw, either one optionally xz compressed or the only file in a tarball
  (as Debian ships them).

The job server works out the format from the file itself, so the URL can
end in anything.

Disk images are read with libguestfs, which the container image includes.
It copies the files out from inside a small VM of its own, so the host
never mounts the image's filesystems. The first build from a disk image
also builds that VM's appliance and caches it in `/var/lib/mprov/guestfs`.
libguestfs uses `/dev/kvm` when the job server can open it. Without it,
the copy runs under emulation and takes several minutes longer.

A job server installed with pip needs `guestfs-tools` installed on its
host to use a disk image.

## Setup

Give the container its settings **in the environment** — not by editing a
configuration file. On start-up the container renders
`/etc/mprov/jobserver.yaml` from these:

| variable | what it sets |
|---|---|
| `MPROV_URL` | the mPCC's URL, e.g. `https://10.0.0.1`. Use an address or an internal name, **not** `localhost`. From 0.7.0 it must be `https://`, with a host name or address the mPCC's certificate is for: the job server authenticates with a certificate of its own (see *Enrolling with the mPCC*) |
| `MPROV_ENROLL_TOKEN` | a one-time enrollment token from the mPCC, if there isn't one in `/etc/mprov/enroll_token`. In a full mprov-compose stack the mPCC writes one there for you |
| `MPROV_CA_FINGERPRINT` | the SHA-256 fingerprint of the mPCC's CA, shown with the token, to check the mPCC by before the token is sent. Not needed with `MPROV_CA_BUNDLE`, or with the mPCC's `ca.crt` in `/etc/mprov` (mprov-compose) |
| `MPROV_TLS_DIR` | where the job server keeps its key and certificate, default `/var/lib/mprov/tls`. Keep it on storage that survives the container being recreated |
| `MPROV_APIKEY` | deprecated: the shared mPCC API key, used only by a job server with no certificate and no token. An mPCC with `MPROV_SHARED_KEY_MODE=off` refuses it, and 0.8.0 drops it |
| `MPROV_ADDRESS` | this job server's address, as the nodes reach it |
| `MPROV_MODS` | the job modules to run, space separated — see *Available job modules* |
| `MPROV_ARGS` | arguments for the job server itself (default `-c /etc/mprov/jobserver.yaml`) |
| `MPROV_CA_BUNDLE` | optional: `caBundle`, the CA certificate file for an `https` mPCC whose certificate is from a site or self-signed CA, e.g. `/etc/mprov/mpcc-ca.pem` |
| `MPROV_LOG_LEVEL` | optional: `logLevel`, one of `error`, `warning`, `info` (the default) or `debug` |
| `TZ` | the container's time zone |

With mprov-compose these live in `.env`, copied from `stack.env.dist`.

### Enrolling with the mPCC

From 0.7.0 each job server has an identity of its own instead of the API key
every agent used to share (mprov-ng/mprov_control_center#381): a key it
makes itself, which never leaves the host, and a certificate the mPCC's CA
signs for it, good for 90 days.

- **The first start** enrolls with a one-time token: `MPROV_ENROLL_TOKEN`,
  else the one the mPCC in the same compose stack writes to
  `/etc/mprov/enroll_token`. For a job server elsewhere, make a token in the
  mPCC (`manage.py mprov_enroll_token`, which prints the CA fingerprint to
  set as `MPROV_CA_FINGERPRINT` too). The token is a secret, so it is only
  sent to an mPCC whose certificate checks out: against `MPROV_CA_BUNDLE`,
  the CA with that fingerprint, or the mPCC's `ca.crt` in `/etc/mprov`.
- **The key and certificate** are kept in `MPROV_TLS_DIR`
  (`/var/lib/mprov/tls`): `jobserver.pem` (0600) and the mPCC's `ca.crt`.
  Lose them and the job server needs a new token.
- **Renewal** is automatic, with a new key, in the certificate's last 29
  days.
- **A refused certificate** (revoked in the mPCC, expired, or its job server
  deleted) is dropped, and the job server enrolls again with the next token
  it finds. In a compose stack the mPCC writes a fresh one at its next start,
  once no job server has an active certificate.
- **Without a certificate or a token** the job server falls back to
  `MPROV_APIKEY`, with a warning, and keeps trying to enroll. That fallback
  goes in 0.8.0.
- **Image builds.** A job server with its certificate never puts the API key
  in an image. The script runner in the image being built gets a 2-hour
  credential of its own, which can only read that image. It goes in the
  image's `/tmp/mprov/tls` and is removed before the image is packed. The
  build fails if the credential can't be had, or can't be removed.
- **On a booted node**, the script runner and the Python post-boot scripts
  (`MProvScript`) use the node's own certificate. The mPCC's boot scripts
  enroll the node for it as it boots, in `/etc/mprov/tls` (`nodeTlsDir`).
  They talk to the mPCC over HTTPS on the same host. An image built before
  0.7.0, which has no node certificate, still uses its key.
- **Scripts** the script runner runs get no `apikey` in `entity.json` with a
  certificate. Instead, `tls` gives the `cert`, `key` and `ca` files to use,
  e.g. with `curl --cert`, and `mprovURL` is the `https://` URL. An image-gen
  script must not copy those files into the image: they're the build's, and
  the booted node has its own.

**Don't edit `/etc/mprov/jobserver.yaml` in a container.** It is written
from a template on every start, so your changes are gone at the next
restart.

- **Per-module settings** go in the plugin files in `/etc/mprov/plugins/`,
  which are on the volume and are pulled in by the `!include` at the end of
  the generated file.

  From 0.7.0 the container brings the config it ships up to date on every
  start (#334). It installs any shipped file that's missing and replaces
  one an earlier release shipped, as long as you haven't edited it. A
  plugin file you have edited is kept, and the new default is written
  beside it as `<name>.yaml.new` (not included) for you to merge. Once your
  file matches the default again, the `.new` is removed. An edited
  `jobserver.yaml.j2` is replaced, and yours is kept as
  `jobserver.yaml.j2.bak`; use the environment or `plugins/local.yaml`
  instead. The plugin code (`*.py`) earlier images copied into
  `/etc/mprov/plugins/` is removed. Nothing else in `/etc/mprov` is
  touched.
- **Global settings with no environment variable of their own** —
  `hostId`, `logTimestamps`, `addresses`, a `heartbeatInterval` other than
  10 — go in a plugin file of their own, say
  `/etc/mprov/plugins/local.yaml`, with just the settings you want:

  ```
  - global:
      hostId: 'rack4-js01'
      logTimestamps: True
  ```

  The `global:` settings from every file are merged, so this adds to the
  generated section rather than replacing it: `mprovURL` and the rest
  still come from the environment. Where a setting is in both, the
  plugin file wins, and between plugin files the one whose name sorts last
  wins. (Before 0.7.0 a second `global:` replaced the whole section, so the
  only way was a complete config of your own passed with `MPROV_ARGS=-c`;
  that still works, but is no longer needed.)

Here is the whole `global:` section with everything it takes. The container
generates `mprovURL`, `heartbeatInterval`, `myaddress` and `jobmodules`, plus
`apikey`, `caBundle` and `logLevel` when `MPROV_APIKEY`, `MPROV_CA_BUNDLE`
and `MPROV_LOG_LEVEL` are set; `tlsDir`, `enrollToken` and `caFingerprint`
default to `MPROV_TLS_DIR`, `MPROV_ENROLL_TOKEN` and `MPROV_CA_FINGERPRINT`;
the other commented-out ones below go in a plugin file, as above:
```
- global:
    # This points to your mprov control center instance.
    # This URL should point to the internal IP address or hostname and include a trailing slash
    # e.g. "http://<IP of internal interface>/"
    
    mprovURL: "https://mprov.local.cluster"
    # where this job server keeps its key and certificate (jobserver.pem) and
    # the mPCC's CA (ca.crt).  Unset, MPROV_TLS_DIR, else /var/lib/mprov/tls.
    # tlsDir: '/var/lib/mprov/tls'
    # the one-time token to enroll with, if there's no certificate yet.
    # Unset, MPROV_ENROLL_TOKEN, else the first line of enrollTokenFile
    # (unset, enroll_token next to this file).
    # enrollToken: '...'
    # enrollTokenFile: '/etc/mprov/enroll_token'
    # the SHA-256 fingerprint of the mPCC's CA, to check it by before the
    # token is sent.  Unset, MPROV_CA_FINGERPRINT.  Not needed with caBundle,
    # or with the mPCC's ca.crt next to this file.
    # caFingerprint: '9865...df2d'
    # deprecated: the shared API key, used only with no certificate or token.
    # apikey: 'kjangknfdasjhngurwegqfdbjhn'
    # this is the interval which this jobserver will check in with the mPCC
    heartbeatInterval: 10
    # runonce: True # uncomment to run the jobserver once and exit.
    # for an https mprovURL whose certificate is from a site or self-signed CA:
    # the CA's certificate file.  Unset, the OS's CA bundle is used.
    # caBundle: '/etc/mprov/mpcc-ca.pem'
    # how much to log: error, warning, info (the default) or debug.  Each line is
    # tagged [E], [W], [I] or [D] and the module, and read with
    # `docker compose logs jobserver`.  Where stdout is journald's, the level
    # is recorded as the line's PRIORITY too, so `journalctl -p warning` works.
    # mprov-webserver logs node boots under "provisioning": a node's kernel
    # request, its image download starting, and the download done (with its
    # size and time) or failed, e.g. "[I] provisioning: c01 (10.0.0.21) got
    # image compute (1.2 GB in 41 s)".
    # logLevel: info
    # this host's id for the mPCC, so a rename keeps its registration.  Unset,
    # a random one is made and kept in host-id next to this file.
    # hostId: 'rack4-js01'
    # logTimestamps: True # stamp lines yourself; journald and docker already do.
    myaddress: 'mprov.local.cluster' # set this to the address of this jobserver.
    # this jobserver's address on each network it serves, so the mPCC sends
    # nodes on each network to the one on theirs.  Each address heartbeats on
    # its own, sent from that address; one that stops is skipped.  Unset, the
    # IPv4 and global IPv6 addresses on the interfaces that are up are used
    # (not loopback, link-local or docker/VM bridges); [] registers none.
    # network (the mPCC network id) and mprovURL (the mPCC's URL from that
    # network, if the global one isn't reachable from it) are optional.
    # addresses:
    #   - 172.16.0.5
    #   - address: 10.20.0.5
    #     network: 3
    #     mprovURL: "http://10.20.0.1/"
    jobmodules:
      # set the jobmodules you want to run here.
      - repo-delete
      - repo-update
      - image-update # REQUIRES mprov-webserver
      - mprov-webserver
      - image-delete
      - dnsmasq
      
# include any plugin yamls.        
- !include plugins/*.yaml
```

### Available job modules
These are the job modules that ship with the job server. Use the
names in the `jobmodules:` config list (and the `-m` argument):

- `dnsmasq` — serves the DNS, DHCP, and TFTP services used for provisioning (each service is toggled separately with `enableDNS`, `enableDHCP`, `enableTFTP` in the plugin config).
- `image-delete` — deletes system images from disk.
- `image-sync` — keeps a local copy of the mPCC's system images up to date in a separate image directory. Must run on image *source* nodes together with `mprov-webserver`.
- `image-update` — builds and updates system images from image-gen scripts. Requires `mprov-webserver`.
- `libgenders` — generates a genders file for use with pdsh.
- `mprov-webserver` — serves images and repo mirrors over HTTP to the systems being provisioned.
- `nads` — Node Auto Detection System; sniffs LLDP on the provisioning interface (`provIntf`) to discover the switch port a provisioning node is attached to.
- `repo-delete` — when the mPCC deletes a repo, removes its mirror from `repoDir` (by default repo-update's); run it on the repo-update node.
- `repo-sync` — keeps the mPCC's repo mirrors up to date on a local directory. Must run on repo *source* nodes together with `mprov-webserver`.
- `repo-update` — mirrors the repos defined in mPCC to disk (reposync).
- `script-runner` — runs image-gen and post-boot scripts against a system image or the local system (see *script-runner args* below).

## Running it

With mprov-compose, from that checkout:

```
docker compose up -d          # start the stack
docker compose logs -f jobserver
docker compose restart jobserver   # after changing .env
docker compose down           # stop it
```

The job server connects to the mPCC and starts a repo sync, which has to
finish before any image can be updated. Watch the log for that first run.

A job server that can't reach the mPCC, or that is on a different API
version to it, says so once and keeps retrying — it won't register until
the mPCC is there and on a matching version. See *API version lock* below.


## API version lock
The job server and the mPCC are locked to the same API version. The job
server sends its own `major.minor` to the mPCC in an `mProv-Version` header,
and refuses any reply that doesn't carry the same one — including a reply
from an mPCC too old to send the header at all. A job server and an mPCC
from different releases fail loudly instead of misbehaving subtly.

There is no support for running a mixed set of versions: the mPCC, the job
servers and mash are upgraded together, in one window. A job server pointed
at an mPCC on another version logs the mismatch and retries, and never
registers.

### MPROV_SKIP_VERSION_CHECK
Setting `MPROV_SKIP_VERSION_CHECK=1` turns the check off, for development
and CI where a work-in-progress branch of one component is run against
another. Never set it in production.

**Set it to exactly `1`.** The mPCC has an environment variable of the same
name, and the two read it slightly differently: the mPCC turns its check off
only for the exact value `1`, while the job server also accepts `true`,
`yes` and anything else that isn't empty, `0`, `false` or `no`. So
`MPROV_SKIP_VERSION_CHECK=true` switches off the job server's half of the
check and leaves the mPCC's on, which is rarely what anyone means. `1` does
the same thing on both sides.

Set it in the container's environment, e.g. in `.env` with mprov-compose,
and take it out again afterwards. The entrypoint `exec`s the job server, so
it inherits whatever the container was given.

Running the package outside a container instead? Then it reads the variable
from its own process environment, which a service manager won't pass on
from your shell — you'd have to set it for the process itself.


## Arguments
Job server takes a few command line arguments.  Global arguments are

- -r Runs the jobserver in 'runonce' mode.  The job server will run any of the job modules listed once then exit.
- -d Tells the job server not to register with the mPCC.  This is useful only when running the `script-runner` module to run post-boot or image-gen scripts.
- -c <configfile> Tells the job server to use an alternate config file.  Defaults to `/etc/mprov/jobserver.yaml`.
- -m <module1,module2,...> Overrides the jobmodules listed in the config file, running only the comma separated list given here.

Some plugins also use commandline arguments.

### script-runner args
The `script-runner` job module will take the following arguments:
- -i <systemimage-ID> This is the id of the system image you are going to run scripts against.  Mutually exclusive with -s
- -s Runs the scripts against the system the job server is running on.  Takes no argument — the job server uses the local hostname.  Mutually exclusive with -i
- -b     Runs scripts in post-boot mode.
- -r (Global Option) The script-runner must be run in `runonce` mode only.  It is probably wise to also pass -d
- -d (Global Option) The script-runner will not register as a job server with mPCC.

### Switching scripts off: disable_scripts
To leave out a script that a system would otherwise run (say its system group's `postboot1`, while you test a `postboot2` on that one node), list its slug under `disable_scripts` in the system's config parameters:

```
disable_scripts:
  - postboot1
```

It works in any layer's config parameters (OS distro, system group, image or system), and the lists from every layer are combined. A single slug can be given as a string. A slug that isn't in the run is only warned about. If a script that still runs depends on a disabled one, script-runner runs nothing and the run fails.

## Testing
A pytest suite for the job modules lives in `tests/` (dnsmasq,
libgenders, repo_update, and a source-syntax check).  CI runs it
against the built container image — see `tests/Dockerfile` for how to
run it locally the same way (`dnsmasq` and `genders` come from the
image's Rocky userland, so a bare `pytest` outside the image will fail
to import the package's system dependencies).
