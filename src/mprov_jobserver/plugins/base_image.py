# Fetch, verify and unpack a published OS image as the base of a system
# image (#92), instead of bootstrapping one with the distro's package
# manager.  Nothing here is specific to one distro: the distro's
# image_update_mod sub-module decides when to use it and what to install
# on top.
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
from urllib.parse import urlparse

import requests

from mprov_jobserver.log import get_logger

log = get_logger(__name__)

# written into the image: where its base came from, to notice a changed base
MARKER = '/etc/mprov-base-image'

SHA256 = re.compile(r'[0-9a-fA-F]{64}')
# BSD style, as Rocky's CHECKSUM files have it: SHA256 (file) = hex
BSD_LINE = re.compile(r'SHA256 \((.+)\) = ([0-9a-fA-F]{64})\s*$')
# GNU sha256sum style: hex, two spaces (or space and *), file
GNU_LINE = re.compile(r'([0-9a-fA-F]{64}) [ *](.+?)\s*$')

# OCI architecture names for the mPCC's distArch choices
OCI_ARCH = {'x86_64': 'amd64', 'aarch64': 'arm64', 'ppc64le': 'ppc64le', 's390x': 's390x'}

OCI_INDEX = 'application/vnd.oci.image.index.v1+json'
DOCKER_LIST = 'application/vnd.docker.distribution.manifest.list.v2+json'

# downloads: seconds to connect, and between bytes
TIMEOUT = (30, 300)


class BaseImageError(Exception):
  """ The base image can't be used; the message says why. """


def parse_checksum(text, filename):
  """
  The sha256 for filename in a checksum file's text: BSD lines
  ("SHA256 (file) = hex"), sha256sum lines ("hex  file"), or a file that
  holds nothing but one hex digest.
  """
  bare = text.strip()
  if SHA256.fullmatch(bare):
    return bare.lower()
  for line in text.splitlines():
    line = line.strip()
    if not line or line.startswith('#'):
      continue
    m = BSD_LINE.match(line)
    if m and m.group(1) == filename:
      return m.group(2).lower()
    m = GNU_LINE.match(line)
    if m and m.group(2) == filename:
      return m.group(1).lower()
  raise BaseImageError(f"the checksum file has no SHA256 for {filename}.")


def expected_sha256(checksum, url, get=requests.get):
  """
  The sha256 the image at url must have.  checksum is the distro's
  base_image_checksum: "sha256:<hex>", or the URL of a checksum file that
  lists the image by its file name.
  """
  checksum = (checksum or '').strip()
  if not checksum:
    raise BaseImageError("the OS distro has a base image URL but no base image checksum.")
  if checksum.lower().startswith('sha256:'):
    digest = checksum[len('sha256:'):]
    if not SHA256.fullmatch(digest):
      raise BaseImageError(f"the base image checksum {checksum!r} isn't sha256: and 64 hex digits.")
    return digest.lower()
  if urlparse(checksum).scheme not in ('http', 'https'):
    raise BaseImageError(f"the base image checksum {checksum!r} is neither sha256:<hex> nor an http(s) URL.")
  if urlparse(checksum).scheme == 'http':
    log.warning(f"the base image checksum comes over plain http ({checksum}), so it only catches a damaged download.")
  try:
    response = get(checksum, timeout=TIMEOUT)
  except requests.RequestException as err:
    raise BaseImageError(f"unable to fetch the base image checksum {checksum}: {err}")
  if response.status_code != 200:
    raise BaseImageError(f"unable to fetch the base image checksum {checksum}: HTTP {response.status_code}")
  return parse_checksum(response.text, os.path.basename(urlparse(url).path))


def download(url, dest, get=requests.get):
  """ Download url to the file dest; returns its sha256. """
  if urlparse(url).scheme not in ('http', 'https'):
    raise BaseImageError(f"the base image URL {url!r} isn't an http(s) URL.")
  digest = hashlib.sha256()
  log.info(f"Downloading the base image {url}")
  # a plain request: the session's mPCC credentials mustn't go to another host
  try:
    with get(url, stream=True, timeout=TIMEOUT) as response:
      if response.status_code != 200:
        raise BaseImageError(f"unable to download the base image {url}: HTTP {response.status_code}")
      with open(dest, 'wb') as out:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
          digest.update(chunk)
          out.write(chunk)
  except (requests.RequestException, OSError) as err:
    raise BaseImageError(f"unable to download the base image {url}: {err}")
  return digest.hexdigest()


