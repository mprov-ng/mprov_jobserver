'''
The running job server's version, and the major.minor of it that the mPCC
is version-matched against (#287).
'''
import os
import re
from importlib.metadata import PackageNotFoundError, version as package_version

import requests

from mprov_jobserver.log import get_logger

log = get_logger(__name__)

# The version header both sides send, and the env var that turns the check
# off for local dev and CI (#452).
VERSION_HEADER = 'mProv-Version'
SKIP_CHECK_ENV = 'MPROV_SKIP_VERSION_CHECK'

# mismatches already logged, so a wrong-version mPCC doesn't fill the log
# with a line per request
_reported = set()

# VERSION at the repo root, for a run from a source checkout where the
# package isn't installed.  src/mprov_jobserver/version.py -> ../../VERSION
VERSION_FILE = os.path.join(os.path.dirname(__file__), '..', '..', 'VERSION')


def jobserver_version():
  ''' The version of the running job server, e.g. 0.6.0 or 0.6.0.dev7. '''
  try:
    return package_version('mprov_jobserver')
  except PackageNotFoundError:
    pass
  try:
    with open(VERSION_FILE) as version_file:
      return version_file.read().strip()
  except OSError:
    # Neither installed nor in a checkout: report an unmatchable version
    # rather than stopping the job server from starting at all.
    return '0.0.0'


def api_version(version=None):
  '''
  The major.minor the mPCC is matched against: 0.6.0.dev7 -> 0.6.  A
  version that doesn't start with major.minor gives 0.0, which matches
  nothing the mPCC advertises.
  '''
  if version is None:
    version = jobserver_version()
  match = re.match(r'\s*(\d+)\.(\d+)', version)
  if match is None:
    return '0.0'
  return f'{match.group(1)}.{match.group(2)}'


class VersionMismatch(requests.exceptions.RequestException):
  '''
  The mPCC doesn't speak this job server's API version (#287).  A subclass
  of RequestException so callers that already handle a failed request treat
  a mismatched mPCC as one.
  '''


def skip_version_check():
  return os.environ.get(SKIP_CHECK_ENV, '').strip().lower() not in ('', '0', 'false', 'no')


def check_response(response, *args, **kwargs):
  '''
  A requests response hook, so every reply from the mPCC is checked wherever
  the call was made from, rather than each caller checking for itself.
  '''
  if skip_version_check():
    return response
  theirs = response.headers.get(VERSION_HEADER)
  if theirs is not None:
    theirs = theirs.strip()
  ours = api_version()
  if theirs == ours:
    return response
  if (ours, theirs) not in _reported:
    _reported.add((ours, theirs))
    # An mPCC that sends no header is too old to be version-locked at all,
    # which is as much a mismatch as the wrong version (#452).
    spoken = f"speaks API version {theirs}" if theirs else "sends no API version"
    log.error(
      f"the mPCC at {response.url} {spoken}, this job server speaks {ours}; "
      f"upgrade them together, or set {SKIP_CHECK_ENV}=1 for a dev or CI run."
    )
  raise VersionMismatch(
    f"mPCC API version {theirs or 'missing'}, expected {ours}", response=response)


def install(session):
  ''' Send our version on a session's requests, and check the mPCC's on its
  replies.  Hooks aren't inherited when a session is copied, so every
  session that talks to the mPCC has to be passed through here (#114's
  per-address sessions included). '''
  session.headers.update({VERSION_HEADER: api_version()})
  if check_response not in session.hooks['response']:
    session.hooks['response'].append(check_response)
  return session
