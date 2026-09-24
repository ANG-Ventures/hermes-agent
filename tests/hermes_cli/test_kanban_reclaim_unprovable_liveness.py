"""Tests: a reclaim must never re-queue a card it could not prove dead.

Measured incident (card ``t_09180e10``, 2026-09-22): the fleet worker sweep saw a
``running`` card whose ``tasks.worker_pid`` was NULL, concluded the worker was
dead, and called ``hermes kanban reclaim``. ``reclaim_task`` passed that NULL pid
to ``_terminate_reclaimed_worker``, which short-circuits on ``not pid`` and
returns ``termination_attempted=False, terminated=False``. The reclaim then wrote
``status='ready'``. The ORIGINAL worker (pid 26401, run 7394) was alive the whole
time; the dispatcher claimed the re-queued card and spawned a SECOND worker
(pid 41892, run 7414). Both landed a commit to the same file 90 seconds apart.

The verbatim event payload of the defect::

    {"manual": true, "prev_pid": null, "termination_attempted": false,
     "terminated": false, "sigkill": false, "retry_status": "ready"}

``prev_pid: null`` + ``termination_attempted: false`` is the whole bug: the
reclaim had, by its own admission, no evidence either way, and chose to re-queue.
Absence of a liveness signal is not evidence of death.

Fail CLOSED: a card whose liveness cannot be proven stays with its current owner
and surfaces for a human. Stranding one card is strictly cheaper than
double-landing commits.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import psutil
import secrets
import subprocess
import sys
import time

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home, monkeypatch):
    # Without this the dispatcher would refuse to spawn for a non-profile
    # assignee (skipped_nonspawnable) and the "no second worker" assertions
    # below would pass trivially instead of proving the guard.
    import hermes_cli.profiles as _profiles

    monkeypatch.setattr(_profiles, "profile_exists", lambda name: True)
    with kb.connect() as c:
        yield c


def _host_lock() -> str:
    """A claim lock owned by THIS host, as the dispatcher writes it."""
    return f"{kb._claimer_id().split(':', 1)[0]}:{secrets.token_hex(8)}"


def _running_card(conn, *, worker_pid, lock=None, title="pid-less running card"):
    """A ``running`` card with an open run, exactly as a live worker leaves it."""
    tid = kb.create_task(conn, title=title, assignee="daedalus-opus")
    lock = lock or _host_lock()
    now = int(time.time())
    expires = now + 3600
    conn.execute(
        "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
        "worker_pid=?, started_at=? WHERE id=?",
        (lock, expires, worker_pid, now, tid),
    )
    conn.execute(
        "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
        "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
        (tid, lock, expires, worker_pid, now),
    )
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, tid))
    conn.commit()
    return tid, lock, run_id


def _events(conn, tid, kind):
    return [
        json.loads(r["payload"]) if r["payload"] else {}
        for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=? "
            "ORDER BY id ASC",
            (tid, kind),
        )
    ]


def _row(conn, tid):
    return conn.execute(
        "SELECT status, claim_lock, worker_pid, current_run_id "
        "FROM tasks WHERE id=?",
        (tid,),
    ).fetchone()


def test_dead_local_claimer_without_worker_pid_is_reclaimed(conn, monkeypatch):
    """A dead claimer without any worker evidence can release its claim."""
    dead_pid = 999991
    lock = f"{kb._claimer_id().split(':', 1)[0]}:{dead_pid}"
    tid, _, run_id = _running_card(conn, worker_pid=None, lock=lock)

    def dead_claimer(pid):
        assert pid == dead_pid
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(psutil, "Process", dead_claimer)
    assert kb.reclaim_task(conn, tid, reason="worker sweep: missing pid") is True
    assert _row(conn, tid)["status"] == "ready"
    assert _row(conn, tid)["claim_lock"] is None
    assert conn.execute("SELECT outcome FROM task_runs WHERE id=?", (run_id,)).fetchone()[0] == "reclaimed"
    payload = _events(conn, tid, "reclaimed")[-1]
    assert payload["terminated"] is True
    assert payload["claimer_pid_dead"] == dead_pid
    assert not _events(conn, tid, "reclaim_refused")


def test_heartbeat_from_previous_run_does_not_hold_dead_claimer(conn, monkeypatch):
    dead_pid = 999995
    lock = f"{kb._claimer_id().split(':', 1)[0]}:{dead_pid}"
    tid, _, run_id = _running_card(conn, worker_pid=None, lock=lock)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "heartbeat", run_id=run_id - 1)
    def dead(pid):
        raise psutil.NoSuchProcess(pid)
    monkeypatch.setattr(psutil, "Process", dead)
    assert kb.reclaim_task(conn, tid) is True


@pytest.mark.parametrize("reclaim_path", ["manual", "ttl", "stale"])
def test_dead_claimer_with_unstamped_heartbeating_worker_keeps_claim(conn, reclaim_path):
    """Popen can succeed before the gateway commits _set_worker_pid.

    A detached worker may then outlive the gateway. Its heartbeat belongs to
    the current run even if the PID was never stamped. Neither operator nor
    automatic recovery may schedule a second worker beside it.
    """
    host = kb._claimer_id().split(":", 1)[0]
    claimer = subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL)
    claimer.wait(timeout=10)
    assert not psutil.pid_exists(claimer.pid)
    lock = f"{host}:{claimer.pid}"
    tid, _, run_id = _running_card(conn, worker_pid=None, lock=lock)
    orphan = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        assert kb.heartbeat_claim(conn, tid, claimer=lock)
        assert kb.heartbeat_worker(conn, tid, note="unstamped worker alive")
        assert _row(conn, tid)["worker_pid"] is None
        if reclaim_path == "ttl":
            conn.execute("UPDATE tasks SET claim_expires=? WHERE id=?", (int(time.time()) - 1, tid))
            conn.commit()
            assert kb.release_stale_claims(conn) == 0
        elif reclaim_path == "stale":
            old = int(time.time()) - 7200
            conn.execute("UPDATE task_runs SET started_at=? WHERE id=?", (old, run_id))
            conn.execute("UPDATE tasks SET last_heartbeat_at=? WHERE id=?", (old, tid))
            conn.commit()
            assert kb.detect_stale_running(conn, stale_timeout_seconds=60) == []
        else:
            assert kb.reclaim_task(conn, tid, reason="worker sweep: missing pid") is False
        spawned = []
        kb.dispatch_once(conn, spawn_fn=lambda task, workspace, board=None: spawned.append(task.id) or 424242)
        assert tid not in spawned
        assert orphan.poll() is None
        assert _row(conn, tid)["status"] == "running"
        assert _row(conn, tid)["claim_lock"] == lock
        assert not _events(conn, tid, "reclaimed")
    finally:
        orphan.kill()
        orphan.wait(timeout=10)


def test_expired_dead_claimer_is_not_renewed(conn, monkeypatch):
    dead_pid = 999992
    lock = f"{kb._claimer_id().split(':', 1)[0]}:{dead_pid}"
    tid, _, run_id = _running_card(conn, worker_pid=None, lock=lock)
    conn.execute("UPDATE tasks SET claim_expires=? WHERE id=?", (int(time.time()) - 1, tid))
    conn.commit()

    def dead_claimer(pid):
        assert pid == dead_pid
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(psutil, "Process", dead_claimer)
    assert kb.release_stale_claims(conn) == 1
    assert _row(conn, tid)["status"] == "ready"
    assert conn.execute("SELECT outcome FROM task_runs WHERE id=?", (run_id,)).fetchone()[0] == "reclaimed"
    assert _events(conn, tid, "reclaimed")[-1]["claimer_pid_dead"] == dead_pid
    assert not _events(conn, tid, "reclaim_deferred")


def test_live_local_claimer_without_worker_pid_still_holds_claim(conn, monkeypatch):
    lock = f"{kb._claimer_id().split(':', 1)[0]}:{os.getpid()}"
    tid, _, _ = _running_card(conn, worker_pid=None, lock=lock)
    seen = []
    original_process = psutil.Process

    def live_claimer(pid):
        seen.append(pid)
        return original_process(pid)

    monkeypatch.setattr(psutil, "Process", live_claimer)
    assert kb.reclaim_task(conn, tid) is False
    assert seen == [os.getpid()]
    assert _row(conn, tid)["claim_lock"] == lock
    assert _events(conn, tid, "reclaim_refused")[-1]["reason"] == "liveness_unprovable"


@pytest.mark.parametrize("suffix, error", [
    ("not-a-pid", None),
    ("999994", PermissionError),
])
def test_unprovable_claimer_stays_held(conn, monkeypatch, suffix, error):
    lock = f"{kb._claimer_id().split(':', 1)[0]}:{suffix}"
    tid, _, _ = _running_card(conn, worker_pid=None, lock=lock)

    def inaccessible(pid):
        raise psutil.AccessDenied(pid)

    if error:
        monkeypatch.setattr(psutil, "Process", inaccessible)
    assert kb.reclaim_task(conn, tid) is False
    assert _row(conn, tid)["claim_lock"] == lock
    assert not _events(conn, tid, "reclaimed")


def test_foreign_claimer_without_worker_pid_is_not_probed(conn, monkeypatch):
    tid, _, _ = _running_card(conn, worker_pid=None, lock="other-host:999993")
    monkeypatch.setattr(psutil, "Process", lambda *_: pytest.fail("foreign PID was probed"))
    # The existing operator reclaim policy releases a foreign claim; do not
    # assert local death for a process on a host we cannot inspect.
    assert kb.reclaim_task(conn, tid) is True
    assert _events(conn, tid, "reclaimed")[-1]["host_local"] is False


# ---------------------------------------------------------------------------
# 1. The incident: pid-less running card must NOT be re-queued ready.
# ---------------------------------------------------------------------------


def test_reclaim_refuses_to_requeue_a_card_with_no_worker_pid(conn):
    """The exact ``t_09180e10`` shape: running, host-local lock, NULL worker_pid.

    The sweep cannot resolve a pid, so it has no death evidence. The card must
    stay ``running`` under its existing claim rather than going back to ready.
    """
    tid, lock, run_id = _running_card(conn, worker_pid=None)

    assert kb.reclaim_task(conn, tid, reason="worker sweep: no worker PID") is False

    row = _row(conn, tid)
    assert row["status"] == "running", (
        "a card whose liveness could not be proven was re-queued; this is the "
        "double-spawn path that landed two commits on t_09180e10"
    )
    assert row["claim_lock"] == lock, "the original owner's claim was released"
    assert row["current_run_id"] == run_id, "the original run was closed"
    assert not _events(conn, tid, "reclaimed"), "a reclaim was recorded anyway"


def test_cli_refuses_unproven_death_even_for_operator(conn):
    """An operator request cannot certify a still-launching worker dead."""
    tid, _lock, _run_id = _running_card(conn, worker_pid=None)
    refused = kc.run_slash(f"reclaim {tid} --reason operator-abort")
    assert "cannot reclaim" in refused
    assert _row(conn, tid)["status"] == "running"
    assert _events(conn, tid, "reclaim_refused")


def test_unprovable_liveness_surfaces_for_a_human(conn):
    """Failing closed must be VISIBLE, not silent — the card needs attention."""
    tid, _lock, _run_id = _running_card(conn, worker_pid=None)

    kb.reclaim_task(conn, tid, reason="worker sweep: no worker PID")

    refusals = _events(conn, tid, "reclaim_refused")
    assert len(refusals) == 1, "the refusal left no trace on the board"
    payload = refusals[0]
    assert payload.get("reason") == "liveness_unprovable"
    assert payload.get("prev_pid") is None
    assert payload.get("needs_attention") is True


def test_no_second_worker_spawns_after_a_refused_reclaim(conn, monkeypatch):
    """End-to-end: the dispatcher must not place a second worker on the card.

    This is the consequence the incident actually paid for — two runs editing
    one file — so it is asserted against ``dispatch_once`` and not just the
    status column.
    """
    tid, _lock, _run_id = _running_card(conn, worker_pid=None)
    kb.reclaim_task(conn, tid, reason="worker sweep: no worker PID")

    spawned: list[str] = []

    def _spawn(task, workspace, board=None):
        spawned.append(task.id)
        return 999999

    result = kb.dispatch_once(conn, spawn_fn=_spawn)

    assert tid not in spawned, "a SECOND worker was spawned onto a live card"
    assert tid not in [t for t, *_ in getattr(result, "spawned", [])]


# ---------------------------------------------------------------------------
# 2. Termination ordering: no re-queue without an attempted termination.
# ---------------------------------------------------------------------------


def test_reclaim_requeues_when_termination_actually_succeeded(conn, monkeypatch):
    """The guard must not strand cards whose worker was PROVEN terminated.

    Control case for the mutation check: with a resolvable pid that dies on
    SIGTERM, the reclaim proceeds exactly as before.
    """
    import signal as _signal

    tid, _lock, _run_id = _running_card(conn, worker_pid=4242)
    state = {"alive": True}
    signals: list[int] = []

    def _kill(pid, sig):
        signals.append(sig)
        if sig == _signal.SIGTERM:
            state["alive"] = False

    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: state["alive"])

    assert kb.reclaim_task(conn, tid, reason="operator", signal_fn=_kill) is True

    row = _row(conn, tid)
    assert row["status"] == "ready"
    assert row["claim_lock"] is None
    payload = _events(conn, tid, "reclaimed")[0]
    assert payload["termination_attempted"] is True
    assert payload["terminated"] is True
    assert signals == [_signal.SIGTERM]


def test_reclaim_holds_a_worker_that_survived_termination(conn, monkeypatch):
    """A signalled worker that refuses to die keeps its card. (Pre-existing
    ``_worker_survived_termination`` behaviour, pinned here so the new guard
    cannot regress it.)"""
    tid, lock, _run_id = _running_card(conn, worker_pid=4243)

    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)

    assert kb.reclaim_task(conn, tid, reason="operator", signal_fn=lambda *_: None) is False

    row = _row(conn, tid)
    assert row["status"] == "running"
    assert row["claim_lock"] == lock


def test_timeout_survivor_keeps_claim_and_rejects_a_second_claim(conn, monkeypatch):
    """SIGKILL delivery is not death proof; a live timed-out owner must hold."""
    tid = kb.create_task(conn, title="timeout survivor", assignee="daedalus-opus",
                         max_runtime_seconds=1)
    first = kb.claim_task(conn, tid)
    assert first is not None
    kb._set_worker_pid(conn, tid, 424242)
    old_run = first.current_run_id
    conn.execute("UPDATE task_runs SET started_at=? WHERE id=?",
                 (int(time.time()) - 500, old_run))
    conn.commit()
    signals = []
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(kb.time, "sleep", lambda seconds: None)
    assert kb.enforce_max_runtime(conn, signal_fn=lambda pid, sig: signals.append(sig)) == []
    assert len(signals) == 2
    assert _row(conn, tid)["status"] == "running"
    assert _row(conn, tid)["current_run_id"] == old_run
    assert kb.claim_task(conn, tid) is None


def test_timeout_second_claim_guard_after_unsafe_requeue(conn, monkeypatch):
    """Defence in depth if an old dispatcher has already released the claim."""
    tid = kb.create_task(conn, title="unsafe timeout release", assignee="daedalus-opus",
                         max_runtime_seconds=1)
    first = kb.claim_task(conn, tid)
    assert first is not None
    kb._set_worker_pid(conn, tid, 424242)
    run_id = first.current_run_id
    # Model an older binary that timed out a process without proving death.
    conn.execute("UPDATE task_runs SET status='timed_out', outcome='timed_out', "
                 "ended_at=?, worker_pid=NULL WHERE id=?", (int(time.time()), run_id))
    conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, "
                 "worker_pid=NULL, current_run_id=NULL WHERE id=?", (tid,))
    conn.commit()
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    assert kb.claim_task(conn, tid) is None
    spawned = []
    kb.dispatch_once(conn, spawn_fn=lambda task, workspace, board=None: spawned.append(task.id) or 999999)
    assert tid not in spawned
    assert _row(conn, tid)["status"] == "ready"
    assert _events(conn, tid, "claim_rejected")[-1]["reason"] == "prior_worker_still_alive"


def test_non_host_local_claim_is_still_reclaimable(conn):
    """A remote host's claim is not ours to prove dead or alive.

    We cannot signal another host's pid, so the pre-existing release path must
    stay open — the new guard is scoped to cards we OWN and cannot resolve.
    """
    tid, _lock, _run_id = _running_card(
        conn, worker_pid=4244, lock="some-other-host:777"
    )

    assert kb.reclaim_task(conn, tid, reason="operator") is True
    assert _row(conn, tid)["status"] == "ready"


# ---------------------------------------------------------------------------
# 3. Second-claim guard (defence in depth for when the above is wrong anyway).
# ---------------------------------------------------------------------------


def test_claim_refuses_a_card_whose_previous_worker_is_alive(conn, monkeypatch):
    """Even if a reclaim wrongly re-queues, the CLAIM is the last line.

    This is positive evidence of an active process, not merely a missing pid:
    the latter describes both a still-launching worker and a never-spawned card.
    """
    tid, lock, run_id = _running_card(conn, worker_pid=4242)
    kb._set_worker_pid(conn, tid, 4242)  # durable spawned event before reclaim
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == 4242)
    conn.execute(
        "UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, "
        "worker_pid=NULL, current_run_id=NULL WHERE id=?", (tid,),
    )
    conn.execute(
        "UPDATE task_runs SET status='reclaimed', outcome='reclaimed', ended_at=? "
        "WHERE id=?", (int(time.time()), run_id),
    )
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "reclaimed", {
            "manual": True, "prev_lock": lock, "retry_status": "ready",
            "prev_pid": 4242, "host_local": True,
            "termination_attempted": False, "terminated": False,
        }, run_id=run_id)

    assert kb.claim_task(conn, tid) is None
    assert any(r.get("reason") == "prior_worker_still_alive"
               for r in _events(conn, tid, "claim_rejected"))


def test_claim_refuses_late_spawn_after_reclaim(conn, monkeypatch):
    """Exact incident: spawn event arrives AFTER reclaim, run_id is NULL."""
    tid, lock, run_id = _running_card(conn, worker_pid=None)
    conn.execute(
        "UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, "
        "worker_pid=NULL, current_run_id=NULL WHERE id=?", (tid,),
    )
    conn.execute(
        "UPDATE task_runs SET status='reclaimed', outcome='reclaimed', ended_at=? "
        "WHERE id=?", (int(time.time()), run_id),
    )
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "reclaimed", {
            "prev_lock": lock, "prev_pid": None, "terminated": False,
            "termination_attempted": False, "retry_status": "ready",
        }, run_id=run_id)
    # _set_worker_pid is the real post-spawn call. The prior run was already
    # cleared, so it cannot stamp the original run_id or task_runs.worker_pid.
    kb._set_worker_pid(conn, tid, 4242)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == 4242)
    assert kb.claim_task(conn, tid) is None
    assert _row(conn, tid)["status"] == "ready"
    assert any(r.get("late_spawn") is True
               for r in _events(conn, tid, "claim_rejected"))


def test_claim_does_not_bypass_unproven_reclaim(conn):
    """A missing pid keeps the old claim; there is no ready card to claim."""
    tid, lock, _run_id = _running_card(conn, worker_pid=None)
    assert kb.reclaim_task(conn, tid, reason="operator abort") is False
    assert kb.claim_task(conn, tid) is None
    assert _row(conn, tid)["claim_lock"] == lock


def test_claim_allows_a_card_whose_worker_was_proven_terminated(conn):
    """The second-claim guard must not wedge the normal retry path.

    A reclaim that DID terminate its worker leaves ``terminated=True``; the
    card is free to be claimed again.
    """
    tid, lock, run_id = _running_card(conn, worker_pid=5150)
    conn.execute(
        "UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, "
        "worker_pid=NULL, current_run_id=NULL WHERE id=?",
        (tid,),
    )
    conn.execute(
        "UPDATE task_runs SET status='reclaimed', outcome='reclaimed', ended_at=? "
        "WHERE id=?",
        (int(time.time()), run_id),
    )
    with kb.write_txn(conn):
        kb._append_event(
            conn, tid, "reclaimed",
            {
                "manual": True,
                "prev_lock": lock,
                "retry_status": "ready",
                "prev_pid": 5150,
                "host_local": True,
                "termination_attempted": True,
                "terminated": True,
                "sigkill": False,
            },
            run_id=run_id,
        )

    claimed = kb.claim_task(conn, tid)
    assert claimed is not None, "a legitimately reclaimed card can no longer retry"
    assert _row(conn, tid)["status"] == "running"


def test_claim_allows_a_normal_never_reclaimed_card(conn):
    """Baseline: the guard is inert on the overwhelmingly common path."""
    tid = kb.create_task(conn, title="fresh", assignee="daedalus-opus")
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    conn.commit()

    assert kb.claim_task(conn, tid) is not None
