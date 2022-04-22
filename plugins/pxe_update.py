from .plugin import JobServerPlugin
from time import sleep
class pxe_update(JobServerPlugin):
  jobModule = 'pxe-update'
