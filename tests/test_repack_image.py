"""
repack_image must not destroy the image it repacks (#177).

The script invokes ``busybox tar``; the tests put a shim named ``busybox``
first on PATH so they run on hosts without busybox installed.  One shim
forwards to the real tar (success path), the other always fails (failure
path).  The failure path is the regression: the old script deleted the
existing image before tarring and masked tar's exit status through the
gzip pipeline, so a broken invocation replaced a good image with garbage.
"""
import os
import pathlib
import subprocess
import tarfile

# Bound at import, before the no_processes fixture stubs subprocess.run out
# for the plugins (same convention as tests/tools.py).
_run = subprocess.run

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "repack_image"


def _make_image_dir(tmp_path):
  """An image directory holding one payload file, plus an existing image."""
  image_dir = tmp_path / "myimage"
  image_dir.mkdir()
  (image_dir / "payload.txt").write_text("hello")
  (image_dir / "myimage.img").write_bytes(b"OLD-IMAGE")
  return image_dir


def _shim_env(tmp_path, tar_fails):
  """env dict with a fake ``busybox`` on PATH.

  With ``tar_fails`` the shim exits 1 for any subcommand; otherwise
  ``busybox tar ...`` execs the real tar.
  """
  bin_dir = tmp_path / "bin"
  bin_dir.mkdir()
  shim = bin_dir / "busybox"
  if tar_fails:
    shim.write_text("#!/bin/sh\nexit 1\n")
  else:
    shim.write_text('#!/bin/sh\n[ "$1" = "tar" ] || exit 127\nshift\nexec tar "$@"\n')
  shim.chmod(0o755)
  env = os.environ.copy()
  env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
  return env


def _run_script(image_dir, env):
  return _run(
    ["bash", str(SCRIPT), str(image_dir)],
    env=env, capture_output=True, text=True)


def test_repack_builds_valid_image(tmp_path):
  """A successful repack writes a gzip'd tar of the directory contents, and
  only those: the old image is swapped out, never swept into the archive."""
  image_dir = _make_image_dir(tmp_path)
  result = _run_script(image_dir, _shim_env(tmp_path, tar_fails=False))
  assert result.returncode == 0, result.stderr

  image = image_dir / "myimage.img"
  assert image.is_file()
  with tarfile.open(image, mode="r:gz") as archive:
    members = {pathlib.Path(m.name).name for m in archive.getmembers()}
  assert "payload.txt" in members
  assert "myimage.img" not in members


def test_failed_repack_preserves_existing_image(tmp_path):
  """When tar fails, the script exits non-zero and leaves the image alone."""
  image_dir = _make_image_dir(tmp_path)
  image = image_dir / "myimage.img"

  result = _run_script(image_dir, _shim_env(tmp_path, tar_fails=True))
  assert result.returncode != 0
  assert image.read_bytes() == b"OLD-IMAGE"
