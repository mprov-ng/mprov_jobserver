# Fetch, verify and unpack a published OS image as the base of a system
# image (#92), instead of bootstrapping one with the distro's package
# manager: a container image or root filesystem tarball, or a cloud disk
# image (qcow2 or raw, #279).  Nothing here is specific to one distro: the
# distro's image_update_mod sub-module decides when to use it and what to
# install on top.
import glob
import hashlib
import json
import lzma
import os
import re
import shutil
import stat
import subprocess
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

# Disk images are read with libguestfs, in its own small VM: the host
# kernel never mounts the image's filesystems, and LVM inside the image
# stays inside that VM.  Its appliance is built on first use and cached
# here, which the container has on the host, so it outlives restarts.
GUESTFS_CACHE = '/var/lib/mprov/guestfs'
# seconds for guestfish to copy a disk image's files out; without KVM it
# runs under emulation, which is slow
GUESTFS_TIMEOUT = 2 * 60 * 60

QCOW2_MAGIC = b'QFI\xfb'
XZ_MAGIC = b'\xfd7zXZ\x00'


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


def _is_disk(head):
  """ Whether these first bytes of a file start a raw disk or filesystem. """
  return (head[510:512] == b'\x55\xaa'          # MBR (and GPT's protective MBR)
          or head[512:520] == b'EFI PART'         # GPT
          or head[:4] == b'XFSB'                  # an XFS filesystem
          or head[1080:1082] == b'\x53\xef')     # an ext2/3/4 filesystem


# compressed tarballs (and xz on its own): gzip, bzip2, xz
COMPRESSED = (b'\x1f\x8b', b'BZh', XZ_MAGIC)


def detect_format(path):
  """
  What the downloaded base image is: 'qcow2', 'tar' (a tarball, compressed
  or not), 'xz' (something else xz compressed: decompress it and look
  again) or 'raw' (a disk or a bare filesystem).  Read from its contents,
  not its URL, which needn't end in the format's suffix.  Anything else is
  taken for a tarball, so that unpacking it says why it can't be read.

  Compressed data is told apart by its magic before the disk signatures
  are looked for, as compressed bytes could happen to contain one.  A disk
  is looked for before asking tarfile, which takes a file that starts with
  a block of zeros (a GPT disk without boot code, a bare ext4 filesystem)
  for an empty tarball.
  """
  with open(path, 'rb') as f:
    head = f.read(4096)
  if head.startswith(QCOW2_MAGIC):
    return 'qcow2'
  if head.startswith(XZ_MAGIC):
    # the same questions of what's inside: a tarball, or a disk to decompress
    try:
      with lzma.open(path) as f:
        inner = f.read(4096)
    except (lzma.LZMAError, EOFError, OSError):
      return 'tar'
    if inner[257:262] != b'ustar' and (inner.startswith(QCOW2_MAGIC) or _is_disk(inner)):
      return 'xz'
    return 'tar'
  if head.startswith(COMPRESSED):
    return 'tar'
  if head[257:262] == b'ustar':
    return 'tar'
  if _is_disk(head):
    return 'raw'
  return 'tar'


def _unxz(path, dest):
  try:
    with lzma.open(path) as src, open(dest, 'wb') as out:
      shutil.copyfileobj(src, out, 1024 * 1024)
  except (lzma.LZMAError, EOFError) as err:
    raise BaseImageError(f"unable to decompress the base image: {err}")


def _disk_in_tar(archive):
  """
  The member name if the tarball holds just one disk image (Debian ships its
  cloud images as a tar.xz of disk.raw), else None: a root filesystem
  tarball has more than one file.
  """
  with tarfile.open(archive, 'r:*') as tar:
    files = []
    for member in tar:
      if member.isfile():
        files.append(member)
      elif not member.isdir():
        return None
      if len(files) > 1:
        return None
    if len(files) != 1 or not files[0].name.endswith(('.raw', '.img', '.qcow2')):
      return None
    head = tar.extractfile(files[0]).read(4096)
  if head.startswith(QCOW2_MAGIC) or _is_disk(head):
    return files[0].name
  return None


def _extract_member(archive, name, dest):
  """ Write one member of the tarball to dest, leaving its runs of zeros as holes. """
  chunk = 1024 * 1024
  zeros = bytes(chunk)
  with tarfile.open(archive, 'r:*') as tar, open(dest, 'wb') as out:
    src = tar.extractfile(name)
    while True:
      data = src.read(chunk)
      if not data:
        break
      if data == zeros[:len(data)]:
        out.seek(len(data), os.SEEK_CUR)
      else:
        out.write(data)
    out.truncate()


