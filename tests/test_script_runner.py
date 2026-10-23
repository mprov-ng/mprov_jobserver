"""
script_runner runs an image's or a system's scripts (from its distro, its
system groups and itself) in dependency order, after writing entity.json:
the entity with its config_params merged distro -> groups -> entity.  It
only runs in a runonce job server (image builds and post-boot).  Downloading
and running each script is replaced by a recorder.
"""
import json
import os
import threading

import types

import pytest

from mprov_jobserver.plugins import script_runner as script_runner_module
from mprov_jobserver.plugins.script_runner import script_runner


def script(slug, depends=(), stype="image-gen"):
  return {"slug": slug, "filename": f"http://mpcc/media/scripts/{slug}.sh",
          "scriptType": {"slug": stype}, "dependsOn": list(depends)}


def entity(**changes):
  data = {
    "name": "compute", "slug": "compute",
    "osdistro": {"scripts": [script("base")], "config_params": "- a: distro\n  b: distro\n"},
    "systemgroups": [{"scripts": [script("group-tool")], "config_params": "b: group\nc: group\n"}],
    "scripts": [script("image-tool", depends=[{"slug": "base"}]), script("motd", stype="post-boot")],
    "config_params": "c: entity\n",
  }
  data.update(changes)
  return data


@pytest.fixture
def plugin(js, tmp_path, monkeypatch):
  js.runonce = True
  # /etc/nologin keeps users out while the scripts run.
  nologin = []
  monkeypatch.setattr(script_runner_module, "Path",
                      lambda p: types.SimpleNamespace(touch=lambda: nologin.append(("touch", p))))
  unlink = os.unlink
  monkeypatch.setattr(os, "unlink", lambda p: nologin.append(("unlink", p)) if p == "/etc/nologin" else unlink(p))
  plugin = script_runner(js)
  plugin.nologin = nologin
  plugin.scriptTmpDir = str(tmp_path / "scripts")
  plugin.ran = []
  plugin.entities = []
  lock = threading.Lock()

  def run_script(filename):
    with lock:
      plugin.ran.append(filename.rsplit("/", 1)[-1][:-3])
      plugin.entities.append(json.loads((tmp_path / "scripts" / "entity.json").read_text()))
  plugin.runScript = run_script
  return plugin


def argv(monkeypatch, *args):
  monkeypatch.setattr(script_runner_module.sys, "argv", ["mprov_jobserver", *args])


# depResolve()

