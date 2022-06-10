from setuptools import setup
import os, sys
if __name__ == '__main__':
    version=""
    if "MPJS_VERSION" in os.environ:
      version = os.environ['MPJS_VERSION']
    else:
      print(f"Error: Please specify your build version via MPJS_VERSION environment variable.")
      sys.exit(1)
    setup(version=version)