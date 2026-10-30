import os
import psutil


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
