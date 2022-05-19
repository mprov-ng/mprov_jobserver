#!/usr/bin/python
import importlib.resources
import os
import signal
from mprov_jobserver.app import JobServer
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
        # using the default config file, see if one exists.
        if not os.path.isfile(configfile):
            # no config file in the default location, let's copy one in and 
            # say something to the user about editing it.
            for entry in importlib.resources.contents('mprov_jobserver'): 
                if entry[-4:] == "yaml":
                    print(entry)

                    # make the standard config directory
                    os.makedirs('/etc/mprov/', exist_ok=True)
                    try: 
                        with importlib.resources.open_text('mprov_jobserver', entry) as yaml_file:
                            with open('/etc/mprov/' + entry, "w") as yaml_file_out:
                                yaml_file_out.write(yaml_file.read())
                    except:
                        print("Error copying " + entry + " to /etc/mprov/")
                        sys.exit(1)
                    
            # that succeeded so let's grab the plugin information
            os.makedirs('/etc/mprov/plugins', exist_ok=True)
            for p_entry in importlib.resources.contents('mprov_jobserver.plugins'):
                if p_entry[-4:] == 'yaml':
                    try:
                        with importlib.resources.open_text('mprov_jobserver.plugins', p_entry) as p_yaml_file:
                            with open('/etc/mprov/plugins/' + p_entry, 'w') as p_yaml_out_file:
                                p_yaml_out_file.write(p_yaml_file.read())
                    except:
                        print('Error copying ' + p_entry + ' to /etc/mprov/plugins/')
                        sys.exit(1)
            
            # if we make it here, we have copied the config to /etc/mprov/ so prompt the user to go modify it
            print('A fresh config has been copied to /etc/mprov/  Please go there')
            print('and edit the .yaml files in that directory to setup your environment.')
            sys.exit(1)
            
    jobServer = JobServer(runonce=runonce, configfile=configfile)
    
    if jobServer is not None:
        # Start the main loop and run the plugin handling routines.
        return jobServer.start()
    return 1
def __main__():
    main()
if __name__ == "__main__" or __name__ == 'mprov.mprov_jobserver':
    main()
