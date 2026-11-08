"""
The job server's own identity for the mPCC (mprov_control_center#381): a
private key it makes itself and a client certificate the mPCC's CA signs,
replacing the API key every agent shared.

- Enrolling: with a one-time token, POST /enroll/jobserver/ {token, csr,
  host_id, name} over HTTPS.  The token is a secret, so it only goes to an
  mPCC whose certificate checks out: against caBundle, against the CA whose
  SHA-256 fingerprint is configured (fetched from /enroll/ca.crt and
  checked), or against the ca.crt the mPCC put next to the token in
  /etc/mprov.  Never on first use alone.
- Renewing: POST /enroll/renew/ {csr} with the current certificate, in the
  last 30 days of its 90, with a new key.
- Using it: requests' session.cert, verifying the mPCC against caBundle or
  the CA the enrollment reply carried.

The key and certificate are kept together in one file, <tlsDir>/jobserver.pem
(0600), so a renewal replaces both at once; the CA is <tlsDir>/ca.crt.  The
key never leaves this host: the mPCC only ever sees the CSR.
"""
import datetime
import hashlib
import os
import tempfile
import warnings
from urllib.parse import urlsplit, urlunsplit

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from mprov_jobserver.log import get_logger

log = get_logger(__name__)

DEFAULT_TLS_DIR = '/var/lib/mprov/tls'
CREDENTIAL = 'jobserver.pem'
CA_CERT = 'ca.crt'
# the mPCC lets an agent renew in the last 30 days; ask a day after that
# opens, so a clock a little ahead of the mPCC's isn't refused as "not due"
RENEW_BEFORE = datetime.timedelta(days=29)
# how long to wait for an enrollment or renewal answer
TIMEOUT = 30


class EnrollError(Exception):
  ''' Enrolling or renewing failed.  retry says whether the same request
  could work later (the mPCC was down or busy); a refused token or CSR
  won't. '''

  def __init__(self, message, retry=False):
    super().__init__(message)
    self.retry = retry


def fingerprint(cert):
  ''' A certificate's SHA-256 fingerprint as lowercase hex, as the mPCC
  prints it (mprov_enroll_token, the admin). '''
  return cert.fingerprint(hashes.SHA256()).hex()


def normalize_fingerprint(value):
  ''' A fingerprint as configured, with colons, spaces and case ignored. '''
  return ''.join(ch for ch in str(value).lower() if ch in '0123456789abcdef')


def new_key_and_csr(name):
  ''' A new EC P-256 key (PEM) and a CSR for it.  The mPCC sets the
  certificate's subject itself; the CN is only a label. '''
  key = ec.generate_private_key(ec.SECP256R1())
  cn = (str(name).strip() or 'mprov-jobserver')[:64]
  csr = (x509.CertificateSigningRequestBuilder()
         .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
         .sign(key, hashes.SHA256()))
  key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption())
  return key_pem, csr.public_bytes(serialization.Encoding.PEM).decode()


def utcnow():
  return datetime.datetime.now(datetime.timezone.utc)


def write_private(path, data):
  ''' Write data to path, mode 0600, through a temporary file beside it, so
  a crash leaves the old file or the new one, never half of one. '''
  if isinstance(data, str):
    data = data.encode()
  fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.' + os.path.basename(path) + '.')
  try:
    with os.fdopen(fd, 'wb') as f:
      f.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
  except BaseException:
    try:
      os.unlink(tmp)
    except OSError:
      pass
    raise


class Identity():
  ''' The credential in a TLS directory. '''

  def __init__(self, tls_dir):
    self.tls_dir = tls_dir or DEFAULT_TLS_DIR
    self.credential_path = os.path.join(self.tls_dir, CREDENTIAL)
    self.ca_path = os.path.join(self.tls_dir, CA_CERT)

  def certificate(self):
    '''
    The certificate in jobserver.pem, if there's one whose key is there
    too; else None.  A file that can't be read is logged and counts as none.
    '''
    try:
      with open(self.credential_path, 'rb') as f:
        data = f.read()
    except FileNotFoundError:
      return None
    except OSError as err:
      log.error(f"unable to read {self.credential_path}: {err}")
      return None
    try:
      cert = x509.load_pem_x509_certificate(data)
      key = serialization.load_pem_private_key(data, password=None)
    except ValueError as err:
      log.error(f"{self.credential_path} doesn't hold a certificate and its key: {err}")
      return None
    if cert.public_key().public_numbers() != key.public_key().public_numbers():
      log.error(f"the key in {self.credential_path} isn't the certificate's.")
      return None
    return cert

  def usable(self, now=None):
    ''' The certificate, if it's in date; else None. '''
    cert = self.certificate()
    if cert is None:
      return None
    now = now or utcnow()
    if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
      log.warning(f"this job server's certificate (in {self.credential_path}) expired "
                  f"{cert.not_valid_after_utc:%Y-%m-%d %H:%M} UTC.")
      return None
    return cert

  def due(self, cert, now=None):
    ''' True if cert is in its renewal window. '''
    return cert.not_valid_after_utc - (now or utcnow()) <= RENEW_BEFORE

  def save(self, key_pem, cert_pem, ca_pem):
    ''' Keep a new key and certificate, and the CA the mPCC sent with them. '''
    os.makedirs(self.tls_dir, mode=0o700, exist_ok=True)
    if isinstance(key_pem, str):
      key_pem = key_pem.encode()
    if ca_pem:
      write_private(self.ca_path, ca_pem)
      os.chmod(self.ca_path, 0o644)
    write_private(self.credential_path, key_pem + cert_pem.encode())

  def save_ca(self, ca_pem):
    os.makedirs(self.tls_dir, mode=0o700, exist_ok=True)
    write_private(self.ca_path, ca_pem)
    os.chmod(self.ca_path, 0o644)


