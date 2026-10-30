"""
The image build disables the distro's stock repos only where the image has
them: asking dnf to disable a repo that isn't there logs "Error:" on a
build that worked (#211).  Uses test_image_update's fake job server, image
store and recorded commands.
"""
# the fixtures (fake_wget is autouse there, so it's imported to apply here too)
from test_image_update import fake_wget, images, plugin, pending_build, job_states  # noqa: F401

from mprov_jobserver.plugins.image_update_mod.rhel import stock_repos_in


def stock_repo_file(images, name, *repos):
  repoDir = images / "compute/etc/yum.repos.d"
  repoDir.mkdir(parents=True, exist_ok=True)
  (repoDir / name).write_text("".join(f"[{repo}]\nname={repo}\nenabled=1\n\n" for repo in repos))


def disables(commands):
  return commands.matching("config-manager --disable")


def test_a_new_image_disables_no_repos(js, plugin, commands, capsys):
  pending_build(js)
  plugin.handle_jobs()
  assert job_states(js)[-1][0] == 4
  # the build's own -mprov repo files aren't stock repos
  assert disables(commands) == []


def test_an_existing_image_disables_only_the_stock_repos_it_has(js, plugin, images, commands):
  stock_repo_file(images, "Rocky-BaseOS.repo", "baseos", "baseos-source")
  stock_repo_file(images, "epel.repo", "epel", "epel-debuginfo")
  pending_build(js)
  plugin.handle_jobs()
  first, second = disables(commands)
  assert first.startswith("dnf -y --installroot=")
  assert "--releasever=9 config-manager --disable baseos epel" in first
  assert second.endswith("/compute dnf -y config-manager --disable baseos epel")


def test_a_failed_disable_only_warns(js, plugin, images, commands, capsys):
  stock_repo_file(images, "Rocky-AppStream.repo", "appstream")
  commands.fail.append("config-manager --disable")
  pending_build(js)
  plugin.handle_jobs()
  assert "Warning: unable to disable the image's stock repos appstream." in capsys.readouterr().out
  assert job_states(js)[-1][0] == 4


def test_stock_repos_in_reads_the_repo_ids(tmp_path):
  repoDir = tmp_path / "etc/yum.repos.d"
  repoDir.mkdir(parents=True)
  (repoDir / "a.repo").write_text("[crb]\n[ powertools ]\n# [extras]\n")
  (repoDir / "a.repo.rpmnew").write_text("[extras]\n")
  (repoDir / "b-mprov.repo").write_text("[baseos-mprov]\n")
  assert stock_repos_in(tmp_path) == ["powertools", "crb"]
  assert stock_repos_in(tmp_path / "missing") == []
