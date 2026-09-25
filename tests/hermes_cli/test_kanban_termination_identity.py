"""Termination and liveness paths check owner IDENTITY, not bare liveness
(t_0ae83825).

A dead, unreaped worker's PID can be reused by an unrelated process: a macOS
daemon or another card's live worker (measured on the live board). Before this
fix every termination path SIGTERMed whatever held ``tasks.worker_pid`` after
checking only that the PID was alive.

The rule is the claim guard's causal window (t_3a06ba8f B-2): a live PID is the
recorded worker iff ``claimed_at - 1 s <= create_time <= spawned_at + 2 s``.

Every test drives REAL processes (``/bin/sleep``) and the REAL create-time
probe. A "recycled holder" is a sleep started AFTER a card's run was back-dated
2 h, so it was provably created long after the recorded spawn. The only process
any path could signal is the test's own sleep.
"""

from __future__ import annotations

import errno
import importlib.util
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

BACKDATE = 7200


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    procs: list[subprocess.Popen] = []
    monkeypatch.setattr(sys.modules[__name__], "_PROCS", procs)
    with kb.connect() as c:
        yield c
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait(timeout=10)


_PROCS: list = []


def _sleeper() -> subprocess.Popen:
    proc = subprocess.Popen(["/bin/sleep", "120"])
    _PROCS.append(proc)
    return proc


def _reaper(proc):
    # Reap promptly so a killed holder does not linger as a zombie.
    threading.Thread(target=proc.wait, daemon=True).start()


def _card(conn, *, recycled: bool, parents=None, max_runtime=None, title="card",
          legacy: bool = False):
    """A running card whose ``worker_pid`` is held by a live /bin/sleep.

    ``recycled=True``: the run's claim/spawn evidence is back-dated 2 h, so
    the holder was created long after the recorded spawn -- the dead worker's
    PID now belongs to an unrelated process. The recorded ``start_token`` is
    shifted by the same 2 h, so the token check (t_21dfa673) also sees a
    different process.
    ``legacy=True``: the ``spawned`` row carries no ``start_token`` (pre-token
    rows), so identity falls back to the wall-clock causal window.
    ``recycled=False``: the holder is the genuine worker (spawned after the
    claim, recorded by ``_set_worker_pid``).
    """
    tid = kb.create_task(
        conn, title=title, assignee="worker", parents=parents or [],
        max_runtime_seconds=max_runtime,
    )
    if parents:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    task = kb.claim_task(conn, tid)
    assert task is not None
    holder = _sleeper()
    _reaper(holder)
    assert kb._set_worker_pid(conn, tid, holder.pid)
    if legacy:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET payload = json_remove(payload, '$.start_token') "
                "WHERE task_id = ? AND kind = 'spawned'", (tid,),
            )
    if recycled:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET payload = json_set(payload, '$.start_token', "
                "json_extract(payload, '$.start_token') - ?) "
                "WHERE task_id = ? AND kind = 'spawned' "
                "AND json_extract(payload, '$.start_token') IS NOT NULL",
                (BACKDATE, tid),
            )
            conn.execute(
                "UPDATE task_events SET created_at = created_at - ? "
                "WHERE task_id = ? AND kind IN ('claimed', 'spawned')",
                (BACKDATE, tid),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = started_at - ? WHERE id = ?",
                (BACKDATE, task.current_run_id),
            )
            conn.execute(
                "UPDATE tasks SET started_at = started_at - ? WHERE id = ?",
                (BACKDATE, tid),
            )
    return tid, holder, task.current_run_id


def _backdate_run(conn, tid, seconds):
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET started_at = ? "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (int(time.time()) - seconds, tid),
        )


class _Recorder:
    """signal_fn test hook. ``deliver=True`` really signals (our own sleep)."""

    def __init__(self, *, deliver=True, raise_exc=None):
        self.calls: list[tuple[int, int]] = []
        self.deliver = deliver
        self.raise_exc = raise_exc

    def __call__(self, pid, sig):
        self.calls.append((pid, sig))
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.deliver:
            os.kill(pid, sig)


def _alive(holder) -> bool:
    return holder.poll() is None and kb._pid_alive(holder.pid)


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]


def _events(conn, tid, kind):
    import json
    return [
        json.loads(r[0]) if r[0] else {}
        for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
            (tid, kind),
        )
    ]


