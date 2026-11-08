"""
bmc-rotate: change, check or recover a BMC's credentials for the mPCC
(mprov_control_center#486, #341).

The mPCC queues one job per BMC; only a job server with an address on that
BMC's network can claim it.  For each job this claims, one at a time:

1. GET /bmcrotations/<id>/secret/ (with this job server's certificate): the
   BMC's address and the credentials, {kind: change|check|find, current,
   new, candidates}.  Never logged.
2. Talk to the BMC: Redfish, or IPMI only when the BMC has no Redfish (not
   when Redfish refuses the credentials).
   - change: log in with the current pair, set the new password, log in
     with it; then, if the user name changes, rename and log in again.
   - check: log in with the current pair.
   - find: try each candidate pair in order; the first that logs in.
3. POST /bmcrotations/<id>/result/ {status, method, verified, tls_verified,
   error}: verified is the pair that just logged in (current,
   password_only, new, or a candidate's index), which is the only pair the
   mPCC stores.  Retried; if it still can't be delivered the job fails,
   and the mPCC treats the change as unconfirmed.

Secrets never reach a log, an argv or the environment: ipmitool gets the
login password with -f and the new credentials through `ipmitool exec`,
each from a 0600 file in a 0700 directory removed afterwards, and every
error is scrubbed of every password before it's logged or reported.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import warnings
from concurrent.futures import ThreadPoolExecutor

import requests

from .plugin import JobServerPlugin
from mprov_jobserver.log import get_logger

log = get_logger(__name__)

RESULT_TRIES = 5
RESULT_RETRY_WAIT = 2
ERROR_MAX = 500
# ipmitool's error text when the BMC answered but refused the credentials,
# as opposed to not answering at all
IPMI_REFUSED = re.compile(r"RAKP|unauthori[sz]ed|invalid (user|password)|password.*(invalid|incorrect)", re.I)


class Unreachable(Exception):
  ''' Neither Redfish nor IPMI on the BMC answered. '''


class Absent(Exception):
  ''' The BMC has no Redfish service. '''


def scrub(text, secrets):
  ''' text with every secret replaced by ***, at most ERROR_MAX long. '''
  text = str(text)
  for secret in sorted((s for s in secrets if s), key=len, reverse=True):
    text = text.replace(secret, "***")
  return text[:ERROR_MAX]


def host_of(address):
  ''' The address as a URL host: an IPv6 address in brackets. '''
  return f"[{address}]" if ":" in address and not address.startswith("[") else address


class Redfish():
  ''' The BMC's Redfish service, logging in with HTTP basic auth. '''
  method = 'redfish'

  def __init__(self, address, verify, timeout):
    self.base = f"https://{host_of(address)}/redfish/v1/"
    self.root = f"https://{host_of(address)}"
    self.verify = verify
    self.timeout = timeout

  def _request(self, method, url, auth=None, **kwargs):
    with warnings.catch_warnings():
      # an unverified BMC certificate is the configured choice, and reported
      # as tls_verified: false; the warning would only repeat it per request
      warnings.simplefilter('ignore')
      return requests.request(method, url, auth=auth, verify=self.verify, timeout=self.timeout, **kwargs)

  def probe(self):
    ''' Raises Absent if there's no Redfish service, Unreachable if the
    BMC's HTTPS port doesn't answer at all. '''
    try:
      response = self._request('GET', self.base)
    except requests.exceptions.SSLError:
      # it answered HTTPS, with a certificate verifyBMC refuses
      raise
    except requests.exceptions.ConnectionError as err:
      raise Absent(f"no Redfish service: {err}")
    except requests.exceptions.Timeout as err:
      raise Absent(f"no answer on HTTPS: {err}")
    if response.status_code == 404:
      raise Absent("no Redfish service (404)")
    try:
      body = response.json()
    except ValueError:
      raise Absent("no Redfish service (not JSON)")
    if not isinstance(body, dict) or 'AccountService' not in body:
      raise Absent("no Redfish account service")

  def login(self, pair):
    ''' True if the pair logs in, False if the BMC refuses it. '''
    response = self._request('GET', self.base + 'AccountService/Accounts', auth=(pair['username'], pair['password']))
    if response.status_code in (401, 403):
      return False
    if not 200 <= response.status_code < 300:
      raise RuntimeError(f"Redfish answered {response.status_code} to a login")
    return True

  def account(self, pair, username):
    ''' (URI, ETag) of the account named username, seen as pair. '''
    auth = (pair['username'], pair['password'])
    response = self._request('GET', self.base + 'AccountService/Accounts', auth=auth)
    response.raise_for_status()
    for member in response.json().get('Members', []):
      uri = member.get('@odata.id', '')
      if not uri:
        continue
      detail = self._request('GET', self.root + uri, auth=auth)
      if detail.status_code != 200:
        continue
      if detail.json().get('UserName') == username:
        return uri, detail.headers.get('ETag')
    raise RuntimeError(f"Redfish has no account named {username}")

  def update(self, pair, username, changes):
    ''' PATCH the account named username, logging in as pair. '''
    uri, etag = self.account(pair, username)
    headers = {'If-Match': etag} if etag else {}
    response = self._request('PATCH', self.root + uri, auth=(pair['username'], pair['password']),
                             json=changes, headers=headers)
    if not 200 <= response.status_code < 300:
      raise RuntimeError(f"Redfish refused the change ({response.status_code}): {response.text[:200]}")

  def check_new(self, username, password):
    ''' Redfish takes any user name and password the BMC's policy allows. '''

  def set_password(self, pair, password):
    self.update(pair, pair['username'], {'Password': password})

  def set_username(self, pair, username):
    self.update(pair, pair['username'], {'UserName': username})


class Ipmi():
  '''
  The BMC over IPMI with ipmitool.  The login password goes in with -f from
  a 0600 file and the new credentials through `ipmitool exec` from another;
  neither is ever on argv or in the environment.
  '''
  method = 'ipmi'

  def __init__(self, address, workdir, channel, interface, timeout):
    self.address = address
    self.workdir = workdir
    self.channel = str(channel)
    self.interface = interface
    self.timeout = timeout

  def _private(self, name, content):
    fd, path = tempfile.mkstemp(dir=self.workdir, prefix=name + '.')
    with os.fdopen(fd, 'w') as f:
      f.write(content)
    os.chmod(path, 0o600)
    return path

  def run(self, pair, args):
    ''' ipmitool, logged in as pair, with args.  (rc, output). '''
    password = pair.get('password') or ''
    # an empty password (a factory default) isn't a secret, and ipmitool
    # won't read an empty -f file (#343)
    pwfile = self._private('login', password) if password else None
    login = ['-f', pwfile] if pwfile else ['-P', '']
    env = {k: v for k, v in os.environ.items() if k != 'IPMI_PASSWORD'}
    try:
      # -v: without it, ipmitool doesn't say why a login was refused
      # ("RAKP 2 HMAC is invalid" for a wrong password), and a refused
      # password looks just like a BMC that doesn't answer (#343)
      result = subprocess.run(['ipmitool', '-v', '-I', self.interface, '-H', self.address,
                               '-U', pair.get('username') or ''] + login + args,
                              capture_output=True, text=True, env=env, timeout=self.timeout)
      return result.returncode, (result.stdout or '') + (result.stderr or '')
    except subprocess.TimeoutExpired:
      return 1, 'ipmitool timed out'
    finally:
      if pwfile:
        os.unlink(pwfile)

  def probe(self, pair):
    ''' Raises Unreachable if the BMC doesn't answer IPMI at all. '''
    rc, out = self.run(pair, ['mc', 'guid'])
    if rc != 0 and 'Unable to read password' in out:
      # our side, not the BMC's: never "unreachable" (#343)
      raise RuntimeError(f"ipmitool couldn't read the login password: {out.strip()[-200:]}")
    if rc != 0 and not IPMI_REFUSED.search(out):
      raise Unreachable(f"no answer over IPMI either: {out.strip()[-200:]}")

  def login(self, pair):
    rc, out = self.run(pair, ['mc', 'guid'])
    if rc == 0:
      return True
    if IPMI_REFUSED.search(out):
      return False
    raise RuntimeError(f"ipmitool failed: {out.strip()[-200:]}")

  def user_id(self, pair, username):
    rc, out = self.run(pair, ['user', 'list', self.channel])
    if rc != 0:
      raise RuntimeError(f"unable to list the BMC's users: {out.strip()[-200:]}")
    for line in out.splitlines():
      fields = line.split()
      if len(fields) >= 2 and fields[0].isdigit() and fields[1] == username:
        return fields[0]
    raise RuntimeError(f"the BMC has no user named {username} on channel {self.channel}")

  def exec(self, pair, line):
    ''' One ipmitool command, from a 0600 file, so its arguments stay off argv. '''
    path = self._private('exec', line + '\n')
    try:
      rc, out = self.run(pair, ['exec', path])
    finally:
      os.unlink(path)
    if rc != 0:
      raise RuntimeError(f"ipmitool refused the change: {out.strip()[-200:]}")

  @staticmethod
  def usable(value, most, what):
    ''' ipmitool exec splits its lines on white space, and IPMI caps lengths. '''
    if not value or re.search(r'\s', value) or len(value) > most:
      raise ValueError(f"the new {what} can't be set over IPMI: it's empty, longer than {most} "
                       "characters, or has white space in it")

  def check_new(self, username, password):
    ''' Raises ValueError, before anything is changed, for credentials
    ipmitool can't set. '''
    self.usable(password, 20, 'password')
    self.usable(username, 16, 'user name')

  def set_password(self, pair, password):
    self.usable(password, 20, 'password')
    uid = self.user_id(pair, pair['username'])
    self.exec(pair, f"user set password {uid} {password}" + (" 20" if len(password) > 16 else ""))

  def set_username(self, pair, username):
    self.usable(username, 16, 'user name')
    uid = self.user_id(pair, pair['username'])
    self.exec(pair, f"user set name {uid} {username}")


class bmc_rotate(JobServerPlugin):
  jobModule = 'bmc-rotate'
  # check the BMCs' HTTPS certificates: off by default, since BMCs mostly
  # have self-signed ones; what was done is reported as tls_verified
  verifyBMC = False
  bmcCABundle = ""
  # how many BMCs at once
  parallel = 4
  ipmiChannel = 1
  ipmiInterface = 'lanplus'
  # seconds to wait for a BMC.  BMCs can take half a minute to apply an
  # account change; one that takes longer than this is reported
  # unconfirmed, which the mPCC's "find" then sorts out.
  timeout = 60

  def verify(self):
    if not self.verifyBMC:
      return False
    return self.bmcCABundle or True

  def handle_jobs(self):
    try:
      response = self.js.session.get(self.js.mprovURL + f"jobs/?module={self.jobModule}&status=1")
    except requests.RequestException as err:
      self.js.mpcc_unreachable(self.jobModule, err)
      return
    if response.status_code != 200:
      return
    try:
      jobs = response.json()
    except ValueError:
      return
    if not isinstance(jobs, list) or not jobs:
      return
    with ThreadPoolExecutor(max_workers=max(1, int(self.parallel or 1))) as pool:
      list(pool.map(self.take, jobs))

  def take(self, job):
    ''' Claim one job and do it; another job server may have it. '''
    if not isinstance(job, dict) or 'id' not in job:
      return
    if not self.js.claim_job(self.jobModule, job):
      return
    try:
      self.do_job(job)
    except Exception as err:
      # nothing secret is in reach here: do_job scrubs what it reports
      log.error(f"bmc-rotate job {job['id']} failed: {err.__class__.__name__}")
      self.js.update_job_status(self.jobModule, 3, jobid=job['id'])

  def do_job(self, job):
    params = job.get('params') if isinstance(job.get('params'), dict) else {}
    rotation = params.get('rotation')
    if not isinstance(rotation, int):
      log.error(f"bmc-rotate job {job['id']} names no request (params: {params!r}).")
      self.js.update_job_status(self.jobModule, 3, jobid=job['id'])
      return
    url = self.js.mprovURL + f"bmcrotations/{rotation}/"
    response = self.js.session.get(url + 'secret/')
    if response.status_code == 409:
      log.info(f"bmc-rotate request {rotation} is already finished.")
      self.js.update_job_status(self.jobModule, 4, jobid=job['id'])
      return
    if response.status_code != 200:
      log.error(f"the mPCC refused bmc-rotate request {rotation}'s details ({response.status_code}).")
      self.js.update_job_status(self.jobModule, 3, jobid=job['id'])
      return
    secret = response.json()
    secrets = [p.get('password', '') for p in [secret.get('current') or {}, secret.get('new') or {}]
               + list(secret.get('candidates') or []) if isinstance(p, dict)]
    workdir = tempfile.mkdtemp(prefix='mprov-bmc-')
    try:
      result = self.rotate(secret, workdir, secrets)
    finally:
      shutil.rmtree(workdir, ignore_errors=True)
    address = (secret.get('bmc') or {}).get('ipaddress')
    log.info(f"bmc-rotate {secret.get('kind')} on {address}: {result['status']} over {result['method'] or 'nothing'}"
             f"{', ' + result['error'] if result.get('error') else ''}")
    delivered = self.report(url + 'result/', result)
    if not delivered:
      log.error(f"bmc-rotate request {rotation}: result not delivered to the mPCC; it treats the request as unconfirmed.")
    self.js.update_job_status(self.jobModule, 4 if delivered else 3, jobid=job['id'])

  def report(self, url, result):
    for attempt in range(RESULT_TRIES):
      try:
        response = self.js.session.post(url, data=json.dumps(result))
        if 200 <= response.status_code < 300:
          return True
        if response.status_code in (403, 409):
          # not ours any more, or already recorded: retrying won't help
          return False
      except requests.RequestException:
        pass
      if attempt + 1 < RESULT_TRIES:
        time.sleep(RESULT_RETRY_WAIT * (attempt + 1))
    return False

  def connect(self, address, workdir, pair):
    ''' Redfish if the BMC has it, else IPMI.  Raises Unreachable. '''
    redfish = Redfish(address, self.verify(), self.timeout)
    try:
      redfish.probe()
      return redfish
    except Absent:
      pass
    ipmi = Ipmi(address, workdir, self.ipmiChannel, self.ipmiInterface, self.timeout)
    ipmi.probe(pair)
    return ipmi

  def rotate(self, secret, workdir, secrets):
    ''' Do the request on the BMC; the result to report, scrubbed. '''
    result = {'status': 'failed', 'method': '', 'verified': None, 'tls_verified': None, 'error': ''}
    # whether the BMC may have been changed: if so, an error leaves it unknown
    touched = [False]
    try:
      kind = secret.get('kind')
      address = (secret.get('bmc') or {}).get('ipaddress')
      current = secret.get('current') or {}
      if not address:
        raise RuntimeError("the request has no BMC address")
      # a pair to see whether the BMC answers IPMI with: for a "find" the
      # mPCC has already cleared the current one, so the first candidate (#343)
      candidates = list(secret.get('candidates') or [])
      probe = current if current.get('username') else (candidates[0] if candidates else current)
      bmc = self.connect(address, workdir, probe)
      result['method'] = bmc.method
      result['tls_verified'] = bool(self.verify()) if bmc.method == 'redfish' else None
      if kind == 'check':
        self.check(bmc, current, result)
      elif kind == 'find':
        self.find(bmc, candidates, result)
      elif kind == 'change':
        self.change(bmc, current, secret.get('new') or {}, result, touched)
      else:
        raise RuntimeError(f"unknown request kind {kind!r}")
    except Unreachable as err:
      result.update(status='unreachable', verified=None, error=str(err))
    except Exception as err:
      if touched[0]:
        # the BMC may already have the new password: the mPCC keeps every
        # pair that might work, and "find" sorts it out
        result.update(status='unconfirmed', verified=None)
      else:
        result.update(status='failed', verified=None)
      result['error'] = f"{err.__class__.__name__}: {err}"
    result['error'] = scrub(result['error'], secrets)
    return result

  def check(self, bmc, current, result):
    if bmc.login(current):
      result.update(status='succeeded', verified='current')
    else:
      result.update(status='failed', error="the BMC refused the stored credentials")

  def find(self, bmc, candidates, result):
    for index, pair in enumerate(candidates):
      if isinstance(pair, dict) and bmc.login(pair):
        result.update(status='succeeded', verified=str(index))
        return
    result.update(status='failed', error="the BMC refused every candidate")

  def change(self, bmc, current, new, result, touched):
    if not bmc.login(current):
      result.update(status='failed', error="the BMC refused the current credentials, so nothing was changed")
      return
    user, password = new.get('username') or current['username'], new.get('password') or current['password']
    # before anything is changed, so a refusal here leaves the BMC as it was
    bmc.check_new(user, password)
    if password != current['password']:
      touched[0] = True
      bmc.set_password(current, password)
      with_password = {'username': current['username'], 'password': password}
      if not bmc.login(with_password):
        if bmc.login(current):
          result.update(status='failed', verified='current', error="the BMC kept the old password")
        else:
          result.update(status='unconfirmed', error="neither the old nor the new password logs in")
        return
    else:
      with_password = current
    if user != current['username']:
      touched[0] = True
      try:
        bmc.set_username(with_password, user)
      except Exception as err:
        if bmc.login(with_password):
          result.update(status='partial', verified='password_only', error=f"the BMC kept the old user name: {err}")
          return
        raise
      if bmc.login({'username': user, 'password': password}):
        result.update(status='succeeded', verified='new')
      elif bmc.login(with_password):
        result.update(status='partial', verified='password_only', error="the BMC kept the old user name")
      else:
        result.update(status='unconfirmed', error="neither the old nor the new user name logs in")
      return
    result.update(status='succeeded', verified='new')
