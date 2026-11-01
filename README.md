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

### The PyPI package is still used — by nodes, not by job servers

Don't mistake "the job server runs in a container" for "the package is
obsolete". Every system image built by `image-update` installs
`mprov_jobserver` from PyPI inside the image:

```
chroot <image> pip3 --no-cache-dir install --upgrade mprov_jobserver
```

That is how a provisioned node gets `script-runner` and the `MProvScript`
class that the node scripts in
[mprov_scripts](https://github.com/mprov-ng/mprov_scripts) are built on.
Nodes are not containers, so the package stays published and supported for
that use.

## Setup

Create an API key in the mPCC for the job server, then give the container
its settings **in the environment** — not by editing a configuration file.
On start-up the container renders `/etc/mprov/jobserver.yaml` from these:

| variable | what it sets |
|---|---|
| `MPROV_URL` | the mPCC's URL, e.g. `http://10.0.0.1`. Use an address or an internal name, **not** `localhost` |
| `MPROV_APIKEY` | the mPCC API key. In a full mprov-compose stack this is generated and filled in for you |
| `MPROV_ADDRESS` | this job server's address, as the nodes reach it |
| `MPROV_MODS` | the job modules to run, space separated — see *Available job modules* |
| `MPROV_ARGS` | arguments for the job server itself (default `-c /etc/mprov/jobserver.yaml`) |
| `TZ` | the container's time zone |

With mprov-compose these live in `.env`, copied from `stack.env.dist`.

**Don't edit `/etc/mprov/jobserver.yaml` in a container.** It is written
from a template on every start, so your changes are gone at the next
restart.

- **Per-module settings** go in the plugin files in `/etc/mprov/plugins/`,
  which are on the volume, are not regenerated, and are pulled in by the
  `!include` at the end of the generated file.
- **Global settings with no environment variable of their own** —
  `caBundle`, `hostId`, `logLevel`, `logTimestamps`, `addresses` — have
  nowhere to go in the generated file. Put them in a file of your own on the
  volume, say `/etc/mprov/jobserver-local.yaml`, and point the job server at
  it instead:

  ```
  MPROV_ARGS=-c /etc/mprov/jobserver-local.yaml
  ```

  That file is yours, so it must carry the whole `global:` section,
  including `mprovURL`, `apikey`, `myaddress` and `jobmodules` — the
  environment variables above no longer apply once you stop using the
  generated file. Copy the generated one as a starting point.

Here is the whole `global:` section with everything it takes. The container
generates only the first few — `mprovURL`, `apikey`, `heartbeatInterval`,
`myaddress` and `jobmodules` — so the commented-out ones below are the ones
that need a config file of your own, as above:
```
- global:
    # This points to your mprov control center instance.
    # This URL should point to the internal IP address or hostname and include a trailing slash
    # e.g. "http://<IP of internal interface>/"
    
    mprovURL: "http://mprov.local.cluster"
    # this is the api key for your mprov control center so that the 
    # jobserver can login and do stuff.
    apikey: 'kjangknfdasjhngurwegqfdbjhn'
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
