"""Class tests: no release path may put a second worker beside a live owner.

Round-2 review of t_09180e10's fix found three doors that still did:

A. dashboard ``running -> ready`` released the claim BEFORE trying to kill the
   worker and ignored the result;
B. ``reconcile_orphaned_running`` requeued a host-local claim whose pid had
   not been stamped yet (spawn still in flight);
C. ``claim_review_task`` had no prior-live-worker guard at all.

The claim guard also keyed on a ``reclaimed`` EVENT, so any releaser that
wrote a different event kind (dashboard ``status``, reconcile ``reconciled``,
parent-reopen ``descendant_invalidated``) walked straight past it.

Every test below crosses the real ``dispatch_once`` spawn boundary with only
the process spawner stubbed, and each negative case has a proven-dead control
so the guard cannot pass by wedging everything.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time

import pytest

from hermes_cli import kanban_db as kb
from plugins.kanban.dashboard import plugin_api as api


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    import hermes_cli.profiles as _profiles

    # Otherwise dispatch skips non-profile assignees and every "no second
    # spawn" assertion passes vacuously.
    monkeypatch.setattr(_profiles, "profile_exists", lambda name: True)
    with kb.connect() as c:
        yield c


def _events(conn, tid, kind):
    return [
        json.loads(r["payload"]) if r["payload"] else {}
        for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=? "
            "ORDER BY id", (tid, kind),
        )
    ]


def _row(conn, tid):
    return conn.execute(
        "SELECT status, claim_lock, worker_pid, current_run_id FROM tasks "
        "WHERE id=?", (tid,),
    ).fetchone()


def _dispatch(conn):
    spawned: list[str] = []

    def spawn(task, workspace, board=None):
        spawned.append(task.id)
        return 777001

    result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=20)
    return spawned, result


def _live_claim(conn, title, pid=424242):
    tid = kb.create_task(conn, title=title, assignee="worker")
    first = kb.claim_task(conn, tid)
    assert first is not None
    kb._set_worker_pid(conn, tid, pid)
    return tid, first


# ---------------------------------------------------------------------------
# A. Dashboard direct move.
# ---------------------------------------------------------------------------


def _survived(pid, lock, **_kw):
    return {"prev_pid": pid, "prev_lock": lock, "host_local": True,
            "termination_attempted": True, "terminated": False,
            "sigkill": True}


def _killed(pid, lock, **_kw):
    return {"prev_pid": pid, "prev_lock": lock, "host_local": True,
            "termination_attempted": True, "terminated": True,
            "sigkill": False}


def test_dashboard_move_refuses_release_when_worker_survives(conn, monkeypatch):
    tid, _first = _live_claim(conn, "dashboard vs survivor")
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _survived)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)

    assert api._set_status_direct(conn, tid, "ready") is False
    row = _row(conn, tid)
    assert row["status"] == "running" and row["worker_pid"] == 424242
    refused = _events(conn, tid, "reclaim_refused")
    assert refused and refused[-1]["needs_attention"] is True

    spawned, _ = _dispatch(conn)
    assert tid not in spawned


def test_dashboard_move_refuses_unstamped_host_local_claim(conn, monkeypatch):
    """Spawn still in flight: NULL pid on our own claim is not death."""
    tid = kb.create_task(conn, title="dashboard vs in-flight", assignee="worker")
    assert kb.claim_task(conn, tid) is not None
    assert api._set_status_direct(conn, tid, "ready") is False
    assert _row(conn, tid)["status"] == "running"
    assert _events(conn, tid, "reclaim_refused")[-1]["reason"] == "liveness_unprovable"


def test_dashboard_move_proceeds_when_worker_proven_dead(conn, monkeypatch):
    tid, _first = _live_claim(conn, "dashboard control")
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _killed)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)

    assert api._set_status_direct(conn, tid, "ready") is True
    assert _row(conn, tid)["status"] == "ready"
    spawned, _ = _dispatch(conn)
    assert tid in spawned


# ---------------------------------------------------------------------------
# Claim guard uses spawned owner evidence, regardless of run outcome/event.
# ---------------------------------------------------------------------------


def test_claim_guard_catches_live_owner_released_without_reclaimed_event(
    conn, monkeypatch,
):
    """A releaser that LIES about termination (here: dashboard, which writes a
    'status' event, never 'reclaimed') must still be stopped at the claim."""
    tid, _first = _live_claim(conn, "lying releaser")
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _killed)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == 424242)
    assert api._set_status_direct(conn, tid, "ready") is True
    assert not _events(conn, tid, "reclaimed")

    spawned, _ = _dispatch(conn)
    assert tid not in spawned
    assert _row(conn, tid)["status"] == "ready"
    rej = _events(conn, tid, "claim_rejected")
    assert rej and rej[-1]["reason"] == "prior_worker_still_alive"
    assert rej[-1]["prev_pid"] == 424242


@pytest.mark.parametrize("release", ["block", "review", "complete", "changes"])
def test_operator_terminal_outcome_does_not_certify_worker_exit(conn, monkeypatch, release):
    tid, first = _live_claim(conn, "operator release")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == 424242)
    if release == "block":
        assert kb.block_task(conn, tid, reason="operator")
        assert kb.unblock_task(conn, tid)
    elif release == "review":
        assert kb.request_review(conn, tid, summary="operator", reviewer="worker", force=True)
    elif release == "complete":
        assert kb.complete_task(conn, tid, summary="operator")
        assert api._set_status_direct(conn, tid, "ready")
        assert kb.claim_task(conn, tid) is None
        assert _events(conn, tid, "claim_rejected")[-1]["reason"] == "prior_worker_still_alive"
        return
    else:
        assert kb.request_review(conn, tid, summary="for review", reviewer="worker",
                                 expected_run_id=first.current_run_id)
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
        review = kb.claim_review_task(conn, tid)
        assert review is not None
        kb._set_worker_pid(conn, tid, 525252)
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == 525252)
        assert kb.request_changes(conn, tid, reason="operator")
    spawned, _ = _dispatch(conn)
    assert tid not in spawned
    assert _events(conn, tid, "claim_rejected")[-1]["reason"] == "prior_worker_still_alive"


def test_ended_synthetic_run_cannot_mask_older_live_owner(conn, monkeypatch):
    tid, _ = _live_claim(conn, "synthetic mask")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == 424242)
    _external_release(conn, tid)
    assert kb.claim_task(conn, tid) is None
    assert kb.block_task(conn, tid, reason="operator pause")
    assert kb.unblock_task(conn, tid)
    spawned, _ = _dispatch(conn)
    assert tid not in spawned
    assert _events(conn, tid, "claim_rejected")[-1]["prev_pid"] == 424242


def test_real_process_survives_operator_block_without_second_spawn(conn):
    tid = kb.create_task(conn, title="real PID operator release", assignee="worker")
    assert kb.claim_task(conn, tid) is not None
    proc = subprocess.Popen(["/bin/sleep", "30"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        kb._set_worker_pid(conn, tid, proc.pid)
        assert kb.block_task(conn, tid, reason="operator pause")
        assert kb.unblock_task(conn, tid)
        spawned, _ = _dispatch(conn)
        assert proc.poll() is None
        assert tid not in spawned
        assert _events(conn, tid, "claim_rejected")[-1]["prev_pid"] == proc.pid
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_terminal_release_allows_proven_dead_worker(conn, monkeypatch):
    tid, _ = _live_claim(conn, "dead self-release")
    assert kb.block_task(conn, tid, reason="operator")
    assert kb.unblock_task(conn, tid)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    spawned, _ = _dispatch(conn)
    assert tid in spawned


# ---------------------------------------------------------------------------
# B. Orphan reconciliation.
# ---------------------------------------------------------------------------


def test_reconcile_refuses_host_local_claim_with_unstamped_pid(conn):
    tid = kb.create_task(conn, title="orphan in flight", assignee="worker")
    first = kb.claim_task(conn, tid)
    assert first is not None and first.worker_pid is None
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET claim_expires=NULL WHERE id=?", (tid,))

    spawned, result = _dispatch(conn)
    assert tid not in result.reconciled_orphans
    assert tid not in spawned
    row = _row(conn, tid)
    assert row["status"] == "running"
    assert row["current_run_id"] == first.current_run_id
    # Surfaced once per run, not once per tick.
    _dispatch(conn)
    refused = _events(conn, tid, "reconcile_refused")
    assert len(refused) == 1 and refused[0]["needs_attention"] is True


def test_reconcile_still_requeues_an_unowned_orphan(conn):
    """Control: no claim lock at all -> nobody to wait for; requeue as before."""
    tid = kb.create_task(conn, title="lockless orphan", assignee="worker")
    assert kb.claim_task(conn, tid) is not None
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET claim_lock=NULL WHERE id=?", (tid,))

    spawned, result = _dispatch(conn)
    assert tid in result.reconciled_orphans
    assert tid in spawned


# ---------------------------------------------------------------------------
# C. Review claim door.
# ---------------------------------------------------------------------------


def _review_run_released_unsafely(conn):
    tid = kb.create_task(conn, title="review retry", assignee="builder")
    builder = kb.claim_task(conn, tid)
    assert builder is not None
    assert kb.request_review(conn, tid, summary="for review", reviewer="argus",
                             expected_run_id=builder.current_run_id)
    review = kb.claim_review_task(conn, tid)
    assert review is not None
    kb._set_worker_pid(conn, tid, 525252)
    # A releaser that did not prove death (older binary / other host).
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET status='timed_out', outcome='timed_out', "
            "ended_at=?, worker_pid=NULL, claim_lock=NULL, claim_expires=NULL "
            "WHERE id=?", (int(time.time()), review.current_run_id))
        conn.execute(
            "UPDATE tasks SET status='review', worker_pid=NULL, claim_lock=NULL, "
            "claim_expires=NULL, current_run_id=NULL WHERE id=?", (tid,))
    return tid


def test_review_claim_refuses_live_prior_reviewer(conn, monkeypatch):
    tid = _review_run_released_unsafely(conn)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == 525252)

    spawned, _ = _dispatch(conn)
    assert tid not in spawned
    assert _row(conn, tid)["status"] == "review"
    rej = _events(conn, tid, "claim_rejected")
    assert rej and rej[-1]["source_status"] == "review"
    assert rej[-1]["prev_pid"] == 525252


def test_review_claim_allows_dead_prior_reviewer(conn, monkeypatch):
    tid = _review_run_released_unsafely(conn)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)

    spawned, _ = _dispatch(conn)
    assert tid in spawned


# ---------------------------------------------------------------------------
# Spawn fence: a launch whose claim was released mid-flight must not run.
# This closes the in-flight window for EVERY releaser at once (the measured
# t_09180e10 mechanism: spawned landed 5 s after the reclaim).
# ---------------------------------------------------------------------------


def _external_release(conn, tid):
    """Some releaser (reclaim / reconcile / dashboard / parent reopen) wins
    the race while the launcher is still setting up the workspace."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='ready', claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL WHERE id=?", (tid,))
        kb._end_run(conn, tid, outcome="reclaimed", status="reclaimed",
                    error="external release during spawn")


