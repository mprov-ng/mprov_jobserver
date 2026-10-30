"""
image_sync keeps a copy of every image (or the configured imageList) on
this job server: it downloads an image whose version differs from the
mPCC's, adds itself to the image's job servers, and removes directories for
images it no longer serves.
"""
import json

import pytest

from mprov_jobserver.plugins.image_sync import image_sync

# Name of the ownership marker file mprov writes into the image
# directories it creates; the sweep may only delete marked directories (#171).
MARKER = ".mprov-managed"


def image(slug="compute", version=3, jobservers=()):
  return {"slug": slug, "version": version, "jobservers": list(jobservers)}


def mark_owned(d):
  """Create a directory the way image_sync creates its own: with a marker."""
  d.mkdir(parents=True)
  (d / MARKER).write_text("mprov owns this directory\n")


@pytest.fixture
def images(tmp_path):
  return tmp_path / "images"


@pytest.fixture
def plugin(js, images):
  js.jobmodules = ["image-sync", "mprov-webserver"]
  plugin = image_sync(js)
  plugin.imageDir = str(images)
  return plugin


def serve_files(js, slug="compute"):
  js.session.routes.update({
    f"images/{slug}": b"IMG",
    f"images/{slug}.initramfs": b"INITRD",
    f"images/{slug}.vmlinuz": b"KERNEL",
  })


def versions(images):
  return json.loads((images / "image-versions").read_text())


def test_downloads_a_new_image_and_adds_itself(js, plugin, images):
  js.session.routes.update({"images/": [{"slug": "compute"}], "systemimages/compute/": image(jobservers=[7])})
  serve_files(js)
  plugin.handle_jobs()
  assert (images / "compute" / "compute.img").read_bytes() == b"IMG"
  assert (images / "compute" / "compute.initramfs").read_bytes() == b"INITRD"
  assert (images / "compute" / "compute.vmlinuz").read_bytes() == b"KERNEL"
  assert versions(images) == {"compute": 3}
  assert js.session.writes == [
    ("PATCH", "systemimages/compute/?addjs", {"slug": "compute", "jobservers": [7, 1]}),
  ]


def test_adding_itself_keeps_a_requested_rebuild(js, plugin, images):
  # the PATCH sent needs_rebuild: false, so syncing an image cleared any
  # rebuild asked for on it (#226)
  js.session.routes.update({"images/": [{"slug": "compute"}], "systemimages/compute/": dict(image(jobservers=[7]), needs_rebuild=True)})
  serve_files(js)
  plugin.handle_jobs()
  [(method, path, data)] = js.session.writes
  assert path == "systemimages/compute/?addjs"
  assert "needs_rebuild" not in data


def test_current_image_isnt_downloaded_again(js, plugin, images):
  images.mkdir()
  (images / "image-versions").write_text(json.dumps({"compute": 3}))
  (images / "compute").mkdir()
  js.session.routes.update({"images/": [{"slug": "compute"}], "systemimages/compute/": image(jobservers=[1])})
  plugin.handle_jobs()
  assert ("GET", "images/compute") not in js.session.requests
  # already listed as a host, so no PATCH
  assert js.session.writes == []


def test_image_list_limits_the_images(js, plugin, images):
  plugin.imageList = ["compute"]
  js.session.routes["systemimages/compute/"] = image(jobservers=[1])
  serve_files(js)
  plugin.handle_jobs()
  assert ("GET", "images/") not in js.session.requests
  assert (images / "compute" / "compute.img").exists()


def test_unknown_image_directories_are_removed(js, plugin, images):
  mark_owned(images / "old-image")
  (images / "old-image" / "old-image.img").write_bytes(b"x")
  js.session.routes["images/"] = []
  plugin.handle_jobs()
  assert not (images / "old-image").exists()
  assert (images / "image-versions").exists()


def test_sweep_never_touches_unmarked_directories(js, plugin, images):
  """#171: a directory mprov did not create must never be deleted."""
  (images / "operator-data").mkdir(parents=True)
  (images / "operator-data" / "precious").write_text("keep")
  mark_owned(images / "old-image")
  js.session.routes["images/"] = []
  plugin.handle_jobs()
  assert (images / "operator-data" / "precious").read_text() == "keep"
  assert not (images / "old-image").exists()


def test_downloaded_image_directory_carries_the_mprov_marker(js, plugin, images):
  js.session.routes.update({"images/": [{"slug": "compute"}], "systemimages/compute/": image(jobservers=[1])})
  serve_files(js)
  plugin.handle_jobs()
  assert (images / "compute" / MARKER).exists()


def test_load_config_refuses_image_dir_outside_the_data_root(js, tmp_path):
  js.jobmodules = ["image-sync", "mprov-webserver"]
  js.config_data = {"image-sync": {
    "dataRoot": str(tmp_path / "mprov"),
    "imageDir": str(tmp_path / "elsewhere"),
  }}
  with pytest.raises(SystemExit):
    image_sync(js).load_config()


def test_load_config_refuses_without_a_data_root(js, tmp_path):
  js.jobmodules = ["image-sync", "mprov-webserver"]
  js.config_data = {"image-sync": {"imageDir": str(tmp_path / "images")}}
  with pytest.raises(SystemExit):
    image_sync(js).load_config()


def test_load_config_accepts_image_dir_inside_the_data_root(js, tmp_path):
  js.jobmodules = ["image-sync", "mprov-webserver"]
  root = tmp_path / "mprov"
  js.config_data = {"image-sync": {
    "dataRoot": str(root),
    "imageDir": str(root / "images"),
  }}
  plugin = image_sync(js)
  assert plugin.load_config() is True
  assert plugin.imageDir == str(root / "images")


def test_corrupt_versions_file_redownloads(js, plugin, images):
  images.mkdir()
  (images / "image-versions").write_text("{not json")
  js.session.routes.update({"images/": [{"slug": "compute"}], "systemimages/compute/": image(jobservers=[1])})
  serve_files(js)
  plugin.handle_jobs()
  assert versions(images) == {"compute": 3}


def test_needs_the_webserver(js):
  js.jobmodules = ["image-sync"]
  with pytest.raises(SystemExit):
    image_sync(js).load_config()


@pytest.mark.xfail(strict=True, raises=TypeError,
                   reason="image_sync compares status_code with the string \"404\", so a deleted image crashes the sync (#141)")
def test_image_deleted_from_the_mpcc_is_skipped(js, plugin, images):
  plugin.imageList = ["gone", "compute"]
  js.session.routes.update({"systemimages/gone/": (404, None), "systemimages/compute/": image(jobservers=[1])})
  serve_files(js)
  plugin.handle_jobs()
  assert (images / "compute" / "compute.img").exists()


def test_configured_image_list_is_made_of_strings(js, tmp_path):
  # compared with directory names by the sweep, like repo_sync's (#235)
  js.jobmodules = ["image-sync", "mprov-webserver"]
  root = tmp_path / "mprov"
  js.config_data = {"image-sync": {"dataRoot": str(root), "imageDir": str(root / "images"), "imageList": ["compute", 2024]}}
  plugin = image_sync(js)
  assert plugin.load_config() is True
  assert plugin.imageList == ["compute", "2024"]
