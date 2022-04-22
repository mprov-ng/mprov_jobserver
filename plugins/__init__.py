# from .dhcp_delete import dhcp_delete
# from .dhcp_update import dhcp_update
# from .dns_delete import dns_delete
# from .dns_update import dns_update
# from .image_delete import image_delete
# from .image_update import image_update
# from .pxe_delete import pxe_delete
# from .pxe_update import pxe_update
# from .repo_delete import repo_delete
# from .repo_update import repo_update
from os.path import dirname, basename, isfile, join
import glob
modules = glob.glob(join(dirname(__file__), "*.py"))
__all__ = [ basename(f)[:-3] for f in modules if isfile(f) and not f.endswith('__init__.py')]
from . import *