"""dev-sandbox.sh must retry a transient upstream fetch before calling a ref unresolvable.

Scheduled Install & Update E2E run 36724184458 (2026-09-30) failed 2 of 10 jobs with
"could not resolve upstream ref: v2026.3.12" / "v2026.4.8" while sibling jobs resolved
the same tags: one failed fetch was treated as a missing ref.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path

import pytest

# The harness only fetches from a throwaway local repo under tmp_path.
pytestmark = pytest.mark.live_system_guard_bypass

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev-sandbox.sh"


def _fetch_block() -> str:
    text = SCRIPT.read_text()
    start = text.index('  UPSTREAM_REPO="$(mktemp -d')
    end = text.index("\nfi\n", start)
    return text[start:end]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _run(tmp_path: Path, url: str, ref: str) -> subprocess.CompletedProcess:
    harness = (
        "set -u\n"
        f"UPSTREAM_URL={url!s}\nINSTALL_REF={ref}\n"
        + _fetch_block()
        + '\necho "COMMIT=$UPSTREAM_COMMIT"\n'
    )
    return subprocess.run(
        ["bash", "-c", harness],
        capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
             "HOME": str(tmp_path), "SANDBOX_FETCH_RETRY_SLEEP": "1",
             "GIT_CONFIG_NOSYSTEM": "1"},
    )


def _make_upstream(path: Path) -> str:
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "c1")
    _git(path, "tag", "-a", "v2026.3.12", "-m", "t")
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


def test_transient_fetch_failure_is_retried(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    sha = _make_upstream(staging)
    upstream = tmp_path / "upstream"          # absent at first: every fetch fails

    def appear() -> None:
        time.sleep(1.5)
        staging.rename(upstream)

    threading.Thread(target=appear, daemon=True).start()
    result = _run(tmp_path, str(upstream), "v2026.3.12")
    assert result.returncode == 0, result.stderr
    assert f"COMMIT={sha}" in result.stdout


def test_missing_ref_still_fails_with_the_fetch_error(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    _make_upstream(upstream)
    result = _run(tmp_path, str(upstream), "v1999.1.1")
    assert result.returncode == 1
    assert "could not resolve upstream ref: v1999.1.1" in result.stderr
    assert "last fetch error:" in result.stderr


def test_raw_sha_is_not_retried(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    sha = _make_upstream(upstream)
    started = time.monotonic()
    result = _run(tmp_path, str(upstream), sha)
    assert result.returncode == 0, result.stderr
    assert f"COMMIT={sha}" in result.stdout
    assert time.monotonic() - started < 3, "a SHA must not wait out the tag retries"
