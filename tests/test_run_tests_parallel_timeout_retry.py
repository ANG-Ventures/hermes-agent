"""A slow test FILE must never fail a slice on its first per-file timeout.

Merge groups 36213157250/36213163045/36213168317 (2026-09-25) each failed on
``TIMED OUT after 300s`` for a file that passes in ~80s on a quiet box: under
8 workers on a 4-vCPU runner the file was merely slow. The inline flake retry
re-ran it immediately under the SAME load and SAME ceiling, so it timed out
again and ejected every PR in the group.

Contract pinned here:
  * a timed-out file is retried ONCE in isolation, after the parallel pool
    drains; pass-on-isolated-retry counts as passed and is reported as SLOW;
  * a file that times out in isolation too still fails the run;
  * the default per-file ceiling scales with worker oversubscription.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUNNER = _REPO_ROOT / "scripts" / "run_tests_parallel.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("_rtp_timeout_retry", _RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(files, timeout_s: str, tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_RUNNER), "-j", "2", "--file-timeout", timeout_s,
         "--files", ":".join(str(f) for f in files)],
        cwd=_REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=120,
    )


def _slow_first_attempt_file(tmp_path: Path) -> Path:
    # First attempt sleeps past the ceiling; any later attempt passes fast.
    sentinel = tmp_path / "attempted"
    f = tmp_path / "test_slow_once.py"
    f.write_text(
        "import time\nfrom pathlib import Path\n"
        f"S = Path({str(sentinel)!r})\n"
        "def test_slow_once():\n"
        "    if not S.exists():\n"
        "        S.write_text('1')\n"
        "        time.sleep(60)\n",
        encoding="utf-8",
    )
    return f


def test_timed_out_file_passes_on_isolated_retry(tmp_path):
    f = _slow_first_attempt_file(tmp_path)
    proc = _run([f], "4", tmp_path)
    assert proc.returncode == 0, proc.stdout[-3000:]
    assert "SLOW" in proc.stdout, proc.stdout[-3000:]
    assert "retried in isolation" in proc.stdout, proc.stdout[-3000:]


def test_file_that_times_out_in_isolation_too_still_fails(tmp_path):
    f = tmp_path / "test_always_hangs.py"
    f.write_text("import time\ndef test_hang():\n    time.sleep(60)\n", encoding="utf-8")
    proc = _run([f], "3", tmp_path)
    assert proc.returncode == 1, proc.stdout[-3000:]
    assert "TIMED OUT" in proc.stdout, proc.stdout[-3000:]


def test_timeout_is_not_retried_inline_under_the_same_load():
    mod = _load_runner()
    calls = []

    def fake_once(file, pytest_args, repo_root, file_timeout):
        calls.append(file)
        return file, 124, "(killed)", {"timed_out": 1, "timeout_secs": 1}, 1.0

    mod._run_one_file_once = fake_once
    _f, rc, _o, summary, _w = mod._run_one_file(Path("x.py"), [], _REPO_ROOT, 1.0, retries=1)
    assert rc == 124 and summary.get("timed_out")
    assert len(calls) == 1, "a timed-out file was re-run inline under the same load"


def test_default_file_timeout_scales_with_oversubscription():
    mod = _load_runner()
    assert mod.scaled_file_timeout(300.0, workers=8, effective_cpus=4) == 600.0
    assert mod.scaled_file_timeout(300.0, workers=4, effective_cpus=4) == 300.0
    # Never below the base, even when under-subscribed.
    assert mod.scaled_file_timeout(300.0, workers=2, effective_cpus=8) == 300.0
