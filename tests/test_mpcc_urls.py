"""
mprovURL always ends with "/", so the paths plugins add to it mustn't start
with one: "http://mpcc//systems/register" only works where the web server
merges slashes, and a managed repo's baseurl in an image kept the "//"
(#115).  The fake session in conftest.py refuses any such request.
"""
# the fixtures (fake_wget is autouse there, so it's imported to apply here too)
from test_image_update import fake_wget, images, plugin, pending_build  # noqa: F401

from conftest import MPROV_URL
from mprov_jobserver.plugins.image_update_mod.rhel import UpdateImage
from mprov_jobserver.plugins.repo_update_mod.rhel import UpdateRepo


def test_os_type_registration_has_no_double_slash(js):
  for module in (UpdateImage, UpdateRepo):
    js.session.writes.clear()
    module(js).load_config()
    assert [(m, p) for m, p, _ in js.session.writes] == [("POST", "ostypes/")]


def test_a_managed_repos_baseurl_has_no_double_slash(js, plugin, images, commands):
  pending_build(js)
  plugin.handle_jobs()
  repofile = images / "compute/etc/yum.repos.d/rocky-9-baseos-mprov.repo"
  assert f'baseurl="{MPROV_URL}osrepos/12/"\n' in repofile.read_text()