def _sha256_of(path):
  digest = hashlib.sha256()
  with open(path, 'rb') as f:
    for chunk in iter(lambda: f.read(1024 * 1024), b''):
      digest.update(chunk)
  return digest.hexdigest()


def _clean_name(name):
  """
  A member name as a relative path inside the tree, or None for the root
  itself.  Refuses names that climb out of the tree.
  """
  parts = []
  for part in name.split('/'):
    if part in ('', '.'):
      continue
    if part == '..':
      raise BaseImageError(f"the base image has a path that leaves the image: {name!r}")
    parts.append(part)
  return '/'.join(parts) or None


def _check_parents(root, rel):
  """
  Refuse a path whose parent directories in the tree include a symlink: as
  root, writing or deleting through one could reach the build host (e.g. an
  earlier member "etc -> /etc", then "etc/passwd").
  """
  path = root
  for part in rel.split('/')[:-1]:
    path = os.path.join(path, part)
    try:
      st = os.lstat(path)
    except FileNotFoundError:
      return
    if stat.S_ISLNK(st.st_mode):
      raise BaseImageError(f"the base image writes through a symlink at {path[len(root):]!r}")


def _remove(path):
  try:
    st = os.lstat(path)
  except FileNotFoundError:
    return
  if stat.S_ISDIR(st.st_mode):
    shutil.rmtree(path)
  else:
    os.unlink(path)


def apply_layer(layer, root):
  """
  Unpack one filesystem layer (a tar file, compressed or not) into root.
  OCI whiteouts in it delete what earlier layers put there: ".wh.<name>"
  removes <name>, ".wh..wh..opq" empties its directory.  Owners, modes
  (setuid included) and device nodes are kept, as a root filesystem needs.
  """
  with tarfile.open(layer, 'r:*') as tar:
    members = tar.getmembers()
    # whiteouts only apply to earlier layers, so do them all before this
    # layer's own files go in
    for member in members:
      rel = _clean_name(member.name)
      if rel is None:
        continue
      base = os.path.basename(rel)
      if not base.startswith('.wh.'):
        continue
      _check_parents(root, rel)
      parent = os.path.join(root, os.path.dirname(rel))
      if base == '.wh..wh..opq':
        if os.path.isdir(parent) and not os.path.islink(parent):
          for entry in os.listdir(parent):
            _remove(os.path.join(parent, entry))
      else:
        _remove(os.path.join(parent, base[len('.wh.'):]))
    for member in members:
      rel = _clean_name(member.name)
      if rel is None or os.path.basename(rel).startswith('.wh.'):
        continue
      _check_parents(root, rel)
      if member.islnk():
        target = _clean_name(member.linkname)
        if target is None:
          raise BaseImageError(f"the base image hard-links {member.name!r} to the image root.")
        _check_parents(root, target + '/x')
        member.linkname = target
      member.name = rel
      dest = os.path.join(root, rel)
      # a file over a directory, or the other way round, from a lower layer
      try:
        st = os.lstat(dest)
        if not (member.isdir() and stat.S_ISDIR(st.st_mode)):
          _remove(dest)
      except FileNotFoundError:
        pass
      kwargs = {'set_attrs': True, 'numeric_owner': True}
      if hasattr(tarfile, 'fully_trusted_filter'):
        # the checks above replace the filters, whose "tar" and "data"
        # settings would strip setuid bits and device nodes
        kwargs['filter'] = 'fully_trusted'
      tar.extract(member, root, **kwargs)


def _oci_blob(layout, digest):
  algo, _, value = (digest or '').partition(':')
  if algo != 'sha256' or not SHA256.fullmatch(value):
    raise BaseImageError(f"the base image's OCI layout names a blob {digest!r} that isn't sha256.")
  path = os.path.join(layout, 'blobs', 'sha256', value.lower())
  if not os.path.isfile(path) or os.path.islink(path):
    raise BaseImageError(f"the base image's OCI layout is missing blob {digest}.")
  if _sha256_of(path) != value.lower():
    raise BaseImageError(f"the base image's OCI blob {digest} doesn't match its digest.")
  return path


