from .plugin import JobServerPlugin
from time import sleep
class dns_update(JobServerPlugin):
  jobModule = 'dns-update'
