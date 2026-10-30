'''
Which CA certificates to trust for HTTPS to the mPCC (#102).  requests
installed from PyPI trusts only certifi's bundle, not the system store, so
an mPCC with a site or self-signed CA failed even after the admin added the
CA to the OS (update-ca-trust), while wget, which uses the OS store, worked.
'''
import os

# the OS bundles update-ca-trust / update-ca-certificates write
OS_CA_BUNDLES = (
  '/etc/pki/tls/certs/ca-bundle.crt',     # RHEL, Rocky, Fedora
  '/etc/ssl/certs/ca-certificates.crt',   # Debian, Ubuntu, Alpine
)


def ca_bundle(configured):
  '''
  The CA bundle to verify the mPCC's certificate with: the configured
  caBundle if one is set, else the OS bundle, else None (requests'
  default, certifi).  The OS bundle holds the public CAs too, so a publicly
  trusted mPCC certificate still verifies.  A configured bundle that
  doesn't exist raises FileNotFoundError: failing to start beats silently
  trusting another set of CAs.
  '''
  if configured:
    if not os.path.isfile(configured):
      raise FileNotFoundError(f"caBundle {configured} doesn't exist")
    return configured
  for path in OS_CA_BUNDLES:
    if os.path.isfile(path):
      return path
  return None
