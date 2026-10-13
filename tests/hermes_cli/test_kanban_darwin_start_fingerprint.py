"""Darwin worker start fingerprints are one value in every process (t_7fe63baa).

psutil's macOS ``create_time()`` re-bases the kernel's start stamp by
``INIT_BOOT_TIME - boot_time()``, and ``INIT_BOOT_TIME`` is captured when THAT
process imported psutil. After a clock step moves ``kern.boottime``, a gateway
up since boot reads every pid a whole second (or more) away from a dispatcher
started later. That split marked live workers ``crashed`` ~80 s after spawn on
the Studio and spawned a duplicate beside them.
"""

import os
import subprocess
import sys
import time

import pytest

from gateway import drain_control
from gateway.status import get_process_start_time
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.mark.platforms("macos")
def test_start_fingerprint_ignores_this_process_psutil_boot_snapshot(monkeypatch):
    """A process whose psutil import predates a clock step reads the SAME
    fingerprint for a pid as a freshly started one."""
    from psutil import _psosx

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        t0 = kbd._process_fingerprint(child.pid)
        token = kb._pid_start_token(child.pid)
        monkeypatch.setattr(_psosx, "INIT_BOOT_TIME", _psosx.INIT_BOOT_TIME + 1)
        time.sleep(2)
        t2 = kbd._process_fingerprint(child.pid)
        assert t0 == t2
        # One source of truth: the fingerprint is the spawn start_token in centiseconds.
        assert t2.rpartition("|")[2] == str(int(round(token * 100)))
        assert kbd._worker_alive(child.pid, t0) is True
    finally:
        monkeypatch.undo()  # the conftest kill guard identifies children by psutil create_time()
        child.kill()
        child.wait()


@pytest.mark.platforms("macos")
def test_darwin_instantiation_epoch_is_a_real_per_boot_value():
    epoch = drain_control.current_instantiation_epoch()
    assert epoch
    assert epoch == drain_control.current_instantiation_epoch()


def test_legacy_epochless_fingerprint_still_matches_after_epoch_gains_a_value():
    """Rows written before darwin had an epoch (``|<cs>``) are not read as a
    reboot once it has one, or every live worker would be reclaimed at deploy."""
    pid = os.getpid()
    current = kbd._process_fingerprint(pid)
    start = current.rpartition("|")[2]
    assert kbd._fingerprints_agree(f"|{start}", current)
    assert not kbd._fingerprints_agree(f"other-boot|{start}", current) or current.startswith("|")


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


@pytest.mark.platforms("posix")
def test_live_worker_naming_its_task_is_never_marked_crashed(board):
    """Whatever the fingerprint says, a running pid whose argv is still
    ``work kanban task <id>`` is that card's worker: no crash, no retry."""
    conn = board
    tid = kb.create_task(conn, title="job", assignee="worker")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "chat", "-q", f"work kanban task {tid}"])
    try:
        kb.claim_task(conn, tid)
        kbd._set_worker_pid(conn, tid, child.pid)
        old = int(time.time()) - 3600
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET worker_started_at = ?, started_at = ? WHERE id = ?",
                         ("|1", old, tid))
        assert kbd._worker_alive(child.pid, "|1") is False  # the fingerprint alone says "gone"
        assert tid not in kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task.status == "running" and task.worker_pid == child.pid
    finally:
        child.kill()
        child.wait()