def test_launch_aborts_when_claim_released_before_spawn(conn, monkeypatch):
    tid = kb.create_task(conn, title="released during setup", assignee="worker")
    kb.recompute_ready(conn)
    real_tip = kb._maybe_emit_scratch_tip

    def tip_then_release(c, task_id, kind):
        real_tip(c, task_id, kind)
        if task_id == tid:
            _external_release(c, tid)

    monkeypatch.setattr(kb, "_maybe_emit_scratch_tip", tip_then_release)
    spawned: list[str] = []
    kb.dispatch_once(
        conn, max_spawn=1,
        spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or 1,
    )
    assert spawned == []
    aborted = _events(conn, tid, "spawn_aborted")
    assert aborted and aborted[-1]["reason"] == "claim_lost_before_spawn"


def test_late_spawn_is_terminated_and_never_stamped(conn, monkeypatch):
    tid = kb.create_task(conn, title="released during spawn", assignee="worker")
    kb.recompute_ready(conn)
    killed: list[int] = []

    def kill(pid, lock, **_kw):
        killed.append(pid)
        return _killed(pid, lock)

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", kill)

    def spawn(task, ws, board=None):
        _external_release(conn, task.id)
        return 434343

    result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1)
    assert killed == [434343]
    assert tid not in [t for t, *_ in result.spawned]
    assert _row(conn, tid)["worker_pid"] is None
    aborted = _events(conn, tid, "spawn_aborted")
    assert aborted[-1]["reason"] == "claim_lost_after_spawn"
    # The orphan is still visible to the claim guard via its spawned event.
    assert any(e.get("late_spawn") for e in _events(conn, tid, "spawned"))


