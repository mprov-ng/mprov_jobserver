""" Run the validators (nodeattr, dnsmasq --test) the tests check output with. """
import subprocess

# Bound at import, before the no_processes fixture stubs subprocess.run out
# for the plugins.
_run = subprocess.run


def run(cmd):
  """ Run a command; return (ok, combined output). """
  result = _run(cmd, capture_output=True, text=True)
  return result.returncode == 0, result.stdout + result.stderr
