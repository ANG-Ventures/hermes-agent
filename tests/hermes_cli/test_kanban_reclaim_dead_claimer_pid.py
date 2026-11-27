"""t_bf2c4ade: a dead LOCAL claimer with no worker is released AT the launch bound.

Measured on t_8b654880 (2026-11-09, run 66791): ``kanban admit`` claimed the card
as ``mac-studio-m3u:4108`` (the CLI's own pid) and launched nothing. Inside the
launch bound a manual ``reclaim`` was refused ``liveness_unprovable``, and that
refusal extended the lease by a flat 120 s, which moved ``claim_expires`` BEFORE
the bound. The TTL sweep then fired inside the bound and logged ``reclaim_deferred
ttl_expired_worker_alive`` with another flat extension: the claim was judged only
on lease expiries that never lined up with the bound.

A hold on a dead claimer now runs the lease to the bound itself, so the sweep
that fires next is the one that releases. Release still re-reads worker evidence
under the write lock. The launch bound is unchanged (no early release, Apollo
r3 ruling on #208), and a claim held by another host keeps today's behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.delenv("HERMES_KANBAN_CLAIM_TTL_SECONDS", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.discard(str(kb.kanban_db_path(board="default").resolve()))
    kb.init_db()
    with kb.connect() as c:
        yield c


def _dead_pid() -> int:
    c = subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL)
    c.wait(timeout=10)
    assert not psutil.pid_exists(c.pid)
    return c.pid


def _admit_shaped_claim(conn, *, age: int, lock_host: str | None = None):
    """The incident row: running, claim_lock = <host>:<dead CLI pid>, no worker
    pid, no spawned/heartbeat event, claimed ``age`` seconds ago with the default
    claim window (expires at claim + TTL)."""
    tid = kb.create_task(conn, title="admit phantom", assignee="daedalus")
    task = kb.claim_task(conn, tid)
    assert task is not None
    host = lock_host or kb._claimer_id().split(":", 1)[0]
    lock = f"{host}:{_dead_pid()}"
    started = int(time.time()) - age
    expires = started + kb.DEFAULT_CLAIM_TTL_SECONDS
    conn.execute("UPDATE tasks SET claim_lock=?, claim_expires=? WHERE id=?", (lock, expires, tid))
    conn.execute("UPDATE task_runs SET claim_lock=?, started_at=?, claim_expires=? WHERE id=?",
                 (lock, started, expires, task.current_run_id))
    conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='claimed'",
                 (json.dumps({"lock": lock, "expires": expires, "run_id": task.current_run_id}), tid))
    conn.commit()
    return tid, lock, task.current_run_id


def _events(conn, tid, kind):
    return [json.loads(r[0] or "{}") for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind))]


def _row(conn, tid):
    return conn.execute("SELECT status, claim_lock, claim_expires FROM tasks WHERE id=?", (tid,)).fetchone()


def _bound(conn, run_id) -> int:
    """The launch bound for the run, read from its own recorded claim time."""
    started = conn.execute("SELECT started_at FROM task_runs WHERE id=?", (run_id,)).fetchone()[0]
    return int(started) + kb.DEAD_CLAIMER_LAUNCH_BOUND_SECONDS


class _Clock:
    """Move time.time() forward for the code under test."""

    def __init__(self, monkeypatch):
        self.offset = 0
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() + self.offset)


def test_incident_replay_refused_reclaim_then_ttl_sweep_releases_at_the_bound(conn, monkeypatch):
    """The t_8b654880 sequence: reclaim inside the bound (refused), then the TTL
    sweep. On fork/main the refusal pulled the lease in to now+120 s and the
    sweep deferred ``ttl_expired_worker_alive`` inside the bound. Now the lease
    runs to the bound and the first sweep past it releases."""
    tid, lock, run_id = _admit_shaped_claim(conn, age=450)
    bound = _bound(conn, run_id)
    assert kb.reclaim_task(conn, tid, reason="admit launched nothing") is False
    assert _events(conn, tid, "reclaim_refused")[-1]["dead_claimer_hold_until"] == bound
    assert _row(conn, tid)["claim_expires"] == bound          # not now + 120

    clock = _Clock(monkeypatch)
    clock.offset = 130                                     # fork/main's lease ran out here
    assert kb.release_stale_claims(conn) == 0
    assert not _events(conn, tid, "reclaim_deferred")       # nothing expired, nothing deferred

    clock.offset = 451                                     # just past the bound
    assert kb.release_stale_claims(conn) == 1
    row = _row(conn, tid)
    assert (row["status"], row["claim_lock"]) == ("ready", None)
    assert not _events(conn, tid, "reclaim_deferred")
    ev = _events(conn, tid, "reclaimed")[-1]
    assert ev["stale_lock"] == lock and ev["dead_claimer_release_basis"] == "launch_bound"
    assert conn.execute("SELECT outcome FROM task_runs WHERE id=?", (run_id,)).fetchone()[0] == "reclaimed"


def test_ttl_sweep_deferral_inside_the_bound_extends_to_the_bound(conn):
    """A lease that expired inside the bound (an older flat extension, a shorter
    lease) is held to the bound, not for another flat 120 s."""
    tid, _, run_id = _admit_shaped_claim(conn, age=300)
    bound = _bound(conn, run_id)
    conn.execute("UPDATE tasks SET claim_expires=? WHERE id=?", (int(time.time()) - 1, tid))
    conn.commit()
    assert kb.release_stale_claims(conn) == 0
    deferred = _events(conn, tid, "reclaim_deferred")[-1]
    assert deferred["claim_expires_now"] == bound == _row(conn, tid)["claim_expires"]
    assert conn.execute("SELECT claim_expires FROM task_runs WHERE id=?", (run_id,)).fetchone()[0] == bound


def test_dead_local_claimer_past_the_bound_is_reclaimed_not_deferred(conn):
    tid, _, _ = _admit_shaped_claim(conn, age=kb.DEAD_CLAIMER_LAUNCH_BOUND_SECONDS + 5)
    assert kb.release_stale_claims(conn) == 1
    assert _row(conn, tid)["claim_lock"] is None
    assert not _events(conn, tid, "reclaim_deferred")


def test_manual_reclaim_past_the_bound_succeeds(conn):
    tid, _, _ = _admit_shaped_claim(conn, age=kb.DEAD_CLAIMER_LAUNCH_BOUND_SECONDS + 5)
    assert kb.reclaim_task(conn, tid, reason="admit launched nothing") is True
    assert (_row(conn, tid)["status"], _row(conn, tid)["claim_lock"]) == ("ready", None)
    assert not _events(conn, tid, "reclaim_refused")


def test_inside_the_bound_is_still_held(conn):
    """No early release (Apollo r3 on #208): inside the bound the claim holds."""
    tid, lock, _ = _admit_shaped_claim(conn, age=60)
    assert kb.reclaim_task(conn, tid) is False
    assert _row(conn, tid)["claim_lock"] == lock
    assert "dead_claimer_no_worker_process" not in _events(conn, tid, "reclaim_refused")[-1]


def test_remote_claim_past_the_bound_is_unchanged(conn):
    """A claim held by another host is not judged here (host_local False): the
    TTL sweep releases it on expiry exactly as on fork/main, with no dead-claimer
    basis and no deferral."""
    tid, _, _ = _admit_shaped_claim(conn, age=kb.DEAD_CLAIMER_LAUNCH_BOUND_SECONDS + 5,
                                    lock_host="pool-host-elsewhere")
    assert kb.release_stale_claims(conn) == 1
    assert _row(conn, tid)["claim_lock"] is None
    ev = _events(conn, tid, "reclaimed")[-1]
    assert ev["host_local"] is False and "dead_claimer_release_basis" not in ev
    assert not _events(conn, tid, "reclaim_deferred")


# -- late worker evidence, re-read under the release's write lock ---------------


def _late_heartbeat(monkeypatch, tid, run_id):
    """Land a heartbeat on the run from ANOTHER connection after the verdict was
    made (in _terminate_reclaimed_worker) and before the release write."""
    from hermes_cli import kanban_db_dispatch as kbd
    real = kbd._terminate_reclaimed_worker

    def verdict_then_heartbeat(*a, **k):
        info = real(*a, **k)
        with kb.connect() as other:
            other.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                          "VALUES (?,?,'heartbeat','{}',?)", (tid, run_id, int(time.time())))
            other.commit()
        return info
    monkeypatch.setattr(kbd, "_terminate_reclaimed_worker", verdict_then_heartbeat)
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", verdict_then_heartbeat)


