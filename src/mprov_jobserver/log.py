'''
The job server's log (#104, #103).  Every line is

  [E] image-update: unable to create initramfs

with [E]rror, [W]arning, [I]nfo or [D]ebug and the module it came from, so
docker logs, plain files and Loki line filters can pick out problems.  When
stdout is journald's (the systemd unit), each line also starts with the
sd-daemon priority prefix (<3>, <4>, <6>, <7>), which journald strips and
records as the line's PRIORITY, so `journalctl -p warning` works.

Lines go to stdout, flushed as they're written: block-buffered output under
systemd or docker arrived late and out of order with the output of the
commands the job server runs (dnf, wget, make), which shares stdout.
'''
import logging
import os
import sys
import time

TAGS = {logging.ERROR: 'E', logging.WARNING: 'W', logging.INFO: 'I', logging.DEBUG: 'D'}
# sd-daemon(3) priorities
PRIORITIES = {logging.ERROR: 3, logging.WARNING: 4, logging.INFO: 6, logging.DEBUG: 7}
ROOT = 'mprov_jobserver'


def journald_stdout():
  '''
  True if stdout is the journal stream systemd gave us: JOURNAL_STREAM holds
  its "device:inode", which a child's or a redirected stdout won't match.
  '''
  stream = os.environ.get('JOURNAL_STREAM', '')
  try:
    dev, ino = (int(part) for part in stream.split(':'))
    st = os.fstat(sys.stdout.fileno())
  except (ValueError, OSError, AttributeError):
    return False
  return (st.st_dev, st.st_ino) == (dev, ino)


def module_name(name):
  '''
  The short module name for a logger: mprov_jobserver.plugins.image_update
  is "image-update", plugins.image_update_mod.rhel "image-update/rhel",
  mprov_jobserver.app "app".
  '''
  parts = name.split('.')
  if parts[0] == ROOT:
    parts = parts[1:]
  if parts and parts[0] == 'plugins':
    parts = parts[1:]
  if not parts:
    return 'jobserver'
  if len(parts) > 1 and parts[0].endswith('_mod'):
    parts[0] = parts[0][:-len('_mod')]
  return '/'.join(part.replace('_', '-') for part in parts)


class Formatter(logging.Formatter):
  journald = False
  timestamps = False

  def format(self, record):
    level = record.levelno
    if level >= logging.ERROR:
      level = logging.ERROR
    elif level not in TAGS:
      level = logging.DEBUG if level < logging.INFO else logging.INFO
    message = record.getMessage().lstrip('\n')
    if record.exc_info:
      message += '\n' + self.formatException(record.exc_info)
    line = f"[{TAGS[level]}] {module_name(record.name)}: {message}"
    if self.timestamps:
      line = time.strftime('%Y-%m-%d %H:%M:%S ', time.localtime(record.created)) + line
    if self.journald:
      line = f"<{PRIORITIES[level]}>{line}"
    return line


class StdoutHandler(logging.StreamHandler):
  ''' Writes to whatever sys.stdout is when a line is logged, and flushes. '''

  def __init__(self):
    super().__init__()

  @property
  def stream(self):
    return sys.stdout

  @stream.setter
  def stream(self, value):
    pass


_formatter = Formatter()
_handler = StdoutHandler()
_handler.setFormatter(_formatter)
_root = logging.getLogger(ROOT)
_root.addHandler(_handler)
_root.setLevel(logging.INFO)
_root.propagate = False


def configure(level='info', timestamps=False):
  '''
  Set the level (error, warning, info or debug; the global logLevel) and
  whether lines carry a timestamp (the global logTimestamps).  journald
  and docker stamp lines themselves.  False for a level it doesn't know.
  '''
  _formatter.journald = journald_stdout()
  _formatter.timestamps = bool(timestamps)
  value = logging.getLevelName(str(level).upper())
  if not isinstance(value, int):
    return False
  _root.setLevel(value)
  return True


def get_logger(name):
  ''' The logger for a module: get_logger(__name__). '''
  if name == '__main__' or not name.startswith(ROOT):
    name = ROOT + '.' + name.rsplit('.', 1)[-1]
  return logging.getLogger(name)