def _dashboard():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_t0ae83825",
        root / "plugins" / "kanban" / "dashboard" / "plugin_api.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Acceptance 1: a recycled holder is never signalled, and the card is
# released as proven-dead, on EVERY termination path. RED on main.
# ---------------------------------------------------------------------------


def test_recycled_holder_reclaim_task(conn):
    tid, holder, _ = _card(conn, recycled=True)
    rec = _Recorder()
    assert kb.reclaim_task(conn, tid, reason="op", signal_fn=rec) is True
    assert rec.calls == []
    assert _alive(holder)
    assert _status(conn, tid) == "ready"


def test_recycled_holder_reclaim_task_legacy_row(conn):
    """A pre-token ``spawned`` row still falls back to the causal window."""
    tid, holder, _ = _card(conn, recycled=True, legacy=True)
    rec = _Recorder()
    assert kb.reclaim_task(conn, tid, reason="op", signal_fn=rec) is True
    assert rec.calls == []
    assert _alive(holder)
    assert _status(conn, tid) == "ready"


def test_genuine_worker_legacy_row_still_signalled(conn):
    tid, holder, _ = _card(conn, recycled=False, legacy=True)
    rec = _Recorder()
    kb.reclaim_task(conn, tid, reason="op", signal_fn=rec)
    assert rec.calls and rec.calls[0] == (holder.pid, signal.SIGTERM)


def test_owner_window_carries_recorded_start_token(conn):
    """Termination identity uses the same start token the claim guard does."""
    tid, holder, run_id = _card(conn, recycled=False)
    window = kb._worker_owner_window(conn, tid, holder.pid, run_id)
    assert len(window) == 3
    token = kb._pid_start_token(holder.pid)
    if token is None:
        pytest.skip("platform has no start token")
    assert window[2] == pytest.approx(token, abs=kb._OWNER_START_TOKEN_TOLERANCE_SECONDS)


def test_recycled_holder_ttl_release(conn):
    tid, holder, _ = _card(conn, recycled=True)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET claim_expires=? WHERE id=?",
                     (int(time.time()) - 10, tid))
    rec = _Recorder()
    kb.release_stale_claims(conn, signal_fn=rec)
    assert rec.calls == []
    assert _alive(holder)
    assert _status(conn, tid) == "ready"
    assert _events(conn, tid, "claim_extended") == []


def test_recycled_holder_max_runtime(conn):
    tid, holder, _ = _card(conn, recycled=True, max_runtime=1)
    rec = _Recorder()
    assert tid in kb.enforce_max_runtime(conn, signal_fn=rec)
    assert rec.calls == []
    assert _alive(holder)
    assert _status(conn, tid) == "ready"


def test_recycled_holder_stale_running(conn):
    tid, holder, _ = _card(conn, recycled=True)
    rec = _Recorder()
    assert tid in kb.detect_stale_running(conn, stale_timeout_seconds=60, signal_fn=rec)
    assert rec.calls == []
    assert _alive(holder)


def test_recycled_holder_progress_stall(conn, monkeypatch):
    tid, holder, rid = _card(conn, recycled=True)
    monkeypatch.setattr(kb, "_worker_cpu_active", lambda _pid: False)
    assert kb.heartbeat_worker(conn, tid, expected_run_id=rid,
                               progress_at=int(time.time()) - 3600)
    assert kb.detect_progress_stalls(conn, stall_seconds=900, reclaim_seconds=1500) == [tid]
    assert _alive(holder)


def test_recycled_holder_dashboard_move(conn):
    tid, holder, _ = _card(conn, recycled=True)
    mod = _dashboard()
    assert mod._set_status_direct(conn, tid, "ready") is True
    assert _alive(holder)
    assert _status(conn, tid) == "ready"


def test_recycled_holder_parent_reopen_invalidation(conn):
    parent = kb.create_task(conn, title="parent", assignee="planner")
    assert kb.complete_task(conn, parent)
    tid, holder, _ = _card(conn, recycled=True, parents=[parent])
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
    kb.invalidate_descendants_for_parent_reopen(conn, parent, author="op")
    assert _alive(holder)
    assert _status(conn, tid) == "todo"


