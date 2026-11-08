"""
Managed files: place the files the mPCC attaches to an image (through its
OS distro, system groups or the image itself) into the image while it's
built (#96, mprov_control_center#519).

The image's entity (GET /systemimages/<slug>/) carries them in
config.files, merged by the mPCC: [{slug, name, version, dest_path, owner,
group, mode, template, url}].  A template's url is the copy the mPCC
rendered for this image, read with the job server's certificate; anything
else is the uploaded file under /media.  An mPCC from before #519 sends no
files, and nothing is done.

They're placed after the base OS is installed and before the image-gen
scripts (and so before Ansible) run, so the scripts can use them.  For each
file, in order: download it (at most 10 MiB), create missing parents as
root:root 0755, write it to a temporary file beside the destination, give
it its owner and group inside the image (`chroot <image> chown`, so the
names are the image's own) and its mode, and rename it into place.  The
log names the file and its version, never its contents: a rendered file
holds the image's Configuration Parameters.

What's placed is recorded in /var/lib/mprov/placed-files.json in the
image.  A file that's no longer attached is removed at the next build.  If
the path belonged to a package before it was placed, the package is then
reinstalled to bring its own file back, and the file is checked against the
package's digest: rpm leaves an existing %config(noreplace) file alone on a
reinstall and still succeeds, so the placed copy is always removed first, and
the reinstall's exit code isn't trusted (#348).  A package file that can't be
restored fails the build, with the placed copy already gone.

Any problem fails the build, so nothing half-configured is packed.  Every
entry is checked before anything is written:
- dest_path is absolute and normalised, not /, and not under /proc, /sys,
  /dev, /run or /tmp/mprov (where the build's credential is); every part of
  it that exists in the image is a real directory, never a link, so a link
  in the image can't send a write outside it; and it isn't a directory;
- mode is 3 or 4 octal digits, with no setuid or setgid bit unless the
  image-update setting allowSetuidFiles is on;
- owner and group are names or numeric ids.
"""
import hashlib
import json
import os
import posixpath
import re
import stat
import subprocess
import tempfile
from urllib.parse import urlsplit, urlunsplit

import requests

from mprov_jobserver.log import get_logger

log = get_logger(__name__)

MAX_BYTES = 10 * 1024 * 1024
FORBIDDEN_ROOTS = ("/proc", "/sys", "/dev", "/run", "/tmp/mprov")
MANIFEST = "/var/lib/mprov/placed-files.json"
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}\$?|[0-9]{1,10}")
MODE = re.compile(r"[0-7]{3,4}")
TIMEOUT = 60


class PlacementError(Exception):
  ''' A managed file couldn't be placed; the build fails. '''


def check(entry, allow_setuid):
  ''' Raises PlacementError if the entry can't be placed as it is. '''
  slug = entry.get('slug') if isinstance(entry, dict) else None
  if not isinstance(slug, str) or not slug:
    raise PlacementError(f"a managed file without a slug: {entry!r}")
  dest = entry.get('dest_path')
  if not isinstance(dest, str) or not dest.startswith('/') or dest == '/' or '\0' in dest \
      or posixpath.normpath(dest) != dest:
    raise PlacementError(f"file {slug}: {dest!r} isn't an absolute, normalised path")
  if any(dest == root or dest.startswith(root + '/') for root in FORBIDDEN_ROOTS):
    raise PlacementError(f"file {slug}: files can't be placed under {', '.join(FORBIDDEN_ROOTS)}")
  mode = str(entry.get('mode') or '')
  if not MODE.fullmatch(mode):
    raise PlacementError(f"file {slug}: mode {mode!r} isn't 3 or 4 octal digits")
  if int(mode, 8) & 0o6000 and not allow_setuid:
    raise PlacementError(f"file {slug}: mode {mode} sets setuid or setgid, which needs the image-update "
                         "setting allowSetuidFiles")
  for what in ('owner', 'group'):
    if not NAME.fullmatch(str(entry.get(what) or '')):
      raise PlacementError(f"file {slug}: {what} {entry.get(what)!r} isn't a name or a numeric id")
  url = entry.get('url')
  if not isinstance(url, str) or not url.startswith('/'):
    raise PlacementError(f"file {slug}: no url to download it from")


def safe_parent(root, dest, create=True):
  '''
  The host path of dest's directory in the image at root, creating missing
  parts root:root 0755.  Raises PlacementError if any part that exists
  isn't a real directory: a link would point the write outside the image.
  '''
  path = root
  for part in posixpath.dirname(dest).strip('/').split('/'):
    if not part:
      continue
    path = os.path.join(path, part)
    try:
      st = os.lstat(path)
    except FileNotFoundError:
      if not create:
        return None
      os.mkdir(path, 0o755)
      os.chown(path, 0, 0)
      continue
    if not stat.S_ISDIR(st.st_mode):
      what = "a link" if stat.S_ISLNK(st.st_mode) else "not a directory"
      raise PlacementError(f"{posixpath.dirname(dest)} in the image goes through {path[len(root):]}, which is {what}")
  return path


