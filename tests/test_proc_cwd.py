"""A process whose cwd read hangs must not hold the scratch prune hostage.

Regression for t_68bd4f4a: on macOS ``psutil.Process.cwd()`` of a process whose cwd is
on a dead hard NFS mount blocks in the kernel with the GIL held, freezing every thread of
the caller (gateway dispatcher, agent workers in ``prune_scratch_dir``).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

import pytest

import hermes_constants_scratch as scratch
import hermes_proc_cwd

pytestmark = pytest.mark.platforms("posix")

# Stand-in for the dead mount: the probe child's cwd() of one pid never returns.
_HANG_PREFIX = (
    "import os, time, psutil\n"
    "_real = psutil.Process.cwd\n"
    "def _cwd(self):\n"
    "    if self.pid == int(os.environ['T68_HUNG_PID']):\n"
    "        time.sleep(600)\n"
    "    return _real(self)\n"
    "psutil.Process.cwd = _cwd\n"
)


def _hang_in_process(monkeypatch, pid: int) -> None:
    """The caller itself must never read that cwd: here it would block for 60 s."""
    import psutil

    real = psutil.Process.cwd

    def cwd(self):
        if self.pid == pid:
            time.sleep(60)
        return real(self)

    monkeypatch.setattr(psutil.Process, "cwd", cwd)


def _sleeper(cwd) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], cwd=str(cwd),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_prune_finishes_and_still_reaps_when_one_cwd_probe_hangs(tmp_path, monkeypatch, caplog):
    root = tmp_path / "scratch"
    doomed = root / "idle-lane"
    doomed.mkdir(parents=True)
    hung_dir = tmp_path / "nfs"
    hung_dir.mkdir()
    hung = _sleeper(hung_dir)
    victim = _sleeper(doomed)
    try:
        old = time.time() - 48 * 3600
        os.utime(doomed, (old, old))
        monkeypatch.setenv("T68_HUNG_PID", str(hung.pid))
        monkeypatch.setattr(hermes_proc_cwd, "_PROBE_CHILD", _HANG_PREFIX + hermes_proc_cwd._PROBE_CHILD)
        caplog.set_level(logging.WARNING, logger="hermes_proc_cwd")
        _hang_in_process(monkeypatch, hung.pid)

        started = time.monotonic()
        assert scratch.prune_idle_entries(root, 24, frozenset()) == 1
        elapsed = time.monotonic() - started

        assert elapsed < 30, f"prune held the caller {elapsed:.1f}s behind one hung cwd probe"
        assert victim.wait(timeout=15) is not None, "process rooted in the pruned entry survived"
        assert hung.poll() is None, "the process with the unreadable cwd must be skipped, not killed"
        assert any(f"pid {hung.pid} " in r.getMessage() for r in caplog.records), caplog.text
        assert not doomed.exists()
    finally:
        for p in (hung, victim):
            p.kill()
            p.wait()


def test_probe_child_does_not_import_modules_from_the_callers_cwd(tmp_path, monkeypatch):
    """A worker's cwd is its workspace; a json.py/psutil.py there must not run in the probe."""
    marker = tmp_path / "shadow-ran"
    for name in ("json", "psutil"):
        (tmp_path / f"{name}.py").write_text(f"open({str(marker)!r}, 'a').write({name!r})\n")
    monkeypatch.chdir(tmp_path)
    sleeper = _sleeper(tmp_path)
    try:
        got = hermes_proc_cwd.probe_cwds([sleeper.pid])
        assert not marker.exists(), f"probe child imported {marker.read_text()}.py from the caller's cwd"
        assert os.path.realpath(got[sleeper.pid]) == os.path.realpath(tmp_path)
    finally:
        sleeper.kill()
        sleeper.wait()
