"""
image_delete removes an image's directory when the mPCC hands this job
server an image-delete job for it.
"""
import pytest

from mprov_jobserver.plugins.image_delete import image_delete

JOBS = "jobs/?jobserver=1&module=image-delete&status=2"


@pytest.fixture
def images(tmp_path):
  (tmp_path / "compute").mkdir()
  (tmp_path / "compute" / "compute.img").write_bytes(b"img")
  (tmp_path / "login").mkdir()
  return tmp_path


@pytest.fixture
def plugin(js, images):
  plugin = image_delete(js)
  plugin.imageDir = str(images)
  return plugin


def statuses(js):
  return [(status, jobid) for _, status, jobid in js.job_updates_by_id if jobid is not None]


def test_deletes_the_image_directory(js, plugin, images):
  js.session.routes[JOBS] = [{"id": 3, "params": {"imageId": "compute"}}]
  plugin.handle_jobs()
  assert not (images / "compute").exists()
  assert (images / "login").exists()
  assert statuses(js) == [(4, 3)]
  # it claimed only this job server's pending image-delete jobs
  assert js.job_updates[0] == ("image-delete", 2, "jobserver=1&module=image-delete&status=1")


def test_no_pending_jobs_does_nothing(js, plugin, images):
  js.has_jobs = False
  plugin.handle_jobs()
  assert (images / "compute").exists()
  assert js.session.requests == []


def test_missing_image_directory_fails_the_job(js, plugin):
  js.session.routes[JOBS] = [{"id": 3, "params": {"imageId": "gone"}}]
  plugin.handle_jobs()
  assert statuses(js) == [(3, 3)]


def test_missing_image_dir_fails_the_job(js, plugin, tmp_path):
  plugin.imageDir = str(tmp_path / "nowhere")
  js.session.routes[JOBS] = [{"id": 3, "params": {"imageId": "compute"}}]
  plugin.handle_jobs()
  assert statuses(js) == [(3, 3)]


def test_image_not_in_the_image_list_is_refused(js, plugin, images):
  plugin.imageList = ["login"]
  js.session.routes[JOBS] = [{"id": 3, "params": {"imageId": "compute"}}]
  plugin.handle_jobs()
  assert (images / "compute").exists()
  assert statuses(js) == [(3, 3)]


def test_each_job_is_handled(js, plugin, images):
  js.session.routes[JOBS] = [
    {"id": 3, "params": {"imageId": "gone"}},
    {"id": 4, "params": {"imageId": "compute"}},
  ]
  plugin.handle_jobs()
  assert statuses(js) == [(3, 3), (4, 4)]
  assert not (images / "compute").exists()