def origin_url(mprov_url, path):
  ''' path (root-relative, as the mPCC sends it) on mprovURL's scheme and host. '''
  parts = urlsplit(mprov_url)
  return urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def download(session, mprov_url, entry):
  ''' The file's bytes, at most MAX_BYTES.  Raises PlacementError. '''
  url = origin_url(mprov_url, entry['url'])
  try:
    if entry.get('template'):
      # rendered for this image, with its Configuration Parameters: the job
      # server's own credentials
      response = session.get(url, stream=True, timeout=TIMEOUT)
    else:
      # the uploaded file under /media, checked against the mPCC's CA, but
      # not an API call (no API version to check)
      response = requests.get(url, stream=True, timeout=TIMEOUT, verify=getattr(session, 'verify', True))
  except requests.RequestException as err:
    raise PlacementError(f"file {entry['slug']}: unable to download it: {err}")
  with response:
    if response.status_code != 200:
      detail = ''
      try:
        body = response.json()
        detail = body.get('detail', '') if isinstance(body, dict) else ''
      except ValueError:
        pass
      raise PlacementError(f"file {entry['slug']}: the mPCC answered {response.status_code}"
                           + (f": {detail}" if detail else ""))
    data = bytearray()
    for chunk in response.iter_content(chunk_size=65536):
      data.extend(chunk)
      if len(data) > MAX_BYTES:
        raise PlacementError(f"file {entry['slug']}: larger than {MAX_BYTES // (1024 * 1024)} MiB")
  return bytes(data)


def chroot(root, args):
  return subprocess.run(['chroot', root] + args, capture_output=True, text=True)


def package_of(root, dest):
  ''' The package that owns dest in the image, or None. '''
  result = chroot(root, ['rpm', '-qf', '--queryformat', '%{NAME}', dest])
  name = result.stdout.strip()
  return name if result.returncode == 0 and name and ' ' not in name else None


def place(root, entry, data):
  ''' Write one file into the image, with its owner, group and mode. '''
  dest = entry['dest_path']
  parent = safe_parent(root, dest)
  target = os.path.join(parent, posixpath.basename(dest))
  try:
    st = os.lstat(target)
    if stat.S_ISDIR(st.st_mode):
      raise PlacementError(f"file {entry['slug']}: {dest} is a directory in the image")
  except FileNotFoundError:
    pass
  fd, tmp = tempfile.mkstemp(dir=parent, prefix='.' + posixpath.basename(dest) + '.mprov.')
  try:
    with os.fdopen(fd, 'wb') as f:
      f.write(data)
    inside = posixpath.join(posixpath.dirname(dest), os.path.basename(tmp))
    owner = f"{entry['owner']}:{entry['group']}"
    result = chroot(root, ['chown', owner, inside])
    if result.returncode != 0:
      raise PlacementError(f"file {entry['slug']}: no user or group {owner} in the image "
                           f"({result.stderr.strip()[-200:]})")
    # after chown, which clears setuid and setgid
    os.chmod(tmp, int(entry['mode'], 8))
    os.replace(tmp, target)
  except BaseException:
    try:
      os.unlink(tmp)
    except OSError:
      pass
    raise


def load_manifest(root):
  parent = safe_parent(root, MANIFEST, create=False)
  if parent is None:
    return {}
  try:
    with open(os.path.join(parent, posixpath.basename(MANIFEST))) as f:
      manifest = json.load(f)
    return manifest if isinstance(manifest, dict) else {}
  except (OSError, ValueError):
    return {}


def save_manifest(root, manifest):
  parent = safe_parent(root, MANIFEST)
  fd, tmp = tempfile.mkstemp(dir=parent, prefix='.placed-files.')
  with os.fdopen(fd, 'w') as f:
    json.dump(manifest, f, indent=2, sort_keys=True)
  os.chmod(tmp, 0o644)
  os.replace(tmp, os.path.join(parent, posixpath.basename(MANIFEST)))


# rpm's FILEDIGESTALGO: the OpenPGP hash algorithm ids; none means MD5
DIGESTS = {1: 'md5', 2: 'sha1', 8: 'sha256', 9: 'sha384', 10: 'sha512', 11: 'sha224'}