def fetch_ca(mprov_url, expected):
  '''
  The mPCC's CA certificate (PEM), fetched from /enroll/ca.crt without
  checking the server, so it's trusted only because its SHA-256 matches the
  expected fingerprint.  Raises EnrollError if it can't be fetched or
  doesn't match.
  '''
  url = mprov_url + 'enroll/ca.crt'
  try:
    # unverified on purpose: the fingerprint is the check, so the warning
    # urllib3 gives for it would only alarm
    with warnings.catch_warnings():
      warnings.simplefilter('ignore')
      response = requests.get(url, verify=False, timeout=TIMEOUT)
  except requests.RequestException as err:
    raise EnrollError(f"unable to fetch the mPCC's CA from {url}: {err}", retry=True)
  if response.status_code != 200:
    raise EnrollError(f"unable to fetch the mPCC's CA from {url}: it answered {response.status_code}", retry=True)
  try:
    cert = x509.load_pem_x509_certificate(response.content)
  except ValueError:
    raise EnrollError(f"{url} didn't send a certificate.", retry=True)
  got = fingerprint(cert)
  if got != normalize_fingerprint(expected):
    raise EnrollError(f"the mPCC's CA at {url} has fingerprint {got}, not the configured caFingerprint; "
                      "not enrolling with it.")
  return cert.public_bytes(serialization.Encoding.PEM).decode()


def _detail(response):
  try:
    body = response.json()
  except ValueError:
    return response.text.strip()[:200]
  if isinstance(body, dict):
    return '; '.join(f"{k}: {v}" for k, v in body.items())
  return str(body)[:200]


def _issued(response, url):
  ''' The (certificate, ca) PEMs of an enrollment or renewal reply. '''
  try:
    body = response.json()
  except ValueError:
    body = None
  if not isinstance(body, dict) or not isinstance(body.get('certificate'), str):
    raise EnrollError(f"{url} gave an invalid reply: {response.text.strip()[:200]}", retry=True)
  return body['certificate'], body.get('ca')


def enroll(mprov_url, verify, token, host_id, name):
  '''
  Enroll with a one-time token.  Returns (key PEM, certificate PEM, CA PEM).
  Raises EnrollError.
  '''
  url = mprov_url + 'enroll/jobserver/'
  key_pem, csr = new_key_and_csr(name)
  try:
    response = requests.post(url, json={'token': token, 'csr': csr, 'host_id': host_id, 'name': name},
                             verify=verify, timeout=TIMEOUT)
  except requests.exceptions.SSLError as err:
    raise EnrollError(f"the mPCC at {mprov_url} failed the certificate check, so the token wasn't sent: {err}",
                      retry=True)
  except requests.RequestException as err:
    raise EnrollError(f"unable to reach the mPCC at {url}: {err}", retry=True)
  if response.status_code == 200:
    cert_pem, ca_pem = _issued(response, url)
    return key_pem, cert_pem, ca_pem
  detail = _detail(response)
  if response.status_code == 403:
    raise EnrollError(f"the mPCC refused the enrollment token ({detail}); make a new one.")
  if response.status_code == 409:
    raise EnrollError(f"the mPCC refused this job server's host id {host_id} ({detail}).")
  if response.status_code == 404:
    raise EnrollError(f"the mPCC at {mprov_url} has no enrollment (it answered 404 for {url}); "
                      "is it 0.7.0 or later?")
  # 400 (a CSR it can't use) won't change; 503 (its signer is down) and
  # anything else may
  raise EnrollError(f"enrollment failed, the mPCC answered {response.status_code}: {detail}",
                    retry=response.status_code != 400)


def renew(session, mprov_url, name):
  '''
  Renew with the current certificate, on session.  Returns (key PEM,
  certificate PEM, CA PEM), or None if the mPCC says it isn't due yet.
  Raises EnrollError.
  '''
  url = mprov_url + 'enroll/renew/'
  key_pem, csr = new_key_and_csr(name)
  try:
    response = session.post(url, json={'csr': csr}, timeout=TIMEOUT)
  except requests.RequestException as err:
    raise EnrollError(f"unable to reach the mPCC at {url}: {err}", retry=True)
  if response.status_code == 200:
    cert_pem, ca_pem = _issued(response, url)
    return key_pem, cert_pem, ca_pem
  if response.status_code == 409:
    return None
  raise EnrollError(f"renewal failed, the mPCC answered {response.status_code}: {_detail(response)}",
                    retry=True)