def test_recycled_holder_does_not_hide_a_crash(conn):
    """Liveness path: a recycled holder must not keep a dead worker 'alive'."""
    tid, holder, _ = _card(conn, recycled=True)
    assert tid in kb.detect_crashed_workers(conn)
    assert _alive(holder)
    assert _status(conn, tid) != "running"


# ---------------------------------------------------------------------------
# Acceptance 2: a GENUINE live worker is still signalled / held as today.
# ---------------------------------------------------------------------------


def test_genuine_worker_reclaim_task_signalled(conn):
    tid, holder, _ = _card(conn, recycled=False)
    rec = _Recorder()
    assert kb.reclaim_task(conn, tid, reason="op", signal_fn=rec) is True
    assert rec.calls and rec.calls[0] == (holder.pid, signal.SIGTERM)
    holder.wait(timeout=10)


def test_genuine_worker_ttl_extended_not_signalled(conn):
    tid, holder, _ = _card(conn, recycled=False)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET claim_expires=? WHERE id=?",
                     (int(time.time()) - 10, tid))
    rec = _Recorder()
    kb.release_stale_claims(conn, signal_fn=rec)
    assert rec.calls == []
    assert _status(conn, tid) == "running"
    assert len(_events(conn, tid, "claim_extended")) == 1


def test_genuine_worker_max_runtime_signalled(conn):
    tid, holder, _ = _card(conn, recycled=False, max_runtime=1)
    _backdate_run(conn, tid, 60)
    rec = _Recorder()
    assert tid in kb.enforce_max_runtime(conn, signal_fn=rec)
    assert rec.calls and rec.calls[0] == (holder.pid, signal.SIGTERM)


def test_genuine_worker_stale_running_signalled(conn):
    tid, holder, _ = _card(conn, recycled=False)
    _backdate_run(conn, tid, 3600)
    rec = _Recorder()
    assert tid in kb.detect_stale_running(conn, stale_timeout_seconds=60, signal_fn=rec)
    assert rec.calls and rec.calls[0] == (holder.pid, signal.SIGTERM)


def test_genuine_worker_progress_stall_signalled(conn, monkeypatch):
    tid, holder, rid = _card(conn, recycled=False)
    _backdate_run(conn, tid, 7200)
    monkeypatch.setattr(kb, "_worker_cpu_active", lambda _pid: False)
    assert kb.heartbeat_worker(conn, tid, expected_run_id=rid,
                               progress_at=int(time.time()) - 3600)
    assert kb.detect_progress_stalls(conn, stall_seconds=900, reclaim_seconds=1500) == [tid]
    assert holder.wait(timeout=10) == -signal.SIGTERM


def test_genuine_worker_dashboard_move_signalled(conn):
    tid, holder, _ = _card(conn, recycled=False)
    mod = _dashboard()
    assert mod._set_status_direct(conn, tid, "ready") is True
    assert holder.wait(timeout=10) == -signal.SIGTERM


def test_genuine_worker_parent_reopen_signalled(conn):
    parent = kb.create_task(conn, title="parent", assignee="planner")
    assert kb.complete_task(conn, parent)
    tid, holder, _ = _card(conn, recycled=False, parents=[parent])
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
    kb.invalidate_descendants_for_parent_reopen(conn, parent, author="op")
    assert holder.wait(timeout=10) == -signal.SIGTERM


def test_genuine_worker_not_reported_crashed(conn):
    tid, holder, _ = _card(conn, recycled=False)
    assert tid not in kb.detect_crashed_workers(conn)
    assert _status(conn, tid) == "running"


@pytest.mark.parametrize("legacy", [True, False], ids=["legacy-window", "start-token"])
def test_genuine_worker_with_lock_lagged_spawn_event_still_signalled(conn, legacy):
    """Arm 2b: ``_set_worker_pid`` runs in write_txn AFTER Popen, so under DB
    lock contention the ``spawned`` event lags the worker's creation (measured
    -7.5 s / -19.6 s). A symmetric +/-N s rule around spawned_at calls this
    genuine worker recycled; the causal window (legacy rows) must not, and the
    start token (every spawn since t_21dfa673) ignores the lag entirely."""
    tid, holder, _ = _card(conn, recycled=False, legacy=legacy)
    created = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_events SET created_at=? WHERE task_id=? AND kind='claimed'",
            (created - 2, tid),
        )
        conn.execute(
            "UPDATE task_events SET created_at=? WHERE task_id=? AND kind='spawned'",
            (created + 20, tid),
        )
    rec = _Recorder()
    assert kb.reclaim_task(conn, tid, reason="op", signal_fn=rec) is True
    assert rec.calls and rec.calls[0] == (holder.pid, signal.SIGTERM)


