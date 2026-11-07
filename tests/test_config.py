"""The config file's documents are merged, and `global:` across them (#301)."""
import json
import pathlib

import jinja2
import pytest
import yaml

from mprov_jobserver.app import JobServer
from mprov_jobserver.config import merge_config

# The container's config template: at the repository root in a checkout, and
# copied next to /tests by tests/Dockerfile.
TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "jobserver.yaml.j2"


@pytest.fixture(autouse=True)
def keep_the_log_level():
  # load_config() applies logLevel to the job server's root logger; put it
  # back so these tests don't change what the others log.
  from mprov_jobserver import log
  level = log._root.level
  yield
  log._root.setLevel(level)


# merge_config()

def test_global_settings_from_a_later_document_are_added():
  merged = merge_config([{"global": {"mprovURL": "http://m/", "apikey": "k"}},
                         {"global": {"logLevel": "debug"}}])
  assert merged["global"] == {"mprovURL": "http://m/", "apikey": "k", "logLevel": "debug"}


def test_a_later_global_setting_wins():
  merged = merge_config([{"global": {"heartbeatInterval": 10}},
                         {"global": {"heartbeatInterval": 30}}])
  assert merged["global"] == {"heartbeatInterval": 30}


def test_other_sections_are_still_replaced_whole():
  merged = merge_config([{"image-update": {"a": 1, "b": 2}},
                         {"image-update": {"a": 3}}])
  assert merged["image-update"] == {"a": 3}


def test_sections_from_different_documents_are_all_kept():
  merged = merge_config([{"global": {"apikey": "k"}}, {"libgenders": {"x": 1}}, {"dnsmasq": {"y": 2}}])
  assert set(merged) == {"global", "libgenders", "dnsmasq"}


def test_an_empty_document_is_skipped():
  assert merge_config([{"global": {"apikey": "k"}}, {}, None]) == {"global": {"apikey": "k"}}


def test_the_input_documents_are_not_changed():
  first = {"global": {"apikey": "k"}}
  merge_config([first, {"global": {"logLevel": "debug"}}])
  assert first == {"global": {"apikey": "k"}}


# load_config() with plugin files

def write_config(tmp_path, global_cfg, plugin_files):
  (tmp_path / "plugins").mkdir()
  for name, documents in plugin_files.items():
    (tmp_path / "plugins" / name).write_text(json.dumps(documents))  # JSON is YAML
  text = json.dumps([{"global": global_cfg}])[:-1] + ', !include "plugins/*.yaml"]'
  path = tmp_path / "jobserver.yaml"
  path.write_text(text)
  return str(path)


def load(configfile):
  server = JobServer.__new__(JobServer)
  server.configfile = configfile
  server.load_config()
  return server


BASE = {"mprovURL": "http://mpcc/", "apikey": "k", "jobmodules": ["libgenders"]}


def test_a_plugin_file_adds_a_global_setting_without_wiping_the_rest(tmp_path, monkeypatch):
  monkeypatch.setattr(JobServer, "use_ca_bundle", lambda self: None)
  path = write_config(tmp_path, BASE, {
    "local.yaml": [{"global": {"logLevel": "debug", "caBundle": "/etc/mprov/ca.pem"}}],
    "libgenders.yaml": [{"libgenders": {"gendersFile": "/tmp/genders"}}],
  })
  server = load(path)
  assert server.mprovURL == "http://mpcc/"
  assert server.apikey == "k"
  assert server.jobmodules == ["libgenders"]
  assert server.logLevel == "debug"
  assert server.caBundle == "/etc/mprov/ca.pem"
  assert server.config_data["libgenders"] == {"gendersFile": "/tmp/genders"}


def test_a_plugin_file_overrides_a_generated_global_setting(tmp_path):
  path = write_config(tmp_path, {**BASE, "heartbeatInterval": 10},
                      {"local.yaml": [{"global": {"heartbeatInterval": 30}}]})
  assert load(path).heartbeatInterval == 30


def test_two_plugin_files_global_settings_both_apply_the_later_name_winning(tmp_path):
  path = write_config(tmp_path, BASE, {
    "a.yaml": [{"global": {"heartbeatInterval": 20, "logLevel": "warning"}}],
    "b.yaml": [{"global": {"heartbeatInterval": 30}}],
  })
  server = load(path)
  assert server.heartbeatInterval == 30
  assert server.logLevel == "warning"


def test_an_unknown_global_setting_in_a_plugin_file_is_still_rejected(tmp_path):
  path = write_config(tmp_path, BASE, {"local.yaml": [{"global": {"nosuchsetting": 1}}]})
  with pytest.raises(SystemExit):
    load(path)


# jobserver.yaml.j2

ENV = {"MPROV_URL": "http://mpcc/", "MPROV_APIKEY": "k", "MPROV_ADDRESS": "10.0.0.2",
       "MPROV_MODS": "libgenders dnsmasq"}


def render(env):
  # the same rendering start_server.sh does
  return jinja2.Template(TEMPLATE.read_text()).render(env=env)


class NoInclude(yaml.FullLoader):
  pass


NoInclude.add_constructor("!include", lambda loader, node: {})


def global_section(env):
  return merge_config(yaml.load(render(env), Loader=NoInclude))["global"]


def test_the_template_leaves_the_optional_settings_out_when_unset():
  section = global_section(ENV)
  assert "caBundle" not in section
  assert "logLevel" not in section
  assert section["mprovURL"] == "http://mpcc/"
  assert section["jobmodules"] == ["libgenders", "dnsmasq"]


def test_the_template_leaves_out_an_empty_variable():
  section = global_section({**ENV, "MPROV_CA_BUNDLE": "", "MPROV_LOG_LEVEL": ""})
  assert "caBundle" not in section
  assert "logLevel" not in section


def test_the_template_sets_ca_bundle_and_log_level_from_the_environment():
  section = global_section({**ENV, "MPROV_CA_BUNDLE": "/etc/mprov/site ca.pem", "MPROV_LOG_LEVEL": "debug"})
  assert section["caBundle"] == "/etc/mprov/site ca.pem"
  assert section["logLevel"] == "debug"
  assert section["apikey"] == "k"
  assert section["myaddress"] == "10.0.0.2"


def test_the_template_leaves_the_api_key_out_without_one():
  """ The shared key is only a fallback now (mprov_control_center#381): a
  job server that enrolls has none. """
  env = {k: v for k, v in ENV.items() if k != "MPROV_APIKEY"}
  assert "apikey" not in global_section(env)
  assert "apikey" not in global_section({**env, "MPROV_APIKEY": ""})


def test_the_template_quotes_the_api_key():
  assert global_section({**ENV, "MPROV_APIKEY": "a1b2.c3: d4"})["apikey"] == "a1b2.c3: d4"


def test_the_rendered_template_loads_in_the_job_server(tmp_path, monkeypatch):
  monkeypatch.setattr(JobServer, "use_ca_bundle", lambda self: None)
  (tmp_path / "plugins").mkdir()
  (tmp_path / "plugins" / "local.yaml").write_text(json.dumps([{"global": {"heartbeatInterval": 30}}]))
  path = tmp_path / "jobserver.yaml"
  path.write_text(render({**ENV, "MPROV_LOG_LEVEL": "warning", "MPROV_CA_BUNDLE": "/etc/mprov/ca.pem"}))
  server = load(str(path))
  assert server.mprovURL == "http://mpcc/"
  assert server.logLevel == "warning"
  assert server.caBundle == "/etc/mprov/ca.pem"
  assert server.heartbeatInterval == 30
