"""Offline rejection gates; real release/ELF smoke runs are documented in .github/runner."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / ".github/scripts/install-ci-tool.sh"
DOWNLOADER = ROOT / ".github/scripts/download-ci-asset.sh"


def _download(tmp_path, mode):
    shim = tmp_path / "shim"
    shim.mkdir()
    # Fail the real shell transport boundary, not the verification gate.
    (shim / "sleep").write_text('#!/bin/sh\nexit 0\n')
    (shim / "curl").write_text(
        '#!/bin/sh\n'
        'printf "%s\\n" "$*" >> "$CALLS"\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = -o ]; then shift; output=$1; fi\n'
        '  url=$1; shift\ndone\n'
        'case "$MODE:$url" in\n'
        '  primary:https://github.com/*) printf "verified payload" > "$output" ;;\n'
        '  *:https://github.com/*|unavailable:*) printf "HTTP 504\\n" >&2; exit 22 ;;\n'
        '  fallback:*) printf "verified payload" > "$output" ;;\n'
        '  json:*) printf \'{"id":123,"name":"asset"}\' > "$output" ;;\n'
        '  *) printf "wrong bytes" > "$output" ;;\n'
        'esac\n'
    )
    for executable in shim.iterdir():
        executable.chmod(0o755)
    output, calls = tmp_path / "asset", tmp_path / "calls"
    env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}",
           "MODE": mode, "CALLS": str(calls)}
    result = subprocess.run(
        ["bash", str(DOWNLOADER), "https://github.com/owner/repo/releases/download/v1/asset",
         "https://api.github.com/repos/owner/repo/releases/assets/123",
         hashlib.sha256(b"verified payload").hexdigest(), str(output)],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
    )
    return result, output, calls.read_text().splitlines()


@pytest.mark.skipif(not shutil.which("bash") or not shutil.which("sha256sum"), reason="installer tools required")
@pytest.mark.parametrize("mode,requests", [("primary", 1), ("fallback", 2)])
def test_verified_download_transport(tmp_path, mode, requests):
    result, output, calls = _download(tmp_path, mode)
    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == b"verified payload"
    assert len(calls) == requests
    if mode == "fallback":
        assert "https://api.github.com/repos/owner/repo/releases/assets/123" in calls[-1]
        assert "Accept: application/octet-stream" in calls[-1]
    for call in calls:
        assert call.startswith("-q ")  # ignore ambient curlrc, including credentials
        assert "--proto =https" in call and "--proto-redir =https" in call
        assert "--connect-timeout 20 --max-time 120" in call
        assert "--max-redirs 5" in call
        assert "Authorization" not in call and "--location-trusted" not in call


@pytest.mark.skipif(not shutil.which("bash") or not shutil.which("sha256sum"), reason="installer tools required")
@pytest.mark.parametrize("mode,requests", [("wrong", 2), ("json", 2), ("unavailable", 6)])
def test_fallback_download_fails_closed(tmp_path, mode, requests):
    result, output, calls = _download(tmp_path, mode)
    assert result.returncode != 0
    assert len(calls) == requests
    if mode == "unavailable":
        assert "download failed after 6 attempts" in result.stderr
    else:
        assert "FAILED" in result.stdout
    assert not output.exists()  # no failed candidate survives to an install step


@pytest.mark.skipif(not shutil.which("bash") or not shutil.which("sha256sum"), reason="Linux installer tools required")
@pytest.mark.parametrize("arch,suffix", [("x86_64", "x64"), ("aarch64", "arm64")])
@pytest.mark.parametrize("tool", ["ripgrep", "gitleaks", "hadolint"])
@pytest.mark.parametrize("fallback", [False, True])
def test_corrupt_release_rejected_before_install(tmp_path, arch, suffix, tool, fallback):
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "uname").write_text(f"#!/bin/sh\nprintf '%s\\n' {arch}\n")
    (shim / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (shim / "curl").write_text(
        '#!/bin/sh\nwhile [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = -o ]; then shift; output=$1; fi\n'
        '  url=$1; shift\ndone\n'
        'printf "%s\\n" "$url" >> "$URL_LOG"\n'
        'case "$FALLBACK:$url" in 1:https://github.com/*) exit 22 ;; esac\n'
        'printf "corrupt release" > "$output"\n'
    )
    for executable in shim.iterdir():
        executable.chmod(0o755)
    destination = tmp_path / "installed"
    path_file = tmp_path / "github-path"
    url_log = tmp_path / "url"
    env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}",
           "GITHUB_PATH": str(path_file), "URL_LOG": str(url_log), "FALLBACK": str(int(fallback)),
           "CI_TOOL_DISTRO_FALLBACK": "0"}
    result = subprocess.run(["bash", str(INSTALLER), tool, str(destination)],
                            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "FAILED" in result.stdout
    assert not destination.exists()
    assert not path_file.exists()
    urls = url_log.read_text().splitlines()
    url = urls[0]
    if fallback:
        ids = {"ripgrep": (307306614, 307306472), "gitleaks": (173813554, 173813549),
               "hadolint": (84037697, 84037701)}
        repo = "BurntSushi/ripgrep" if tool == "ripgrep" else f"{tool}/{tool}"
        asset_id = ids[tool][arch == "aarch64"]
        assert urls == [url, f"https://api.github.com/repos/{repo}/releases/assets/{asset_id}"]
    else:
        assert len(urls) == 1
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
    return {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}", "CALLS": str(calls),
            "CI_TOOL_DISTRO_FALLBACK": "0"}, calls


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


def _distro_fallback(tmp_path, tool, *, have_rg, apt_ok=True):
    """Every GitHub GET fails (403); rg/sudo are shims so the host's own rg never answers."""
    env, calls = _flaky_curl_env(tmp_path, failures=99)
    env.pop("CI_TOOL_DISTRO_FALLBACK")
    shim = tmp_path / "shim"
    rg_script = '#!/bin/sh\nprintf "ripgrep 14.1.0\\n"\n'
    (tmp_path / "rg.template").write_text(rg_script)
    (shim / "sudo").write_text(
        '#!/bin/sh\necho "$*" >> "$SUDO_LOG"\n'
        + ('cat "$SHIM/../rg.template" > "$SHIM/rg"; chmod +x "$SHIM/rg"\n' if apt_ok else "exit 100\n"))
    if have_rg:
        (shim / "rg").write_text(rg_script)
    for executable in shim.iterdir():
        executable.chmod(0o755)
    # PATH = shim + a toolbox of the utilities the scripts call, so no host rg can answer.
    toolbox = tmp_path / "toolbox"
    toolbox.mkdir()
    for name in ("bash", "sh", "mktemp", "rm", "dirname", "sha256sum", "wc", "chmod", "cat", "tar",
                 "mkdir", "install"):
        found = shutil.which(name)
        if found:
            (toolbox / name).symlink_to(found)
    env.update(PATH=f"{shim}:{toolbox}", SUDO_LOG=str(tmp_path / "sudo"), SHIM=str(shim))
    destination = tmp_path / "installed"
    result = subprocess.run(["bash", str(INSTALLER), tool, str(destination)],
                            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    sudo_log = tmp_path / "sudo"
    return result, destination, sudo_log.read_text().splitlines() if sudo_log.exists() else []


@pytest.mark.skipif(not shutil.which("bash"), reason="bash required")
def test_ripgrep_uses_present_rg_when_github_refuses(tmp_path):
    result, destination, sudo = _distro_fallback(tmp_path, "ripgrep", have_rg=True)
    assert result.returncode == 0, result.stderr
    assert "using distro rg" in result.stderr and "ripgrep 14.1.0" in result.stdout
    assert sudo == [] and not destination.exists()  # no unverified bytes installed


@pytest.mark.skipif(not shutil.which("bash"), reason="bash required")
def test_ripgrep_installs_distro_package_when_github_refuses(tmp_path):
    result, _, sudo = _distro_fallback(tmp_path, "ripgrep", have_rg=False)
    assert result.returncode == 0, result.stderr
    assert sudo == ["-n apt-get install -y -qq --no-install-recommends ripgrep"]


@pytest.mark.skipif(not shutil.which("bash"), reason="bash required")
def test_ripgrep_fails_when_distro_package_also_fails(tmp_path):
    result, _, sudo = _distro_fallback(tmp_path, "ripgrep", have_rg=False, apt_ok=False)
    assert result.returncode == 1
    assert "pinned download and distro package both failed" in result.stderr
    assert len(sudo) == 2  # install, then update (+ install skipped because update failed)


@pytest.mark.skipif(not shutil.which("bash"), reason="bash required")
def test_other_tools_never_take_the_distro_fallback(tmp_path):
    result, _, sudo = _distro_fallback(tmp_path, "gitleaks", have_rg=True)
    assert result.returncode == 1 and sudo == []


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
