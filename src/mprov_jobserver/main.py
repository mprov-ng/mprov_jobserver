#!/usr/bin/python
import importlib.resources
import os
import signal
from mprov_jobserver.app import JobServer
import sys
from mprov_jobserver.log import get_logger

log = get_logger(__name__)

jobServer = None

# define our exit handler.
def exitHandler(signum, frame):
    if jobServer is not None:
        jobServer.stop()
        
    else:
        log.error("jobServer is None?")
        sys.exit(1)

signal.signal(signal.SIGINT, exitHandler)
signal.signal(signal.SIGTERM, exitHandler)

def copy_default_config(dest):
    """
    Copy the packaged config (jobserver.yaml and the plugins' YAMLs) into
    dest, for a job server started without one.  Each file is created 0600:
    the config and a plugin's settings can hold secrets.  Uses
    importlib.resources.files(); contents() and open_text() are deprecated
    (#346).  False, said, if a file can't be copied.
    """
    sources = [(importlib.resources.files('mprov_jobserver'), dest),
               (importlib.resources.files('mprov_jobserver.plugins'), os.path.join(dest, 'plugins'))]
    for package, target in sources:
        try:
            os.makedirs(target, exist_ok=True)
            for entry in sorted(package.iterdir(), key=lambda e: e.name):
                if not entry.name.endswith('.yaml') or not entry.is_file():
                    continue
                path = os.path.join(target, entry.name)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w') as out:
                    out.write(entry.read_text())
                os.chmod(path, 0o600)
                log.info(f"copied {entry.name} to {target}")
        except OSError as e:
            log.error(f"copying the default config to {target}: {e}")
            return False
    return True


def main():
    global jobServer
    runonce=False

    if '-r' in set(sys.argv):
        runonce = True
    
    if '-c' in set(sys.argv):
        configfile = sys.argv[sys.argv.index('-c') + 1]
    else:
        configfile = "/etc/mprov/jobserver.yaml"
        # using the default config file, see if one exists.
        if not os.path.isfile(configfile):
            # no config file in the default location, let's copy one in and
            # say something to the user about editing it.
            if not copy_default_config('/etc/mprov'):
                sys.exit(1)
            # if we make it here, we have copied the config to /etc/mprov/ so prompt the user to go modify it
            log.info('A fresh config has been copied to /etc/mprov/  Please go there')
            log.info('and edit the .yaml files in that directory to setup your environment.')
            sys.exit(1)
            
    jobServer = JobServer(runonce=runonce, configfile=configfile)
    
    if jobServer is not None:
        # Start the main loop and run the plugin handling routines.
        return jobServer.start()
    return 1
# exit with main()'s status, so a failed runonce session exits nonzero (#221)
def __main__():
    sys.exit(main())
if __name__ == "__main__" or __name__ == 'mprov.mprov_jobserver':
    sys.exit(main())
