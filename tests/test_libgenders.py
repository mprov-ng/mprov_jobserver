"""
libgenders writes /etc/genders from the mPCC's systems, groups, images and
distros.  Each test checks the file with nodeattr, the genders tool pdsh
uses, as well as by content.
"""
import pytest

from mprov_jobserver.plugins.libgenders import libgenders
from tools import run


def nodeattr(genders, *args):
  return run(["nodeattr", "-f", str(genders), *args])


def nodes_with(genders, attr):
  ok, out = nodeattr(genders, "-n", attr)
  assert ok, out
  return sorted(out.split())


@pytest.fixture
def genders(tmp_path):
  return tmp_path / "genders"


@pytest.fixture
def include(tmp_path):
  # doesn't exist unless a test writes it
  return tmp_path / "genders.include"


@pytest.fixture
def plugin(js, genders, include):
  js.session.routes.update({
    "systemgroups/": [{"id": 1, "name": "Compute Nodes"}, {"id": 2, "name": "gpu"}],
    "distros/": [{"id": 7, "name": "Rocky 9"}],
    "systemimages/": [{"slug": "compute-img", "osdistro": 7}],
    "systems/": [],
  })
  plugin = libgenders(js)
  plugin.gendersFile = str(genders)
  plugin.gendersInclude = str(include)
  return plugin


def test_nodes_get_os_image_and_group_attributes(js, plugin, genders):
  js.session.routes["systems/"] = [
    {"hostname": "node01", "systemimage": "compute-img", "systemgroups": [1, 2]},
    {"hostname": "node02", "systemimage": None, "systemgroups": [2]},
  ]
  plugin.handle_jobs()

  ok, out = nodeattr(genders, "-k")
  assert ok, out
  # group and distro names are slugified, so lowercase with no spaces (#117)
  assert nodes_with(genders, "os=rocky-9") == ["node01"]
  assert nodes_with(genders, "image=compute-img") == ["node01"]
  assert nodes_with(genders, "compute-nodes") == ["node01"]
  assert nodes_with(genders, "gpu") == ["node01", "node02"]
  assert nodes_with(genders, "all") == ["node01", "node02"]
  assert js.job_updates[-1][:2] == ("libgenders", 4)


def test_osdistro_can_be_a_nested_object(js, plugin, genders):
  # The systemimages/ list can return osdistro as an ID or as an object.
  js.session.routes["systemimages/"] = [{"slug": "compute-img", "osdistro": {"id": 7, "name": "Rocky 9"}}]
  js.session.routes["systems/"] = [{"hostname": "node01", "systemimage": "compute-img", "systemgroups": []}]
  plugin.handle_jobs()
  assert nodes_with(genders, "os=rocky-9") == ["node01"]


def test_a_node_that_cant_be_resolved_is_commented_out(js, plugin, genders):
  # node02's image isn't in the systemimages/ list.  It becomes a comment,
  # and the rest of the file stays valid.
  js.session.routes["systems/"] = [
    {"hostname": "node01", "systemimage": None, "systemgroups": [1]},
    {"hostname": "node02", "systemimage": "gone-img", "systemgroups": []},
  ]
  plugin.handle_jobs()

  ok, out = nodeattr(genders, "-k")
  assert ok, out
  assert nodes_with(genders, "all") == ["node01"]
  assert "# Error processing node node02" in genders.read_text()


def test_mpcc_error_fails_the_job(js, plugin, genders):
  js.session.routes["systems/"] = (500, None)
  plugin.handle_jobs()
  assert js.job_updates[-1][:2] == ("libgenders", 3)
  assert not genders.exists()


NODES = [
  {"hostname": "node01", "systemimage": "compute-img", "systemgroups": [1]},
  {"hostname": "node02", "systemimage": None, "systemgroups": [2]},
]


def test_include_file_is_appended(js, plugin, genders, include):
  # The header promises entries in /etc/genders.include end up in the file (#136).
  include.write_text("# site extras\nlogin01 login,all\nnode01 bigmem\n")
  js.session.routes["systems/"] = NODES
  plugin.handle_jobs()

  ok, out = nodeattr(genders, "-k")
  assert ok, out
  assert nodes_with(genders, "login") == ["login01"]
  # an include line can add attributes to a generated node
  assert nodes_with(genders, "bigmem") == ["node01"]
  assert nodes_with(genders, "all") == ["login01", "node01", "node02"]
  text = genders.read_text()
  assert text.index("node02 ") < text.index(f"# Included from {include}") < text.index("login01")
  assert js.job_updates[-1][:2] == ("libgenders", 4)


def test_include_without_a_final_newline(js, plugin, genders, include):
  include.write_text("login01 login")
  js.session.routes["systems/"] = NODES
  plugin.handle_jobs()
  assert genders.read_text().endswith("login01 login\n")
  assert nodes_with(genders, "login") == ["login01"]


def test_no_include_file_adds_nothing(js, plugin, genders):
  js.session.routes["systems/"] = NODES
  plugin.handle_jobs()
  assert "Included from" not in genders.read_text()
  assert nodes_with(genders, "all") == ["node01", "node02"]


def test_unreadable_include_is_skipped(js, plugin, genders, include, capsys):
  include.mkdir()  # exists, but can't be read as a file
  js.session.routes["systems/"] = NODES
  plugin.handle_jobs()

  ok, out = nodeattr(genders, "-k")
  assert ok, out
  assert nodes_with(genders, "all") == ["node01", "node02"]
  assert f"# Unable to read {include}" in genders.read_text()
  assert "Warning[libgenders]: Unable to read" in capsys.readouterr().out
  assert js.job_updates[-1][:2] == ("libgenders", 4)


def test_include_path_comes_from_the_config(js):
  js.config_data = {"libgenders": {"gendersInclude": "/srv/genders.extra"}}
  plugin = libgenders(js)
  assert plugin.load_config()
  assert plugin.gendersInclude == "/srv/genders.extra"