def package_digests(root, package, dest):
  '''
  The digests the installed package records for dest (one per installed
  instance), and the algorithm, or None if rpm can't say.  An empty digest
  means rpm has no content for the path (a %ghost file, a directory).
  '''
  algo = chroot(root, ['rpm', '-q', '--queryformat', '%{FILEDIGESTALGO}\\n', package])
  files = chroot(root, ['rpm', '-q', '--queryformat', '[%{FILENAMES}\\t%{FILEDIGESTS}\\n]', package])
  if algo.returncode != 0 or files.returncode != 0:
    return None
  ids = algo.stdout.split()
  name = DIGESTS.get(int(ids[0]), None) if ids and ids[0].isdigit() else 'md5'
  if name is None:
    return None
  digests = {line.split('\t', 1)[1] for line in files.stdout.splitlines()
             if '\t' in line and line.split('\t', 1)[0] == dest}
  return (name, digests) if digests else None


def file_digest(path, name):
  digest = hashlib.new(name)
  with open(path, 'rb') as f:
    for chunk in iter(lambda: f.read(1 << 16), b''):
      digest.update(chunk)
  return digest.hexdigest()


def remove(root, dest, record):
  '''
  Undo an earlier placement of dest.  Raises PlacementError if dest was a
  package's file and the package can't bring it back.
  '''
  package = record.get('package') if isinstance(record, dict) else None
  slug = record.get('slug') if isinstance(record, dict) else None
  try:
    parent = safe_parent(root, dest, create=False)
  except PlacementError as err:
    if package:
      raise PlacementError(f"unable to restore {dest} from package {package}: {err}") from err
    log.warning(f"not removing {dest}: {err}")
    return
  target = os.path.join(parent, posixpath.basename(dest)) if parent is not None else None
  removed = False
  if target is not None:
    try:
      if not stat.S_ISDIR(os.lstat(target).st_mode):
        os.unlink(target)
        removed = True
    except FileNotFoundError:
      pass
  if not package:
    if removed:
      log.info(f"removed {dest} (file {slug} no longer attached)")
    return
  # the path was the package's before it was placed: with the placed copy
  # gone, a reinstall writes the package's own file, even a noreplace one
  if chroot(root, ['rpm', '-q', package]).returncode != 0:
    log.info(f"removed {dest} (file {slug} no longer attached; package {package} is no longer installed)")
    return
  result = chroot(root, ['dnf', '-y', 'reinstall', package])
  problem = None
  if result.returncode != 0:
    problem = f"dnf reinstall failed ({result.stderr.strip()[-200:] or result.stdout.strip()[-200:]})"
  else:
    expected = package_digests(root, package, dest)
    parent = safe_parent(root, dest, create=False)
    target = os.path.join(parent, posixpath.basename(dest)) if parent is not None else None
    if expected is None:
      problem = f"rpm has no digest for it in {package}"
    elif expected[1] == {''}:
      # no content in the package (a %ghost file): nothing to bring back
      pass
    elif target is None or not os.path.isfile(target) or os.path.islink(target):
      problem = "the reinstall didn't write it"
    elif file_digest(target, expected[0]) not in expected[1]:
      problem = "it doesn't match the package's digest after the reinstall"
  if problem:
    raise PlacementError(f"unable to restore {dest} from package {package} (file {slug} no longer "
                         f"attached): {problem}.  The placed copy has been removed.")
  log.info(f"restored {dest} from package {package} (file {slug} no longer attached)")


def place_files(session, mprov_url, root, config, allow_setuid=False):
  '''
  Place config's managed files into the image at root.  Raises
  PlacementError; nothing is written if any entry fails its checks.
  '''
  if not isinstance(config, dict) or 'files' not in config:
    # an mPCC from before #519
    return
  files = config.get('files') or []
  if not isinstance(files, list):
    raise PlacementError("config.files isn't a list")
  root = os.path.realpath(root)
  for entry in files:
    check(entry, allow_setuid)
  dests = [entry['dest_path'] for entry in files]
  if len(set(dests)) != len(dests):
    raise PlacementError("two managed files have the same destination")
  previous = load_manifest(root)
  manifest = {}
  for entry in files:
    dest = entry['dest_path']
    record = previous.get(dest)
    if isinstance(record, dict):
      package = record.get('package')
    else:
      # whose the path was before anything was placed there, checked without
      # following a link in the image
      parent = safe_parent(root, dest)
      existed = os.path.lexists(os.path.join(parent, posixpath.basename(dest)))
      package = package_of(root, dest) if existed else None
    data = download(session, mprov_url, entry)
    place(root, entry, data)
    manifest[dest] = {'slug': entry['slug'], 'version': entry.get('version'), 'package': package}
    log.info(f"placed {dest} ({entry['slug']} v{entry.get('version')})")
  for dest in sorted(set(previous) - set(manifest)):
    remove(root, dest, previous[dest])
  if manifest or previous:
    save_manifest(root, manifest)