# ---------------------------------------------------------------------------
# Acceptance 3: EPERM. A mismatched holder is released without a signal; a
# MATCHED one stays held (needs_attention) instead of deferring silently.
# ---------------------------------------------------------------------------


def _eperm():
    return _Recorder(raise_exc=PermissionError(errno.EPERM, "Operation not permitted"))


def test_eperm_recycled_holder_released_ttl(conn):
    tid, holder, _ = _card(conn, recycled=True)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET claim_expires=?, last_heartbeat_at=? WHERE id=?",
            (int(time.time()) - 10,
             int(time.time()) - kb.DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS - 60,
             tid),
        )
    rec = _eperm()
    for _ in range(3):
        kb.release_stale_claims(conn, signal_fn=rec)
    assert rec.calls == []
    assert _status(conn, tid) == "ready"
    assert _events(conn, tid, "reclaim_deferred") == []


def test_eperm_matched_worker_held_needs_attention(conn):
    tid, holder, _ = _card(conn, recycled=False)
    rec = _eperm()
    assert kb.reclaim_task(conn, tid, reason="op", signal_fn=rec) is False
    assert rec.calls == [(holder.pid, signal.SIGTERM)]
    assert _status(conn, tid) == "running"
    refused = _events(conn, tid, "reclaim_refused")
    assert refused and refused[-1]["needs_attention"] is True
    assert refused[-1]["signal_error"] == "EPERM"
    assert refused[-1]["owner_identity"] == "verified"


def test_eperm_matched_worker_ttl_deferred_needs_attention(conn):
    tid, holder, _ = _card(conn, recycled=False)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET claim_expires=?, last_heartbeat_at=? WHERE id=?",
            (int(time.time()) - 10,
             int(time.time()) - kb.DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS - 60,
             tid),
        )
    kb.release_stale_claims(conn, signal_fn=_eperm())
    assert _status(conn, tid) == "running"
    deferred = _events(conn, tid, "reclaim_deferred")
    assert deferred and deferred[-1]["needs_attention"] is True


# ---------------------------------------------------------------------------
# Fail closed: an UNREADABLE create time never signals and never releases.
# ---------------------------------------------------------------------------


def test_unreadable_create_time_never_signals_never_releases(conn, monkeypatch):
    tid, holder, _ = _card(conn, recycled=True)
    monkeypatch.setattr(kb, "_pid_create_time", lambda _pid: None)
    monkeypatch.setattr(kb, "_pid_start_token", lambda _pid: None)
    rec = _Recorder()
    assert kb.reclaim_task(conn, tid, reason="op", signal_fn=rec) is False
    assert rec.calls == []
    assert _alive(holder)
    assert _status(conn, tid) == "running"
    refused = _events(conn, tid, "reclaim_refused")
    assert refused and refused[-1]["reason"] == "liveness_unprovable"
    assert refused[-1]["identity_unverifiable"] is True


def test_unreadable_create_time_max_runtime_holds(conn, monkeypatch):
    tid, holder, _ = _card(conn, recycled=True, max_runtime=1)
    monkeypatch.setattr(kb, "_pid_create_time", lambda _pid: None)
    monkeypatch.setattr(kb, "_pid_start_token", lambda _pid: None)
    rec = _Recorder()
    assert kb.enforce_max_runtime(conn, signal_fn=rec) == []
    assert rec.calls == []
    assert _status(conn, tid) == "running"
    refused = _events(conn, tid, "timeout_refused")
    assert refused and refused[-1]["identity_unverifiable"] is True


# ---------------------------------------------------------------------------
# Class guard: no call site may signal without an identity window.
# ---------------------------------------------------------------------------


def test_owner_window_is_a_required_keyword():
    with pytest.raises(TypeError):
        kb._terminate_reclaimed_worker(os.getpid(), "x:1")  # type: ignore[call-arg]