def disk_to_tar(image, fmt, dest, run=subprocess.run):
  """
  Copy the root filesystem out of a disk image (fmt 'qcow2' or 'raw') into
  the tarball dest, with libguestfs: guestfish opens the image read-only,
  finds the OS on it (its partitions, LVM and mount points) and tars up /.
  Owners are kept as numbers: the appliance's own user names would map
  them wrongly.
  """
  guestfish = shutil.which('guestfish')
  if guestfish is None:
    raise BaseImageError("the base image is a disk image, and reading one needs libguestfs's guestfish, which isn't installed (dnf install guestfs-tools).")
  env = dict(os.environ)
  # run qemu directly, not through libvirt, which the container hasn't got
  env.setdefault('LIBGUESTFS_BACKEND', 'direct')
  env.setdefault('LIBGUESTFS_CACHEDIR', GUESTFS_CACHE)
  try:
    os.makedirs(env['LIBGUESTFS_CACHEDIR'], exist_ok=True)
  except OSError as err:
    raise BaseImageError(f"unable to make libguestfs's cache directory {env['LIBGUESTFS_CACHEDIR']}: {err}")
  if not os.access('/dev/kvm', os.R_OK | os.W_OK):
    log.warning("this job server has no /dev/kvm, so reading the disk image runs under emulation and is slow.")
  log.info(f"Copying the base image's files out of the {fmt} disk image with libguestfs")
  try:
    result = run([guestfish, '--ro', f'--format={fmt}', '-a', image, '-i',
                  'tar-out', '/', dest, 'numericowner:true'],
                 env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                 timeout=GUESTFS_TIMEOUT)
  except subprocess.TimeoutExpired:
    raise BaseImageError(f"libguestfs took over {GUESTFS_TIMEOUT} seconds to read the disk image.")
  except OSError as err:
    raise BaseImageError(f"unable to run guestfish: {err}")
  if result.returncode != 0:
    detail = ' '.join((result.stderr or '').strip().splitlines()[-3:])
    raise BaseImageError(f"libguestfs couldn't read the disk image: {detail or 'exit status ' + str(result.returncode)}")


def unpack(archive, root, workdir, arch='x86_64', run=subprocess.run):
  """
  Unpack a base image into root: an OCI image archive (index.json and
  blobs/, as Rocky's Container-Base images are), a plain root filesystem
  tarball, or a cloud disk image (qcow2, or raw, either of them perhaps xz
  compressed or alone in a tarball).  A disk image's files are copied out
  as a tarball, which then unpacks with the same checks as any other.
  workdir is scratch space outside root.
  """
  try:
    fmt = detect_format(archive)
    if fmt == 'xz':
      _unxz(archive, os.path.join(workdir, 'decompressed'))
      archive = os.path.join(workdir, 'decompressed')
      fmt = detect_format(archive)
      if fmt == 'xz':
        raise BaseImageError("the base image is xz compressed twice.")
    if fmt == 'tar':
      member = _disk_in_tar(archive)
      if member is not None:
        disk = os.path.join(workdir, 'disk')
        _extract_member(archive, member, disk)
        archive, fmt = disk, detect_format(disk)
    if fmt in ('qcow2', 'raw'):
      rootfs = os.path.join(workdir, 'rootfs.tar')
      disk_to_tar(archive, fmt, rootfs, run=run)
      archive = rootfs
    oci = _is_oci_archive(archive)
  except (tarfile.TarError, OSError, EOFError) as err:
    raise BaseImageError(f"the base image isn't an archive or disk image this job server can read: {err}")
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


def clean_unpacked(root):
  """
  Remove what a cloud disk image brings that belongs to the machine it was
  installed on, not to an image: its boot menu (grub's config and the boot
  loader entries for its kernel, which mProv's own kernel replaces, and its
  rescue kernel), its host name, which would override the one each node
  gets, and any SSH host keys, which every node would share.  Run once, straight after unpacking,
  before mProv installs anything.  Returns the paths removed.
  """
  removed = []
  patterns = ('boot/loader/entries/*', 'boot/grub2/grub.cfg', 'boot/efi/EFI/*/grub.cfg',
              # the rescue kernel belongs to no package, so removing the
              # base's kernel packages leaves it
              'boot/vmlinuz-0-rescue-*', 'boot/initramfs-0-rescue-*',
              'etc/hostname', 'etc/ssh/ssh_host_*')
  for pattern in patterns:
    for path in sorted(glob.glob(os.path.join(root, pattern))):
      rel = os.path.relpath(path, root)
      # never through a linked directory ("boot -> /boot" would reach the
      # build host's); a link itself is removed, not what it points to
      _check_parents(root, rel)
      if os.path.isdir(path) and not os.path.islink(path):
        continue
      os.unlink(path)
      removed.append('/' + rel)
  return removed


def unmask_units(root):
  """
  Remove the systemd masks (links to /dev/null in /etc/systemd/system) a
  base image comes with.  A container image masks what a container has no
  use for, Rocky's systemd-logind, getty.target and console-getty among
  them; on a node that leaves no console login and no login sessions.  A
  freshly installed OS has no masks there, so all of them go.  Returns the
  unit names unmasked.
  """
  unitDir = os.path.join(root, 'etc/systemd/system')
  unmasked = []
  if not os.path.isdir(unitDir) or os.path.islink(unitDir):
    return unmasked
  for name in sorted(os.listdir(unitDir)):
    path = os.path.join(unitDir, name)
    if os.path.islink(path) and os.readlink(path) == '/dev/null':
      os.unlink(path)
      unmasked.append(name)
  return unmasked


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