def _oci_manifest(layout, descriptor, arch):
  """ Follow image indexes down to the manifest for linux/arch. """
  for _ in range(4):
    with open(_oci_blob(layout, descriptor.get('digest'))) as f:
      doc = json.load(f)
    if doc.get('mediaType', descriptor.get('mediaType')) not in (OCI_INDEX, DOCKER_LIST) and 'layers' in doc:
      return doc
    descriptor = _pick_manifest(doc.get('manifests') or [], arch)
  raise BaseImageError("the base image's OCI indexes nest too deeply.")


def _pick_manifest(manifests, arch):
  if not manifests:
    raise BaseImageError("the base image's OCI index lists no manifests.")
  if len(manifests) == 1 and 'platform' not in manifests[0]:
    return manifests[0]
  want = OCI_ARCH.get(arch, arch)
  for descriptor in manifests:
    platform = descriptor.get('platform') or {}
    if platform.get('os', 'linux') == 'linux' and platform.get('architecture') == want:
      return descriptor
  raise BaseImageError(f"the base image has no manifest for linux/{want}.")


def unpack_oci(layout, root, arch):
  """ Unpack an OCI image layout directory's layers, in order, into root. """
  try:
    with open(os.path.join(layout, 'index.json')) as f:
      index = json.load(f)
    manifest = _oci_manifest(layout, _pick_manifest(index.get('manifests') or [], arch), arch)
  except (OSError, ValueError) as err:
    raise BaseImageError(f"unable to read the base image's OCI layout: {err}")
  layers = manifest.get('layers') or []
  if not layers:
    raise BaseImageError("the base image's OCI manifest has no layers.")
  for descriptor in layers:
    if 'zstd' in (descriptor.get('mediaType') or ''):
      raise BaseImageError("the base image has a zstd-compressed layer, which isn't supported.")
    apply_layer(_oci_blob(layout, descriptor.get('digest')), root)


def _is_oci_archive(archive):
  with tarfile.open(archive, 'r:*') as tar:
    for name in tar.getnames():
      if _clean_name(name) == 'oci-layout':
        return True
  return False


def _extract_layout(archive, dest):
  """ Unpack an OCI archive's own files (blobs and JSON, nothing else) into dest. """
  with tarfile.open(archive, 'r:*') as tar:
    for member in tar.getmembers():
      rel = _clean_name(member.name)
      if rel is None:
        continue
      if not (member.isfile() or member.isdir()):
        raise BaseImageError(f"the base image's OCI archive has a {member.name!r} that isn't a file or directory.")
      member.name = rel
      kwargs = {}
      if hasattr(tarfile, 'data_filter'):
        kwargs['filter'] = 'data'
      tar.extract(member, dest, set_attrs=False, **kwargs)


def unpack(archive, root, workdir, arch='x86_64'):
  """
  Unpack a base image archive into root: an OCI image archive (index.json
  and blobs/, as Rocky's Container-Base images are) or a plain root
  filesystem tarball.  workdir is scratch space outside root.
  """
  try:
    oci = _is_oci_archive(archive)
  except (tarfile.TarError, OSError) as err:
    raise BaseImageError(f"the base image isn't a tar archive this job server can read: {err}")
  try:
    os.makedirs(root, exist_ok=True)
    if oci:
      layout = os.path.join(workdir, 'layout')
      os.makedirs(layout, exist_ok=True)
      _extract_layout(archive, layout)
      unpack_oci(layout, root, arch)
    else:
      apply_layer(archive, root)
  except (tarfile.TarError, OSError) as err:
    raise BaseImageError(f"unable to unpack the base image: {err}")


def reset_identity(root):
  """
  Leave nothing that would make every node built from the image the same
  machine, or mount the base's own disks: an empty machine-id (systemd makes
  one at boot) and an fstab with no entries.
  """
  etc = os.path.join(root, 'etc')
  os.makedirs(etc, exist_ok=True)
  for name, text in (('machine-id', ''),
                     ('fstab', '# Written by mProv: the base image\'s entries are removed.\n')):
    path = os.path.join(etc, name)
    if os.path.islink(path):
      os.unlink(path)
    with open(path, 'w') as f:
      f.write(text)
    os.chmod(path, 0o444 if name == 'machine-id' else 0o644)


def read_marker(root):
  """ What write_marker recorded in this tree, or None (a tree not built from a base image). """
  try:
    with open(root + MARKER) as f:
      return json.load(f)
  except (OSError, ValueError):
    return None


def write_marker(root, url, sha256):
  with open(root + MARKER, 'w') as f:
    json.dump({'url': url, 'sha256': sha256}, f)
    f.write('\n')