def test_ttl_path_late_evidence_under_the_lock_keeps_the_claim(conn, monkeypatch):
    tid, lock, run_id = _admit_shaped_claim(conn, age=kb.DEAD_CLAIMER_LAUNCH_BOUND_SECONDS + 5)
    _late_heartbeat(monkeypatch, tid, run_id)
    assert kb.release_stale_claims(conn) == 0
    assert (_row(conn, tid)["status"], _row(conn, tid)["claim_lock"]) == ("running", lock)
    assert not _events(conn, tid, "reclaimed")


def test_manual_path_late_evidence_under_the_lock_keeps_the_claim(conn, monkeypatch):
    tid, lock, run_id = _admit_shaped_claim(conn, age=kb.DEAD_CLAIMER_LAUNCH_BOUND_SECONDS + 5)
    _late_heartbeat(monkeypatch, tid, run_id)
    assert kb.reclaim_task(conn, tid, reason="probe") is False
    assert _row(conn, tid)["claim_lock"] == lock


def test_stale_path_late_evidence_under_the_lock_keeps_the_claim(conn, monkeypatch):
    from hermes_cli import kanban_db_dispatch as kbd
    tid, lock, run_id = _admit_shaped_claim(conn, age=kb.DEAD_CLAIMER_LAUNCH_BOUND_SECONDS + 5)
    conn.execute("UPDATE tasks SET claim_expires=? WHERE id=?", (int(time.time()) + 900, tid))
    conn.commit()
    _late_heartbeat(monkeypatch, tid, run_id)
    assert kbd.detect_stale_running(conn, stale_timeout_seconds=1) == []
    assert (_row(conn, tid)["status"], _row(conn, tid)["claim_lock"]) == ("running", lock)
