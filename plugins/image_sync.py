from .plugin import JobServerPlugin
from time import sleep
class image_sync(JobServerPlugin):
  jobModule = 'image-sync'