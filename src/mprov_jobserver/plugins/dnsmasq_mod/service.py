import os
import psutil
from mprov_jobserver.log import get_logger

log = get_logger(__name__)


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
