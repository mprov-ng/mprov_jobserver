import signal
from mprov.mprov_jobserver.app import JobServer

jobServer = None

# define our exit handler.
def exitHandler(signum, frame):
    if jobServer is not None:
        jobServer.stop()
        raise KeyboardInterrupt;
    else:
        print("jobServer is None?")

signal.signal(signal.SIGINT, exitHandler)
#signal.signal(signal.SIGKILL, exitHandler)

def main():
    global jobServer
    jobServer = JobServer()

    if jobServer is not None:
        # Start the main loop and run the plugin handling routines.
        return jobServer.start()
    return 1
def __main__():
    main()
if __name__ == "__main__" or __name__ == 'mprov.mprov_jobserver':
    main()