def test_dep_resolve_orders_steps(js):
  steps = script_runner(js).depResolve({"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]})
  assert steps == [{"a"}, {"b", "c"}, {"d"}]


def test_dep_resolve_rejects_a_dependency_nobody_provides(js):
  assert script_runner(js).depResolve({"a": ["missing"]}) is None


def test_dep_resolve_rejects_a_cycle(js):
  assert script_runner(js).depResolve({"a": ["b"], "b": ["a"]}) is None


def test_dep_resolve_of_nothing_is_no_steps(js):
  assert script_runner(js).depResolve({}) == []


# handle_jobs()

def test_image_scripts_run_in_dependency_order(js, plugin, tmp_path, monkeypatch):
  argv(monkeypatch, "-i", "compute")
  js.session.routes["systemimages/compute/"] = entity()
  plugin.handle_jobs()
  # image-gen scripts only; base before image-tool, which depends on it
  assert sorted(plugin.ran) == ["base", "group-tool", "image-tool"]
  assert plugin.ran.index("base") < plugin.ran.index("image-tool")
  # the entity file is removed when the scripts are done, and /etc/nologin is lifted
  assert not (tmp_path / "scripts" / "entity.json").exists()
  assert plugin.nologin == [("touch", "/etc/nologin"), ("unlink", "/etc/nologin")]


def test_entity_json_merges_config_and_adds_credentials(js, plugin, monkeypatch):
  argv(monkeypatch, "-i", "compute")
  js.session.routes["systemimages/compute/"] = entity()
  plugin.handle_jobs()
  seen = plugin.entities[0]
  assert seen["config_params"] == {"a": "distro", "b": "group", "c": "entity"}
  assert seen["mprovURL"] == "http://mpcc/"
  assert seen["apikey"] == "test-key"


def test_post_boot_mode_runs_post_boot_scripts(js, plugin, monkeypatch):
  argv(monkeypatch, "-i", "compute", "-b")
  js.session.routes["systemimages/compute/"] = entity()
  plugin.handle_jobs()
  assert plugin.ran == ["motd"]


def test_nads_image_gets_no_api_key(js, plugin, monkeypatch):
  # the N.A.D.S. image is served to unknown machines
  argv(monkeypatch, "-i", "nads")
  js.session.routes["systemimages/nads/"] = entity(name="__nads__", slug="nads")
  plugin.handle_jobs()
  assert plugin.entities[0]["apikey"] == ""


def test_system_mode_uses_the_systems_image_distro(js, plugin, monkeypatch):
  argv(monkeypatch, "-s")
  system = entity()
  system["hostname"] = "node01"
  system["systemimage"] = {"osdistro": system.pop("osdistro")}
  del system["name"]
  # ?self can answer with a one-entry list
  js.session.routes["systems/?self"] = [system]
  plugin.handle_jobs()
  assert "base" in plugin.ran
  assert plugin.entities[0]["hostname"] == "node01"


def test_unknown_image_falls_back_to_nads(js, plugin, monkeypatch):
  argv(monkeypatch, "-i", "gone")
  js.session.routes.update({"systemimages/gone/": (404, None), "systemimages/nads/": entity(slug="nads")})
  plugin.handle_jobs()
  assert ("GET", "systemimages/nads/") in js.session.requests
  assert plugin.ran


def test_refuses_to_run_outside_runonce(js, plugin, monkeypatch):
  argv(monkeypatch, "-i", "compute")
  js.runonce = False
  assert plugin.handle_jobs() is False
  assert js.session.requests == []


@pytest.mark.parametrize("args", [[], ["-i", "compute", "-s"], ["-h"]])
def test_bad_arguments_run_nothing(js, plugin, monkeypatch, args):
  argv(monkeypatch, *args)
  assert plugin.handle_jobs() is False
  assert js.session.requests == []
  # a bad invocation must never create /etc/nologin and lock users out (#173)
  assert plugin.nologin == []


def test_entity_load_failure_never_creates_nologin(js, plugin, monkeypatch):
  # neither the image nor the nads fallback loads: handle_jobs() exits(1),
  # and it must not have locked anyone out on the way (#173).
  argv(monkeypatch, "-i", "gone")
  js.session.routes.update({"systemimages/gone/": (404, None), "systemimages/nads/": (404, None)})
  with pytest.raises(SystemExit):
    plugin.handle_jobs()
  assert plugin.nologin == []


def test_nologin_is_lifted_when_the_scripts_region_fails(js, plugin, monkeypatch):
  # if anything raises while the scripts run (here: the entity file can't
  # be removed), /etc/nologin is still lifted on the way out (#173).
  argv(monkeypatch, "-i", "compute")
  js.session.routes["systemimages/compute/"] = entity()
  previous_unlink = os.unlink
  def fail_on_entity_file(path):
    if str(path).endswith("entity.json"):
      raise OSError("cannot remove the entity file")
    return previous_unlink(path)
  monkeypatch.setattr(os, "unlink", fail_on_entity_file)
  with pytest.raises(OSError):
    plugin.handle_jobs()
  assert plugin.nologin == [("touch", "/etc/nologin"), ("unlink", "/etc/nologin")]


@pytest.mark.xfail(strict=True, raises=TypeError,
                   reason="group scripts' dependsOn are objects (depth 3), but they're used as slugs (#146)")
def test_group_script_dependencies(js, plugin, monkeypatch):
  argv(monkeypatch, "-i", "compute")
  data = entity()
  data["systemgroups"][0]["scripts"] = [script("group-tool", depends=[script("base")])]
  js.session.routes["systemimages/compute/"] = data
  plugin.handle_jobs()
  assert plugin.ran.index("base") < plugin.ran.index("group-tool")


@pytest.mark.xfail(strict=True, raises=TypeError,
                   reason="with -s, distro scripts' dependsOn are slugs (depth 4), but they're read as objects (#146)")
def test_system_mode_distro_script_dependencies(js, plugin, monkeypatch):
  argv(monkeypatch, "-s")
  system = entity(scripts=[])
  system["hostname"] = "node01"
  del system["name"]
  system["systemimage"] = {"osdistro": dict(system.pop("osdistro"),
                                            scripts=[script("base"), script("tool", depends=["base"])])}
  js.session.routes["systems/?self"] = system
  plugin.handle_jobs()
  assert plugin.ran.index("base") < plugin.ran.index("tool")


@pytest.mark.xfail(strict=True, raises=TypeError,
                   reason="an unresolvable dependency makes depResolve() return None, which handle_jobs() iterates (#146)")
def test_unresolvable_dependency_runs_nothing(js, plugin, monkeypatch):
  argv(monkeypatch, "-i", "compute")
  js.session.routes["systemimages/compute/"] = entity(scripts=[script("tool", depends=[{"slug": "missing"}])])
  plugin.handle_jobs()
  assert plugin.ran == []