def token_from(value, path):
  '''
  The enrollment token: value (MPROV_ENROLL_TOKEN or global enrollToken)
  if set, else the first line of the file at path; None if there's
  neither.  The file is the mPCC's (compose writes it into /etc/mprov), so
  it's only read, never changed.
  '''
  if value and str(value).strip():
    return str(value).strip()
  if not path:
    return None
  try:
    with open(path) as f:
      token = f.readline().strip()
  except FileNotFoundError:
    return None
  except OSError as err:
    log.warning(f"unable to read the enrollment token in {path}: {err}")
    return None
  return token or None


def token_id(token):
  ''' A short, non-secret id for a token, to remember one that was refused. '''
  return hashlib.sha256(token.encode()).hexdigest()[:16]


# ---- a booted node's certificate (mprov_control_center#381, C2) ------------
#
# The mPCC's boot scripts enroll a node as it boots (static/mprov_enroll.py)
# and keep its key, certificate and the mPCC's CA in /etc/mprov/tls, as
# separate files; the stateful installer copies them to disk.  The mPCC's
# scripts renew it (mprov_control_center#506); the job server only uses it.

NODE_TLS_DIR = '/etc/mprov/tls'
NODE_KEY, NODE_CERT = 'node.key', 'node.crt'


class NodeCredential():
  ''' A node's key, certificate and CA, as paths, and its certificate. '''

  def __init__(self, cert_path, key_path, ca_path, cert):
    self.cert_path = cert_path
    self.key_path = key_path
    self.ca_path = ca_path
    self.cert = cert


def node_credential(tls_dir=None, now=None):
  '''
  The node's credential in tls_dir (/etc/mprov/tls), if it has all three
  files and the certificate is in date and its key's; else None.  An
  expired one is said: the node needs to enroll again.
  '''
  tls_dir = tls_dir or NODE_TLS_DIR
  cert_path, key_path, ca_path = (os.path.join(tls_dir, name) for name in (NODE_CERT, NODE_KEY, CA_CERT))
  if not all(os.path.isfile(path) for path in (cert_path, key_path, ca_path)):
    return None
  try:
    with open(cert_path, 'rb') as f:
      cert = x509.load_pem_x509_certificate(f.read())
    with open(key_path, 'rb') as f:
      key = serialization.load_pem_private_key(f.read(), password=None)
  except (OSError, ValueError) as err:
    log.error(f"unable to read this node's credential in {tls_dir}: {err}")
    return None
  if cert.public_key().public_numbers() != key.public_key().public_numbers():
    log.error(f"the key in {key_path} isn't the certificate's.")
    return None
  now = now or utcnow()
  if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
    log.error(f"this node's certificate (in {cert_path}) expired {cert.not_valid_after_utc:%Y-%m-%d %H:%M} UTC; "
              "it needs to enroll again: netboot it, or let it re-enroll from the mPCC's admin.")
    return None
  return NodeCredential(cert_path, key_path, ca_path, cert)


def https_url(url):
  '''
  The mPCC's HTTPS URL for its URL: the same host and path on port 443, as
  the mPCC's boot scripts use it (static/mprov_enroll.py).  A node's
  configuration has the plain-HTTP URL it boots from.
  '''
  parts = urlsplit(url)
  if parts.scheme == 'https':
    return url
  host = parts.hostname or ''
  if ':' in host:
    host = f'[{host}]'
  return urlunsplit(('https', host, parts.path or '/', '', ''))


# ---- an image build's credential (mprov_control_center#381, C2) ------------

def build_credential(session, mprov_url, image):
  '''
  A 2-hour credential for the script runner inside an image this job server
  is building: POST /enroll/build/ {csr, image}, on session, which has the
  job server's own certificate.  It can only read that image.  Returns (key
  PEM, certificate PEM, CA PEM).  Raises EnrollError.
  '''
  url = mprov_url + 'enroll/build/'
  key_pem, csr = new_key_and_csr('build ' + str(image))
  try:
    response = session.post(url, json={'csr': csr, 'image': image}, timeout=TIMEOUT)
  except requests.RequestException as err:
    raise EnrollError(f"unable to reach the mPCC at {url}: {err}", retry=True)
  if response.status_code == 200:
    cert_pem, ca_pem = _issued(response, url)
    return key_pem, cert_pem, ca_pem
  if response.status_code == 404:
    raise EnrollError(f"the mPCC at {mprov_url} has no build credentials (it answered 404 for {url}); "
                      "it needs to be 0.7.0 or later.")
  raise EnrollError(f"the mPCC refused a build credential for image {image} "
                    f"({response.status_code}: {_detail(response)}).", retry=response.status_code >= 500)
