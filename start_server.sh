#!/bin/bash
MPROV_ARGS=${MPROV_ARGS:="-c /etc/mprov/jobserver.yaml"}
# let's template our config out based on env vars
python3 -c 'import os
import sys
import jinja2
sys.stdout.write(jinja2.Template(sys.stdin.read()).render(env=os.environ))
' <  /etc/mprov/jobserver.yaml.j2 > /etc/mprov/jobserver.yaml

#/usr/local/bin/mprov_jobserver ${MPROV_ARGS}