def test_fenced_late_pid_never_overwrites_successor(conn):
    """Round-2 finding B detail: late _set_worker_pid overwrote 434343->424242."""
    tid = kb.create_task(conn, title="successor pid", assignee="worker")
    first = kb.claim_task(conn, tid)
    assert first is not None
    _external_release(conn, tid)
    second = kb.claim_task(conn, tid)
    assert second is not None
    assert kb._set_worker_pid(conn, tid, 434343,
                              run_id=second.current_run_id) is True
    assert kb._set_worker_pid(conn, tid, 424242,
                              run_id=first.current_run_id) is False
    assert _row(conn, tid)["worker_pid"] == 434343


# ---------------------------------------------------------------------------
# Round-4 review: prior-owner shapes the ended-run scan could not see.
# ---------------------------------------------------------------------------


def _leak_open_run(conn, tid, status="ready"):
    """A partial release: ownership columns cleared, the run left OPEN.

    claim_task's invariant recovery closes such a run after the liveness check,
    so the guard must inspect it before that happens."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status=?, claim_lock=NULL, claim_expires=NULL, "
            "worker_pid=NULL WHERE id=?", (status, tid),
        )


def _run_ended_at(conn, run_id):
    return conn.execute(
        "SELECT ended_at FROM task_runs WHERE id=?", (run_id,),
    ).fetchone()["ended_at"]


def test_open_leaked_run_with_real_live_owner_blocks_dispatch(conn):
    tid = kb.create_task(conn, title="leaked open run", assignee="worker")
    first = kb.claim_task(conn, tid)
    assert first is not None
    proc = subprocess.Popen(["/bin/sleep", "30"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        kb._set_worker_pid(conn, tid, proc.pid, run_id=first.current_run_id)
        _leak_open_run(conn, tid)
        assert _run_ended_at(conn, first.current_run_id) is None

        spawned, _ = _dispatch(conn)
        assert proc.poll() is None
        assert tid not in spawned
        rej = _events(conn, tid, "claim_rejected")[-1]
        assert rej["reason"] == "prior_worker_still_alive"
        assert rej["prev_pid"] == proc.pid and rej["prev_run_open"] is True
        # Refused before invariant recovery: the live owner's run stays open.
        assert _run_ended_at(conn, first.current_run_id) is None
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_open_leaked_run_with_dead_owner_is_recovered_and_dispatched(conn, monkeypatch):
    tid, first = _live_claim(conn, "leaked open run, dead owner")
    _leak_open_run(conn, tid)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)

    spawned, _ = _dispatch(conn)
    assert tid in spawned
    assert _run_ended_at(conn, first.current_run_id) is not None
    assert _row(conn, tid)["current_run_id"] != first.current_run_id


def test_open_leaked_run_live_owner_blocks_review_claim(conn, monkeypatch):
    tid, first = _live_claim(conn, "leaked open run, review door")
    _leak_open_run(conn, tid, status="review")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == 424242)

    assert kb.claim_review_task(conn, tid) is None
    rej = _events(conn, tid, "claim_rejected")[-1]
    assert rej["source_status"] == "review" and rej["prev_run_open"] is True
    assert _row(conn, tid)["status"] == "review"

    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    assert kb.claim_review_task(conn, tid) is not None


@pytest.mark.parametrize("alive_pid", [424242, 434343])
def test_every_spawn_of_one_run_is_probed(conn, monkeypatch, alive_pid):
    """A run stamped twice: whichever PID still lives blocks the claim; a dead
    sibling stamp must not vouch for it (older- or newer-alive)."""
    tid, first = _live_claim(conn, "double stamp")
    kb._set_worker_pid(conn, tid, 434343, run_id=first.current_run_id)
    assert len(_events(conn, tid, "spawned")) == 2
    _external_release(conn, tid)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == alive_pid)

    spawned, _ = _dispatch(conn)
    assert tid not in spawned
    assert _events(conn, tid, "claim_rejected")[-1]["prev_pid"] == alive_pid


def test_double_stamped_run_with_all_owners_dead_is_dispatched(conn, monkeypatch):
    tid, first = _live_claim(conn, "double stamp, both dead")
    kb._set_worker_pid(conn, tid, 434343, run_id=first.current_run_id)
    _external_release(conn, tid)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)

    spawned, _ = _dispatch(conn)
    assert tid in spawned


# ---------------------------------------------------------------------------
# TTL expiry: the original missing-PID rule on the release_stale_claims path.
# ---------------------------------------------------------------------------


def _expire_claim(conn, tid):
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET claim_expires=? WHERE id=?",
            (int(time.time()) - 60, tid),
        )


def test_ttl_expiry_holds_host_local_claim_with_no_worker_pid(conn):
    """No PID is unknown liveness, not death: TTL expiry must not requeue it
    (and has nothing it could signal)."""
    tid = kb.create_task(conn, title="ttl null pid", assignee="worker")
    first = kb.claim_task(conn, tid)
    assert first is not None and _row(conn, tid)["worker_pid"] is None
    _expire_claim(conn, tid)
    signals = []

    assert kb.release_stale_claims(
        conn, signal_fn=lambda pid, sig: signals.append((pid, sig)),
    ) == 0
    assert signals == []
    row = _row(conn, tid)
    assert row["status"] == "running" and row["claim_lock"] == first.claim_lock
    assert row["current_run_id"] == first.current_run_id
    assert not _events(conn, tid, "reclaimed")

    spawned, _ = _dispatch(conn)
    assert tid not in spawned
    assert _row(conn, tid)["current_run_id"] == first.current_run_id


def test_ttl_expiry_requeues_host_local_claim_whose_worker_is_proven_dead(
    conn, monkeypatch,
):
    tid, first = _live_claim(conn, "ttl dead pid")
    _expire_claim(conn, tid)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)

    def gone(pid, sig):
        raise ProcessLookupError(pid)

    assert kb.release_stale_claims(conn, signal_fn=gone) == 1
    assert _row(conn, tid)["status"] == "ready"
    reclaimed = _events(conn, tid, "reclaimed")[-1]
    assert reclaimed["terminated"] is True and reclaimed["worker_pid"] == 424242

    spawned, _ = _dispatch(conn)
    assert tid in spawned
    assert _row(conn, tid)["current_run_id"] != first.current_run_id
