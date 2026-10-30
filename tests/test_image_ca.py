"""
An image built for an https mPCC with a site or self-signed CA trusts that
CA (#267): the job server's caBundle goes into the image at the same path
(the host's dnf --installroot reads sslcacert as a host path) and into the
trust anchors, the mPCC's repos name it as sslcacert, and update-ca-trust
runs in the image once ca-certificates is installed.
"""
import pytest

# the fixtures (fake_wget is autouse there, so it's imported to apply here too)
from test_image_update import fake_wget, images, plugin, image, job_states, pending_build  # noqa: F401

from mprov_jobserver.plugins.image_update_mod.rhel import MPCC_CA_ANCHOR

UNMANAGED = {"id": 13, "name": "EPEL", "managed": False, "ostype": {"slug": "rhel"},
             "repo_package_url": "https://dl.fedoraproject.org/pub/epel/9/Everything/x86_64/"}


@pytest.fixture
def ca(js, tmp_path):
  path = tmp_path / "etc" / "mprov" / "mpcc-ca.pem"
  path.parent.mkdir(parents=True)
  path.write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
  js.config_data["global"]["caBundle"] = str(path)
  return path


def build(js, plugin):
  data = image()
  data["osdistro"]["extrarepos"] = [UNMANAGED]
  pending_build(js, data)
  plugin.handle_jobs()


def test_the_image_gets_the_mpccs_ca(js, plugin, images, commands, ca):
  build(js, plugin)
  img = images / "compute"
  assert (img / str(ca).lstrip("/")).read_text() == ca.read_text()
  assert (img / MPCC_CA_ANCHOR.lstrip("/")).read_text() == ca.read_text()
  managed = (img / "etc/yum.repos.d/rocky-9-baseos-mprov.repo").read_text()
  assert f"sslcacert={ca}\n" in managed
  # public repos keep the image's normal trust
  assert "sslcacert" not in (img / "etc/yum.repos.d/epel-mprov.repo").read_text()
  # update-ca-trust in the image, after the install that brings ca-certificates
  run = commands.run
  [minimal] = [i for i, cmd in enumerate(run) if "groupinstall 'Minimal Install'" in cmd and cmd.startswith("dnf")]
  [update] = [i for i, cmd in enumerate(run) if cmd == f"chroot {img} update-ca-trust"]
  assert minimal < update
  assert job_states(js)[-1][0] == 4


def test_without_a_configured_ca_nothing_changes(js, plugin, images, commands):
  build(js, plugin)
  img = images / "compute"
  assert not (img / MPCC_CA_ANCHOR.lstrip("/")).exists()
  assert "sslcacert" not in (img / "etc/yum.repos.d/rocky-9-baseos-mprov.repo").read_text()
  assert not commands.matching("update-ca-trust")


def test_a_ca_that_cant_be_copied_fails_the_build(js, plugin, images, commands, ca):
  ca.unlink()
  build(js, plugin)
  assert job_states(js) == [(3, 9)]
  assert not commands.matching("--installroot")


def test_a_failed_update_ca_trust_fails_the_build(js, plugin, images, commands, ca):
  commands.fail.append("update-ca-trust")
  build(js, plugin)
  assert job_states(js)[0] == (3, 9)
  assert not commands.matching("tar c")
