# mProv Job Server
![Build Status](https://img.shields.io/github/actions/workflow/status/mprov-ng/mprov_jobserver/dockerimage-dev.yml?style=plastic)
![Latest Version](https://img.shields.io/pypi/v/mprov-jobserver.svg)
![Supported Python](https://img.shields.io/pypi/pyversions/mprov-jobserver.svg)
![Wheel Status](https://img.shields.io/pypi/wheel/mprov-jobserver.svg)
![License](https://img.shields.io/pypi/l/mprov-jobserver.svg)

The mProv Job server is the work horse behind the scenes of the mPCC.  The job server connects to the mPCC and will run jobs that are queued on the mPCC.  The job server is also responsible for serving images created through the mPCC, as well as running scripts on the images and hosts when they come up.

## Requirements

- MUST BE INSTALLED AS ROOT 
- The mProv Job server requires python 3.8 and above.  

## Installation
The best way to install the job server is to run:
```
wget https://raw.githubusercontent.com/mprov-ng/mprov_jobserver/main/install_mprov_jobserver.sh -O - | bash
```

This will download and run the jobserver installation script from this repo.


## Setup
You will need to create an API key in the mPCC for the job server.  Once you have the api key, you will want to add that key to the `/etc/mprov/jobserver.yaml` file.  There should be an example in there already, it will not work.  You must replace it.  While you are in there, you will want to setup the `mprovURL` entry to point to your mPCC instance.  After that, you can enable/disable whatever jobmodules you want this job server to run by uncomment/commenting the lines that describe the jobmodules.

do not use localhost use IP or internal name 

Here is an example for a good first jobserver config:
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
    # tagged [E], [W], [I] or [D] and the module; under systemd, journald also
    # gets the level, so `journalctl -p warning -u mprov_jobserver` works.
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

## Post Setup
After you have setup the `/etc/mprov/jobserver.yaml` you can enable the jobserver with this command:
```
# systemctl enable --now mprov_jobserver
```
The jobserver should connect to the mPCC and start a repo sync which is required before you can update any images.


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
