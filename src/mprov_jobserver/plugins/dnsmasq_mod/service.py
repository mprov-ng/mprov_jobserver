import os
import psutil
from mprov_jobserver.log import get_logger

log = get_logger(__name__)


# What dnsmasq makes of each hosts directory the DNS and DHCP workers manage,
# for the README they keep in it.
HOSTS_DIRS = {
    'dns': ('-dns.conf', 'DNS', 'a hosts file', 'hostsdir'),
    'dhcp': ('-dhcp.conf', 'DHCP', 'dhcp-host entries', 'dhcp-hostsdir'),
}


def hosts_dir_readme(kind):
    '''
    The .README the DNS or DHCP worker keeps in its hosts directory, saying
    which files it prunes.  It's a dotfile because dnsmasq loads every other
    file in that directory, and would read a README as hosts.
    '''
    suffix, service, contents, option = HOSTS_DIRS[kind]
    return f"""\
This directory is managed by mprov_jobserver's dnsmasq plugin.

dnsmasq loads every file in this directory as {contents} (it is
dnsmasq's {option}), except files whose names start with a dot, like
this one.

mprov_jobserver writes one file here for each mPCC network with {service}
managed, named "<network-slug>{suffix}", and rewrites it whenever that
network's {service} config changes.  Every file whose name ends in
"{suffix}" is treated as one of these: when its slug no longer matches a
network the mPCC lists with {service} managed (the network was deleted or
renamed, or {service} was turned off for it), mprov_jobserver deletes the
file automatically.  Each removal is logged at INFO level.

Don't put files of your own named "*{suffix}" in this directory: they will
be deleted.  Files with any other name are left alone, and dnsmasq still
loads them.

mprov_jobserver rewrites this file; changes made to it will be lost.
"""


ROOT_README = """\
The dns/ and dhcp/ directories here are managed by mprov_jobserver's
dnsmasq plugin.  They hold the host files it generates from the mPCC:
dns/ is dnsmasq's hostsdir and dhcp/ is its dhcp-hostsdir, so dnsmasq
loads every file in them.

mprov_jobserver deletes files in those directories automatically once the
mPCC network they were written for is gone, and that includes files put
there by hand if they're named the same way:

  dns/*-dns.conf    deleted once the network is no longer DNS-managed
  dhcp/*-dhcp.conf  deleted once the network no longer has DHCP

Each directory has a .README (a dotfile, so dnsmasq doesn't load it) with
the details.  Other files and directories here belong to other parts of
mprov_jobserver and aren't covered by this note.

mprov_jobserver rewrites this file; changes made to it will be lost.
"""


def write_if_changed(path, text):
    '''
    Write text to path, unless the file already holds exactly that.  True if
    it changed.  The text goes to ".<name>.tmp" in the same directory and is
    renamed over the file, so dnsmasq, which watches its hosts directories,
    never reads a half-written or truncated file (#210).  dnsmasq's watch
    ignores dotfiles, so it doesn't read the temporary file either.
    '''
    try:
        with open(path) as current:
            if current.read() == text:
                return False
    except FileNotFoundError:
        pass
    directory, name = os.path.split(path)
    tmp = os.path.join(directory, '.' + name + '.tmp')
    with open(tmp, 'w') as conf:
        conf.write(text)
    os.replace(tmp, path)
    return True


def remove_stale(directory, suffix, keep):
    '''
    Remove the hosts files in directory that this plugin writes (named
    "<network-slug><suffix>") but didn't write this pass: the network was
    deleted, renamed or no longer has DNS/DHCP managed.  dnsmasq reads every
    file in its hosts directories, so a leftover kept answering (#244).
    Other files, an operator's included, are left alone.  True if any was
    removed.
    '''
    removed = False
    for name in os.listdir(directory):
        if name.startswith('.') or not name.endswith(suffix) or name in keep:
            continue
        try:
            os.unlink(os.path.join(directory, name))
        except FileNotFoundError:
            continue
        log.info(f"Removed {os.path.join(directory, name)}: its network is no longer served.")
        removed = True
    return removed


def running_dnsmasq(binary, pidfile):
    '''
    The dnsmasq we run, as a psutil.Process, or None if it isn't running.
    Found by its pid file and checked to be that binary, instead of killing
    the first process with "dnsmasq" anywhere in its name (#181): that could
    be any other program, or another dnsmasq (e.g. libvirt's, which uses the
    same binary with its own pid file).
    '''
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    try:
        proc = psutil.Process(pid)
        if os.path.realpath(proc.exe()) == os.path.realpath(binary):
            return proc
    except (psutil.Error, OSError):
        pass
    return None
