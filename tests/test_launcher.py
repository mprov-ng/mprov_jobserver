import os
import shutil
import subprocess
from pathlib import Path

import pytest


LAUNCHER = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "mprov_jobserver"
    / "mprov_jobserver"
)


def create_fake_python(path, marker, version="3.11.0"):
    """Create a fake Python executable for launcher tests."""
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-V" ]; then\n'
        f'  echo "Python {version}"\n'
        "else\n"
        f'  echo "{marker}"\n'
        "fi\n"
    )
    path.chmod(0o755)


def run_launcher(tmp_path, adjacent=None, path_python=None, cwd=None):
    """Run the launcher with controlled adjacent and PATH interpreters."""
    launcher_dir = tmp_path / "venv" / "bin"
    other_bin = tmp_path / "other-bin"

    launcher_dir.mkdir(parents=True)
    other_bin.mkdir()

    launcher = launcher_dir / "mprov_jobserver"
    shutil.copy2(LAUNCHER, launcher)

    for name, version in (adjacent or {}).items():
        create_fake_python(
            launcher_dir / name,
            marker=f"adjacent-{name}",
            version=version,
        )

    for name, version in (path_python or {}).items():
        create_fake_python(
            other_bin / name,
            marker=f"path-{name}",
            version=version,
        )

    env = os.environ.copy()
    env["PATH"] = f"{other_bin}:/usr/bin:/bin"

    process = subprocess.Popen(
        ["bash", str(launcher)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=cwd,
    )

    stdout, stderr = process.communicate(timeout=10)

    return process.returncode, stdout.strip(), stderr.strip()


def test_launcher_prefers_adjacent_python(tmp_path):
    """Prefer adjacent python3 over python3 from PATH."""
    returncode, stdout, stderr = run_launcher(
        tmp_path,
        adjacent={"python3": "3.11.0"},
        path_python={"python3": "3.11.0"},
    )

    assert returncode == 0, stderr
    assert stdout == "adjacent-python3"


def test_launcher_prefers_adjacent_python_without_python3(tmp_path):
    """Use adjacent python when adjacent python3 is unavailable."""
    returncode, stdout, stderr = run_launcher(
        tmp_path,
        adjacent={"python": "3.11.0"},
        path_python={"python3": "3.11.0"},
    )

    assert returncode == 0, stderr
    assert stdout == "adjacent-python"


def test_launcher_falls_back_to_path(tmp_path):
    """Use PATH when no adjacent interpreter exists."""
    returncode, stdout, stderr = run_launcher(
        tmp_path,
        path_python={"python3": "3.11.0"},
    )

    assert returncode == 0, stderr
    assert stdout == "path-python3"


def test_launcher_works_from_another_directory(tmp_path):
    """Find adjacent Python regardless of the working directory."""
    working_dir = tmp_path / "different-directory"
    working_dir.mkdir()

    returncode, stdout, stderr = run_launcher(
        tmp_path,
        adjacent={"python3": "3.11.0"},
        path_python={"python3": "3.11.0"},
        cwd=working_dir,
    )

    assert returncode == 0, stderr
    assert stdout == "adjacent-python3"


def test_launcher_rejects_old_python(tmp_path):
    """Reject Python versions below the minimum supported version."""
    returncode, stdout, stderr = run_launcher(
        tmp_path,
        adjacent={"python3": "3.7.0"},
    )

    assert returncode != 0
    assert "need python 3.8 or above" in stdout
    assert "illegal option" not in stderr.lower()
    assert "unrecognized option" not in stderr.lower()

def test_launcher_falls_back_to_path_python(tmp_path):
    """Use python from PATH when python3 cannot be found."""
    launcher_dir = tmp_path / "launcher"
    fake_bin = tmp_path / "fake-bin"

    launcher_dir.mkdir()
    fake_bin.mkdir()

    launcher = launcher_dir / "mprov_jobserver"
    shutil.copy2(LAUNCHER, launcher)

    create_fake_python(
        fake_bin / "python",
        marker="path-python",
    )

    # Provide required shell utilities without exposing system python3.
    for command in ("awk", "sort", "dirname", "readlink"):
        executable = shutil.which(command)
        assert executable is not None
        (fake_bin / command).symlink_to(executable)

    env = os.environ.copy()
    env["PATH"] = str(fake_bin)

    process = subprocess.Popen(
        ["/bin/bash", str(launcher)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 0, stderr
    assert stdout.strip() == "path-python"


def run_through_symlink(tmp_path, link_target):
    """
    Lay out a node image's bundle (#309): the launcher and its python3 in
    lib/mprov/bin, a python3 on PATH as well, and bin/mprov_jobserver a
    symlink to the launcher.  Run the launcher through the link, the way
    script-runner.sh and the systemd units call it.
    """
    bundle_bin = tmp_path / "lib" / "mprov" / "bin"
    link_dir = tmp_path / "local" / "bin"
    other_bin = tmp_path / "other-bin"
    for d in (bundle_bin, link_dir, other_bin):
        d.mkdir(parents=True)

    launcher = bundle_bin / "mprov_jobserver"
    shutil.copy2(LAUNCHER, launcher)
    launcher.chmod(0o755)
    create_fake_python(bundle_bin / "python3", marker="bundled-python3")
    create_fake_python(other_bin / "python3", marker="path-python3")

    link = link_dir / "mprov_jobserver"
    link.symlink_to(link_target(launcher, link_dir))

    env = os.environ.copy()
    env["PATH"] = f"{other_bin}:/usr/bin:/bin"
    # Popen, not subprocess.run: conftest.py refuses run() for the plugins.
    process = subprocess.Popen(
        [str(link)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    stdout, stderr = process.communicate(timeout=10)
    return process.returncode, stdout.strip(), stderr.strip()


def test_launcher_run_through_a_symlink_uses_the_targets_python(tmp_path):
    """The interpreter next to the link's target wins, not PATH's (#310)."""
    returncode, stdout, stderr = run_through_symlink(
        tmp_path, lambda launcher, link_dir: launcher
    )

    assert returncode == 0, stderr
    assert stdout == "bundled-python3"


def test_launcher_run_through_a_relative_symlink(tmp_path):
    """A relative link, as `ln -s ../../lib/mprov/bin/...` makes, works too."""
    returncode, stdout, stderr = run_through_symlink(
        tmp_path, lambda launcher, link_dir: os.path.relpath(launcher, link_dir)
    )

    assert returncode == 0, stderr
    assert stdout == "bundled-python3"
