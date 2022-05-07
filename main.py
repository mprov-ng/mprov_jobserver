#!/usr/bin/python
import signal
from mprov.mprov_jobserver.app import JobServer
import sys

jobServer = None

# define our exit handler.
def exitHandler(signum, frame):
    if jobServer is not None:
        jobServer.stop()
        
    else:
        print("jobServer is None?")
        sys.exit(1)

signal.signal(signal.SIGINT, exitHandler)
signal.signal(signal.SIGTERM, exitHandler)

def main():
    global jobServer
    runonce=False

    if '-r' in set(sys.argv):
        runonce = True
    
    if '-c' in set(sys.argv):
        configfile = sys.argv[sys.argv.index('-c') + 1]
    else:
        configfile = "/etc/mprov/jobserver.yaml"

    jobServer = JobServer(runonce=runonce, configfile=configfile)
    
    if jobServer is not None:
        # Start the main loop and run the plugin handling routines.
        return jobServer.start()
    return 1
def __main__():
    main()
if __name__ == "__main__" or __name__ == 'mprov.mprov_jobserver':
    main()
