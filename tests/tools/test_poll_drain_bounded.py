"""Regression test: _reconcile_local_exit's drain must be bounded when a
descendant holds the pipe open and floods it (unbounded-drain hang, 2026-07-23).

Incident: a session ran `codex login 2>&1 &`. The wrapper exited; the codex
TUI inherited the stdout pipe and redrew its spinner to it continuously. The
session's reader thread was already stuck, so nothing competed for the pipe.
poll() → _reconcile_local_exit → `BufferedReader.read()` (read-to-EOF) after
O_NONBLOCK: a flooding writer never yields EOF and (with no competing reader)
rarely yields a would-block gap, so the read busy-looped, pinning a gateway
worker thread for ~2h. Each /stop → continue pinned another.

This test constructs that exact state — a fake exited direct child whose
stdout pipe is being flooded by a live descendant, with NO reader thread —
and asserts _reconcile_local_exit returns within the drain bound.
"""

import os
import subprocess
import sys
import threading
import time

import pytest

from tools.process_registry import (
    _DRAIN_DEADLINE_SECONDS,
    _DRAIN_MAX_BYTES,
    ProcessRegistry,
    ProcessSession,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX pipe semantics")


class _IncidentStdout:
    """Faithful stand-in for the incident pipe's BufferedReader.

    - ``fileno()`` exposes the REAL read end of a pipe being flooded by a
      live descendant — the bounded drain (``os.read``) operates on this.
    - ``read()`` (the pre-fix drain's call) behaves as ``BufferedReader.read``
      did against a never-gapping writer: it never returns. Pre-fix code
      calling it hangs exactly like the production incident.
    """

    def __init__(self, fd: int):
        self._fd = fd
        self._stop = threading.Event()

    def fileno(self) -> int:
        return self._fd

    def read(self, *args):  # pragma: no cover - only reached by pre-fix code
        while not self._stop.wait(0.05):
            pass
        return b""

    def close(self):
        self._stop.set()
        try:
            os.close(self._fd)
        except OSError:
            pass


class _ExitedProcWithFloodedPipe:
    """Mimics a Popen whose direct child exited while a descendant floods
    the inherited stdout pipe (the `codex login 2>&1 &` shape)."""

    def __init__(self):
        r, w = os.pipe()
        # Descendant: `yes` floods the write end continuously, no gaps.
        self._flooder = subprocess.Popen(
            ["yes", "|/-\\" * 512],
            stdout=w,
            stderr=subprocess.DEVNULL,
        )
        os.close(w)  # our copy; flooder holds its own
        self.stdout = _IncidentStdout(r)
        self.pid = self._flooder.pid  # irrelevant to the code under test

    def poll(self):
        return 0  # direct child exited

    def cleanup(self):
        try:
            self._flooder.kill()
            self._flooder.wait(timeout=5)
        except Exception:
            pass
        try:
            self.stdout.close()
        except Exception:
            pass


@pytest.fixture()
def incident_session():
    registry = ProcessRegistry()
    proc = _ExitedProcWithFloodedPipe()
    session = ProcessSession(
        id="proc_testdrain001",
        command="codex login 2>&1 &",
        cwd=os.getcwd(),
        started_at=time.time(),
    )
    session.process = proc  # type: ignore[assignment]  # duck-typed Popen stand-in
    registry._running[session.id] = session
    # Let the flooder saturate the pipe buffer before the drain runs.
    time.sleep(0.2)
    yield registry, session, proc
    proc.cleanup()


def test_reconcile_drain_bounded_under_flooding_pipe(incident_session):
    """THE CONTRACT: the drain returns within its deadline (+ slack) even
    when the pipe never hits EOF and never presents a would-block gap."""
    registry, session, proc = incident_session

    result: dict = {}

    def run():
        t0 = time.monotonic()
        registry._reconcile_local_exit(session)
        result["elapsed"] = time.monotonic() - t0

    t = threading.Thread(target=run, daemon=True)
    t.start()
    # Generous wall-clock bound: drain deadline + scheduling slack. Pre-fix,
    # the unbounded read outlives this by orders of magnitude (observed: hours).
    t.join(timeout=_DRAIN_DEADLINE_SECONDS + 10.0)

    assert not t.is_alive(), (
        "_reconcile_local_exit is still running — unbounded drain regression "
        "(the 2026-07-23 codex-login hang)"
    )
    assert result["elapsed"] < _DRAIN_DEADLINE_SECONDS + 5.0
    # And the session must be reconciled to exited.
    assert session.exited is True
    assert session.exit_code == 0


def test_reconcile_drain_deadline_when_pipe_never_gaps(incident_session, monkeypatch):
    """Deterministic incident shape: reads ALWAYS return data — no EOF, no
    would-block gap, ever (the codex-login spinner cadence). The drain must
    stop at its own deadline/byte cap, not rely on the pipe cooperating."""
    import tools.process_registry as pr_mod

    registry, session, proc = incident_session
    real_os_read = os.read

    def always_data_read(fd, n):
        # Only intercept the drain's fd; everything else behaves normally.
        if fd == session.process.stdout.fileno():  # type: ignore[union-attr]
            return b"|" * min(n, 65536)
        return real_os_read(fd, n)

    monkeypatch.setattr(pr_mod.os, "read", always_data_read)

    t0 = time.monotonic()
    registry._reconcile_local_exit(session)
    elapsed = time.monotonic() - t0

    # Deadline cap (+ slack): pre-fix this never returns.
    assert elapsed < _DRAIN_DEADLINE_SECONDS + 5.0
    assert session.exited is True


def test_reconcile_drain_byte_cap(incident_session):
    """Drained output appended to the buffer respects _DRAIN_MAX_BYTES."""
    registry, session, proc = incident_session
    before = len(session.output_buffer)

    t = threading.Thread(
        target=registry._reconcile_local_exit, args=(session,), daemon=True
    )
    t.start()
    t.join(timeout=_DRAIN_DEADLINE_SECONDS + 10.0)
    assert not t.is_alive()

    appended = len(session.output_buffer) - before
    # Buffer may also be ring-truncated to max_output_chars; either way the
    # drain must not have appended more than the cap.
    assert appended <= _DRAIN_MAX_BYTES
