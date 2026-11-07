"""
Bring the container's /etc/mprov up to date with the config it ships (#334).

/etc/mprov is a volume the job server shares with the mPCC (mprov-compose's
mprov-conf).  The template the container renders its config from
(jobserver.yaml.j2) and the plugin configs (plugins/*.yaml) used to reach
it only by Docker seeding the volume, which it does only while the volume is
empty: a job server created after the mPCC had written its secrets there
never started, and an upgrade never brought a new template or plugin
config (mprov-ng/mprov-compose#21).

The image keeps a pristine copy under /usr/share/mprov/config, and
start_server.sh runs this on every start, before rendering.  For each file
it ships:

- missing: installed;
- identical: left alone;
- the same as a version an earlier release shipped (listed in
  shipped-config.sha256), so not edited by the site: replaced;
- edited by the site: a plugin config is kept, and the new default written
  beside it as <name>.yaml.new (which `!include plugins/*.yaml` doesn't
  pick up); the template, which is never a site file, is replaced and the
  site's copy kept as jobserver.yaml.j2.bak.

It also removes the plugin code earlier images copied into
/etc/mprov/plugins (*.py, __pycache__/, the *_mod/ packages): the job
server imports its plugins from the installed package, never from there.

Nothing else in /etc/mprov is touched: the mPCC's secrets, the rendered
jobserver.yaml, and a site's own files (a CA bundle, local.yaml, its own
config) stay as they are.  A failure is logged, never fatal, so this can't
stop a job server that would have started without it.

  python3 -m mprov_jobserver.container_config /usr/share/mprov/config /etc/mprov
"""
import hashlib
import os
import shutil
import stat
import sys
import tempfile

from mprov_jobserver.log import configure as log_configure, get_logger

# by name, not __name__: run with -m, this module is __main__
log = get_logger('mprov_jobserver.container_config')

TEMPLATE = 'jobserver.yaml.j2'
HASHES = 'shipped-config.sha256'


def sha256(data):
  return hashlib.sha256(data).hexdigest()


def released_hashes(src):
  '''
  {path: {sha256, ...}} from shipped-config.sha256: every version of each
  shipped file a release has had.
  '''
  known = {}
  with open(os.path.join(src, HASHES)) as f:
    for line in f:
      line = line.strip()
      if not line or line.startswith('#'):
        continue
      digest, path = line.split(None, 1)
      known.setdefault(path, set()).add(digest)
  return known


def shipped_files(src):
  ''' The paths, relative to src, of the files the container ships. '''
  plugins = sorted(name for name in os.listdir(os.path.join(src, 'plugins')) if name.endswith('.yaml'))
  return [TEMPLATE] + ['plugins/' + name for name in plugins]


def is_regular_file(path):
  ''' True for a regular file; False for one that's missing, a link or anything else. '''
  try:
    return stat.S_ISREG(os.lstat(path).st_mode)
  except FileNotFoundError:
    return False


def write_file(path, data):
  ''' Write data to path through a temporary file beside it, mode 0600. '''
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


def remove_file(path):
  ''' Remove a regular file if there is one; True if it was removed. '''
  if is_regular_file(path):
    os.unlink(path)
    return True
  return False


def refresh_file(src, dest, rel, known):
  ''' Bring dest/rel up to date with src/rel.  Returns what was done. '''
  with open(os.path.join(src, rel), 'rb') as f:
    new = f.read()
  path = os.path.join(dest, rel)
  if not os.path.lexists(path):
    write_file(path, new)
    log.info(f"installed {rel}")
    return 'installed'
  if not is_regular_file(path):
    log.warning(f"{path} isn't a regular file, so it's left as it is.")
    return 'skipped'
  with open(path, 'rb') as f:
    current = f.read()
  if current == new:
    if remove_file(path + '.new'):
      log.info(f"removed {rel}.new: {rel} is the shipped default again")
    return 'current'
  if sha256(current) in known.get(rel, ()):
    write_file(path, new)
    remove_file(path + '.new')
    log.info(f"updated {rel} from an earlier release's")
    return 'updated'
  if rel == TEMPLATE:
    # never a site file: the documented settings are the environment and
    # plugins/*.yaml.  Keeping an edited one would keep it from ever getting
    # a new setting (mprov-ng/mprov-compose#21).
    write_file(path + '.bak', current)
    write_file(path, new)
    log.warning(f"{rel} had local changes; replaced it with the shipped one and kept the old one as {rel}.bak. "
                "Set the job server's settings in the environment or in plugins/local.yaml instead.")
    return 'replaced'
  try:
    with open(path + '.new', 'rb') as f:
      pending = f.read()
  except OSError:
    pending = None
  if pending != new:
    write_file(path + '.new', new)
  log.warning(f"{rel} has local changes, so it's kept; the new default is in {rel}.new.")
  return 'kept'


def remove_plugin_code(plugins):
  '''
  Remove the plugin code earlier images copied into plugins/: *.py files,
  __pycache__/ and the *_mod/ packages once that leaves them empty.  Links
  are never followed.  Returns how many files and directories were removed.
  '''
  removed = 0

  def clean(directory):
    nonlocal removed
    for name in sorted(os.listdir(directory)):
      path = os.path.join(directory, name)
      st = os.lstat(path)
      if stat.S_ISDIR(st.st_mode) and name == '__pycache__':
        shutil.rmtree(path)
        removed += 1
      elif stat.S_ISREG(st.st_mode) and name.endswith('.py'):
        os.unlink(path)
        removed += 1

  clean(plugins)
  for name in sorted(os.listdir(plugins)):
    path = os.path.join(plugins, name)
    if name.endswith('_mod') and stat.S_ISDIR(os.lstat(path).st_mode):
      clean(path)
      if not os.listdir(path):
        os.rmdir(path)
        removed += 1
  return removed


def refresh(src, dest):
  '''
  Bring dest (/etc/mprov) up to date with the config shipped in src.
  Returns {path: what was done}; a file that couldn't be done is 'failed'.
  '''
  known = released_hashes(src)
  plugins = os.path.join(dest, 'plugins')
  if os.path.islink(plugins) or (os.path.lexists(plugins) and not os.path.isdir(plugins)):
    log.error(f"{plugins} isn't a directory, so the plugin configs can't be brought up to date.")
    files = [TEMPLATE]
  else:
    os.makedirs(plugins, exist_ok=True)
    files = shipped_files(src)
  results = {}
  for rel in files:
    try:
      results[rel] = refresh_file(src, dest, rel, known)
    except OSError as err:
      log.error(f"unable to bring {rel} up to date: {err}")
      results[rel] = 'failed'
  if files != [TEMPLATE]:
    try:
      removed = remove_plugin_code(plugins)
      if removed:
        log.info(f"removed {removed} copies of plugin code earlier images left in {plugins}")
    except OSError as err:
      log.error(f"unable to remove the plugin code earlier images left in {plugins}: {err}")
  return results


def main(argv=None):
  argv = sys.argv[1:] if argv is None else argv
  log_configure(os.environ.get('MPROV_LOG_LEVEL') or 'info')
  if len(argv) != 2:
    log.error("usage: python3 -m mprov_jobserver.container_config <shipped config dir> <config dir>")
    return 0
  try:
    refresh(argv[0], argv[1])
  except Exception as err:
    # never stop a start that would have worked without this
    log.error(f"unable to bring {argv[1]} up to date with {argv[0]}: {err}")
  return 0


if __name__ == '__main__':
  sys.exit(main())
