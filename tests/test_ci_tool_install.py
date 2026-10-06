"""Offline rejection gates; real release/ELF smoke runs are documented in .github/runner."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / ".github/scripts/install-ci-tool.sh"


@pytest.mark.skipif(not shutil.which("bash") or not shutil.which("sha256sum"), reason="Linux installer tools required")
@pytest.mark.parametrize("arch,suffix", [("x86_64", "x64"), ("aarch64", "arm64")])
@pytest.mark.parametrize("tool", ["ripgrep", "gitleaks", "hadolint"])
def test_corrupt_release_rejected_before_install(tmp_path, arch, suffix, tool):
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "uname").write_text(f"#!/bin/sh\nprintf '%s\\n' {arch}\n")
    (shim / "curl").write_text(
        '#!/bin/sh\nwhile [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = -o ]; then shift; output=$1; fi\n'
        '  url=$1; shift\ndone\n'
        'printf "%s\\n" "$url" > "$URL_LOG"\n'
        'printf "corrupt release" > "$output"\n'
    )
    for executable in shim.iterdir():
        executable.chmod(0o755)
    destination = tmp_path / "installed"
    path_file = tmp_path / "github-path"
    url_log = tmp_path / "url"
    env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}",
           "GITHUB_PATH": str(path_file), "URL_LOG": str(url_log)}
    result = subprocess.run(["bash", str(INSTALLER), tool, str(destination)],
                            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    assert result.returncode != 0
    assert "FAILED" in result.stdout
    assert not destination.exists()
    assert not path_file.exists()
    url = url_log.read_text()
    if tool == "ripgrep":
        assert f"-{arch}-unknown-linux-" in url
    elif tool == "gitleaks":
        assert f"_linux_{suffix}.tar.gz" in url
    else:
        assert f"hadolint-Linux-{'arm64' if arch == 'aarch64' else arch}" in url


def _flaky_curl_env(tmp_path, failures):
    """PATH shim: curl exits 35 (connection reset) `failures` times, then serves a corrupt body."""
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "uname").write_text("#!/bin/sh\nprintf 'x86_64\\n'\n")
    (shim / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (shim / "curl").write_text(
        '#!/bin/sh\necho call >> "$CALLS"\n'
        f'[ "$(wc -l < "$CALLS")" -le {failures} ] && exit 35\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = -o ]; then shift; output=$1; fi\n'
        '  shift\ndone\n'
        'printf "corrupt release" > "$output"\n'
    )
    for executable in shim.iterdir():
        executable.chmod(0o755)
    calls = tmp_path / "calls"
    return {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}", "CALLS": str(calls)}, calls


@pytest.mark.skipif(not shutil.which("bash") or not shutil.which("sha256sum"), reason="Linux installer tools required")
def test_transient_download_failure_is_retried(tmp_path):
    env, calls = _flaky_curl_env(tmp_path, failures=3)
    result = subprocess.run(["bash", str(INSTALLER), "ripgrep", str(tmp_path / "installed")],
                            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    assert len(calls.read_text().splitlines()) == 4
    assert "FAILED" in result.stdout  # reached the checksum gate after the blips
    assert result.returncode != 0


@pytest.mark.skipif(not shutil.which("bash"), reason="bash required")
def test_persistent_download_failure_gives_up(tmp_path):
    env, calls = _flaky_curl_env(tmp_path, failures=99)
    destination = tmp_path / "installed"
    result = subprocess.run(["bash", str(INSTALLER), "ripgrep", str(destination)],
                            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    assert result.returncode == 1
    assert len(calls.read_text().splitlines()) == 6
    assert "download failed after 6 attempts" in result.stderr
    assert not destination.exists()


@pytest.mark.skipif(not shutil.which("bash"), reason="bash required")
def test_unknown_arch_fails_before_network(tmp_path):
    (tmp_path / "uname").write_text("#!/bin/sh\nprintf 'unsupported-machine\\n'\n")
    (tmp_path / "uname").chmod(0o755)
    (tmp_path / "curl").write_text("#!/bin/sh\nexit 99\n")
    (tmp_path / "curl").chmod(0o755)
    result = subprocess.run(["bash", str(INSTALLER), "ripgrep"],
                            env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
                            stdin=subprocess.DEVNULL, capture_output=True, text=True)
    assert result.returncode == 1
    assert "Unsupported CI tool/architecture" in result.stderr


def test_workflows_share_installer():
    for workflow, jobs, tool in [("tests.yml", ("test", "e2e"), "ripgrep"),
                                 ("fleet-secret-scan.yml", ("secret_scan",), "gitleaks")]:
        data = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
        for job in jobs:
            steps = data["jobs"][job]["steps"]
            installs = [s for s in steps if s.get("name", "").startswith(f"Install {tool}")]
            assert len(installs) == 1
            assert installs[0]["run"] == f"bash .github/scripts/install-ci-tool.sh {tool}"
