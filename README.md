# mProv Job Server
![Build Status](https://img.shields.io/github/actions/workflow/status/mprov-ng/mprov_jobserver/ci_build.yml?style=plastic)
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
You will need to create an API key in the mPCC for the job server.  Once you have the api key, you will want to add that key to the `jobserver.yaml` file.  There should be an example in there already, it will not work.  You must replace it.  While you are in there, you will want to setup the `mprovURL` entry to point to your mPCC instance.  After that, you can enable/disable whatever jobmodules you want this job server to run by uncomment/commenting the lines that describe the jobmodules.

do not use localhost use IP or internal name 

## Arguments
Job server takes a few command line arguments.  Global arguments are

- -r Runs the jobserver in 'runonce' mode.  The job server will run any of the job modules listed once then exit.
- -d Tells the job server not to register with the mPCC.  This is useful only when running the `script-runner` module to run post-boot or image-gen scripts.

Some plugins also use commandline arguments.

### script-runner args
The `script-runner` job module will take the following arguments:
- -i <systemimage-ID> This is the id of the system image you are going to run scripts against.  Mutually exclusive with -s
- -s <system-hostname> The host name of the system you are running the scripts against.
- -b     Runs scripts in post-boot mode.
- -r (Global Option) The script-runner must be run in `runonce` mode only.  It is probably wise to also pass -d
- -d (Global Option) The script-runner will not register as a job server with mPCC
