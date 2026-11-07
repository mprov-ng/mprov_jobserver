#!/bin/bash
MPROV_ARGS=${MPROV_ARGS:="-c /etc/mprov/jobserver.yaml"}

# if a file exists for a generated apikey, let's source it
# because it likely came from an mpcc container.
if [ -e /etc/mprov/apikey ]
then
  . /etc/mprov/apikey
fi
# bring /etc/mprov (a volume shared with the mPCC) up to date with the
# template and plugin configs this image ships (#334).  It only logs on a
# failure, never stops the start.
/usr/libexec/mprov/bin/python3 -m mprov_jobserver.container_config /usr/share/mprov/config /etc/mprov

# let's template our config out based on env vars, on the job server's own
# Python: in the image, /usr/bin/python3 is the OS's and has no jinja2 (#111)
/usr/libexec/mprov/bin/python3 -c 'import os
import sys
import jinja2
sys.stdout.write(jinja2.Template(sys.stdin.read()).render(env=os.environ))
' <  /etc/mprov/jobserver.yaml.j2 > /etc/mprov/jobserver.yaml

# exec, so this script doesn't sit between the container's init and the job
# server: without it a SIGTERM here kills the script and leaves the job
# server orphaned (#294).  MPROV_ARGS is deliberately unquoted: it holds a
# whole argument string, e.g. "-c /etc/mprov/jobserver.yaml", and is meant
# to be split into separate arguments.
# shellcheck disable=SC2086
exec /usr/local/bin/mprov_jobserver ${MPROV_ARGS}