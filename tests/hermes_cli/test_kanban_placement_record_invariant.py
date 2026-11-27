"""Every worker run has a placement record; every run without a worker says why (t_4b313c15).

The invariant: a ``spawned`` event is never written without a ``placement`` event for the same run, in the same
transaction. Measured 14 days to 2026-11-11 over every board: one run broke it, landscaping/t_1073ffef run 34.
The operator ended run 34 (``scheduled``) one second after the claim; the ci-box spawn landed four seconds later;
``_set_worker_pid`` saw the claim lost, wrote ``spawned`` (late_spawn, claim_lost) and returned before the
``placement`` write. ``spawn_aborted`` then stopped the worker, but nothing recorded where it had run.

A run that starts no worker carries ``task_runs.no_worker_class``: ``operator`` (a human/human:* lane row),
``synthetic`` (a zero-duration run written for a card that was never claimed) or ``refused`` (claimed, closed
with no worker), so "no placement event" is never the only evidence.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kb.connect() as c:
        yield c


def _kinds(conn, run_id):
    return [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE run_id=? ORDER BY id", (run_id,))]


def _payload(conn, run_id, kind):
    row = conn.execute("SELECT payload FROM task_events WHERE run_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                       (run_id, kind)).fetchone()
    return json.loads(row[0]) if row and row[0] else None


def _cls(conn, run_id):
    return conn.execute("SELECT no_worker_class FROM task_runs WHERE id=?", (run_id,)).fetchone()[0]


def test_t1073ffef_run34_late_spawn_after_operator_close_still_writes_its_placement(conn):
    """The one real case: the run is ended by an operator transition while the spawn is in flight."""
    tid = kb.create_task(conn, title="operator closes the run mid-spawn", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    run_id = claimed.current_run_id
    assert kb.schedule_task(conn, tid, reason="operator hold", wake_at=2_000_000_000) is True
    record = {"run_id": run_id, "route": "remote-policy", "host": "ci-box", "route_class": "portable"}
    assert kbd._set_worker_pid(conn, tid, 80596, run_id=run_id, placement=record) is False
    kinds = _kinds(conn, run_id)
    assert "spawned" in kinds and "allocation" in kinds
    placed = _payload(conn, run_id, "allocation")
    assert placed["host"] == "ci-box" and placed["claim_lost"] is True
    # A worker existed, so the run is not a no-worker run, whatever its close said.
    assert _cls(conn, run_id) is None


def test_spawn_without_a_caller_record_still_writes_a_placement(conn):
    tid = kb.create_task(conn, title="caller passed no record", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    assert kbd._set_worker_pid(conn, tid, 4242, run_id=claimed.current_run_id) is True
    placed = _payload(conn, claimed.current_run_id, "allocation")
    assert placed is not None and placed["reason"] == "unrecorded" and placed["run_id"] == claimed.current_run_id


def test_worker_adopted_pid_writes_a_placement_when_the_dispatcher_wrote_none(conn, monkeypatch):
    tid = kb.create_task(conn, title="dispatcher died before the pid write", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    monkeypatch.setattr(kb, "_host_prefix", lambda: (claimed.claim_lock or "").split(":", 1)[0])
    assert kbd.adopt_worker_pid(conn, tid, claimed.current_run_id, 5151) is True
    placed = _payload(conn, claimed.current_run_id, "allocation")
    assert placed is not None and placed["entry"] == "worker_registered"


def test_worker_registering_after_its_run_was_closed_records_placement_and_clears_class(conn, monkeypatch):
    """Prism f065138133fd: claim R, close R mid-spawn, dispatcher dies before _set_worker_pid, worker starts."""
    tid = kb.create_task(conn, title="dispatcher died, run closed, worker started anyway", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    run_id = claimed.current_run_id
    assert kb.schedule_task(conn, tid, reason="operator hold", wake_at=2_000_000_000) is True
    assert _cls(conn, run_id) == "refused"
    monkeypatch.setattr(kb, "_host_prefix", lambda: (claimed.claim_lock or "").split(":", 1)[0])
    assert kbd.adopt_worker_pid(conn, tid, run_id, 9191) is False
    kinds = _kinds(conn, run_id)
    assert "worker_registered" in kinds and kinds.count("allocation") == 1
    placed = _payload(conn, run_id, "allocation")
    assert placed["entry"] == "worker_registered" and placed["claim_lost"] is True
    assert _payload(conn, run_id, "worker_registered")["claim_lost"] is True
    assert _cls(conn, run_id) is None
    # The card's pid is not touched by a losing worker.
    assert conn.execute("SELECT worker_pid FROM tasks WHERE id=?", (tid,)).fetchone()[0] is None


def test_worker_with_a_run_id_of_another_task_records_nothing(conn):
    tid = kb.create_task(conn, title="a", assignee="worker")
    other = kb.create_task(conn, title="b", assignee="worker")
    run_id = kb.claim_task(conn, other).current_run_id
    assert kbd.adopt_worker_pid(conn, tid, run_id, 9292) is False
    assert "allocation" not in _kinds(conn, run_id)


def test_placement_is_never_written_twice_for_one_run(conn, monkeypatch):
    tid = kb.create_task(conn, title="adopt after a normal spawn", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    assert kbd._set_worker_pid(conn, tid, 6161, run_id=claimed.current_run_id, placement={"host": "ace-ai"}) is True
    monkeypatch.setattr(kb, "_host_prefix", lambda: (claimed.claim_lock or "").split(":", 1)[0])
    kbd.adopt_worker_pid(conn, tid, claimed.current_run_id, 6161)
    assert _kinds(conn, claimed.current_run_id).count("allocation") == 1


def test_operator_verdict_row_is_classed_operator(conn):
    tid = kb.create_task(conn, title="human review verdict", assignee="human:apollo")
    with kb.write_txn(conn):
        run_id = kb._synthesize_ended_run(conn, tid, outcome="completed", summary="approved")
    assert _cls(conn, run_id) == "operator"


def test_never_claimed_close_is_classed_synthetic(conn):
    tid = kb.create_task(conn, title="closed without a claim", assignee="worker")
    with kb.write_txn(conn):
        run_id = kb._synthesize_ended_run(conn, tid, outcome="completed", summary="superseded")
    assert _cls(conn, run_id) == "synthetic"


def test_claimed_then_closed_before_any_spawn_is_classed_refused(conn):
    tid = kb.create_task(conn, title="refused before spawn", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    with kb.write_txn(conn):
        kb._end_run(conn, tid, outcome="workspace_refused", status="workspace_refused")
    assert _cls(conn, claimed.current_run_id) == "refused"


def test_a_run_that_spawned_is_not_classed(conn):
    tid = kb.create_task(conn, title="normal worker", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    assert kbd._set_worker_pid(conn, tid, 7171, run_id=claimed.current_run_id, placement={"host": "ace-ai"}) is True
    with kb.write_txn(conn):
        kb._end_run(conn, tid, outcome="completed", status="done")
    assert _cls(conn, claimed.current_run_id) is None


def test_claimed_human_lane_close_is_classed_operator(conn):
    """2,412 of the 14-day rows: a review-lane run under a human profile, closed with changes_requested."""
    tid = kb.create_task(conn, title="human lane claimed", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET profile='human:apollo' WHERE id=?", (claimed.current_run_id,))
        kb._end_run(conn, tid, outcome="changes_requested", status="changes_requested")
    assert _cls(conn, claimed.current_run_id) == "operator"


def test_a_repeated_set_worker_pid_never_writes_a_second_placement(conn):
    """Prism 532aad7ea955: a second _set_worker_pid on the same run appended a second record."""
    tid = kb.create_task(conn, title="pid stamped twice", assignee="worker")
    run_id = kb.claim_task(conn, tid).current_run_id
    assert kbd._set_worker_pid(conn, tid, 1111, run_id=run_id, placement={"host": "ace-ai", "reason": "policy"})
    assert kbd._set_worker_pid(conn, tid, 1111, run_id=run_id) is True
    assert _kinds(conn, run_id).count("allocation") == 1
    assert _payload(conn, run_id, "allocation")["host"] == "ace-ai"


def test_adopt_then_set_without_run_id_replaces_the_placeholder(conn, monkeypatch):
    """Prism 532aad7ea955 / operator c-ec799aedac187b1f: _set_worker_pid without run_id writes against the
    resolved launching run, so the worker's placeholder is replaced, never joined by a second record."""
    tid = kb.create_task(conn, title="worker adopted first", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    run_id = claimed.current_run_id
    monkeypatch.setattr(kb, "_host_prefix", lambda: (claimed.claim_lock or "").split(":", 1)[0])
    assert kbd.adopt_worker_pid(conn, tid, run_id, 2222) is True
    assert kbd._set_worker_pid(conn, tid, 2222, placement={"host": "ace-ai", "reason": "policy"}) is True
    assert _kinds(conn, run_id).count("allocation") == 1
    assert _payload(conn, run_id, "allocation")["host"] == "ace-ai"



def test_adopt_then_a_launch_with_no_pid_leaves_one_full_placement(conn, monkeypatch):
    """Prism-direct P1 (operator 01:4x 11-12): a spawn that reports no pid appended its record after a worker's
    ``unrecorded`` placeholder, so the run had two placement rows, the placeholder first."""
    tid = kb.create_task(conn, title="worker adopted, spawn returned no pid", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    run_id = claimed.current_run_id
    monkeypatch.setattr(kb, "_host_prefix", lambda: (claimed.claim_lock or "").split(":", 1)[0])
    assert kbd.adopt_worker_pid(conn, tid, run_id, 3333) is True
    assert _payload(conn, run_id, "allocation")["reason"] == "unrecorded"
    monkeypatch.setattr(kbd, "_book_local_start", lambda *a: None)
    monkeypatch.setattr("hermes_cli.kanban_continuation.record_placement", lambda *a: True)
    full = {"run_id": run_id, "host": "ace-ai", "route_class": "local", "reason": "policy", "entry": "dispatch"}
    assert kbd._launch_claimed(conn, claimed, "/scratch", board=None, spawn=lambda *a, **k: None,
                               spawn_kwargs={}, host=None, pool=None, placement_record=lambda: dict(full),
                               state=kbd._LaunchState()) == kbd._LAUNCH_OK
    assert _kinds(conn, run_id).count("allocation") == 1
    assert _payload(conn, run_id, "allocation") == full

@pytest.mark.parametrize("where", ["record", "commit"])
def test_a_lost_claim_returns_false_even_when_its_record_cannot_be_written(conn, monkeypatch, where):
    """Prism 5c5caf4aae3d / operator c-b6a31ddd13a9c5e9: the losing verdict must not depend on the audit write.
    An exception from the record, or from the transaction's commit, used to reach
    register_current_worker_from_env, whose handler returns True, and the losing worker started."""
    tid = kb.create_task(conn, title="lost claim, disk full", assignee="worker")
    run_id = kb.claim_task(conn, tid).current_run_id
    assert kb.schedule_task(conn, tid, reason="operator hold", wake_at=2_000_000_000) is True
    if where == "record":
        def boom(*a, **k):
            raise sqlite3.OperationalError("database or disk is full")
        monkeypatch.setattr(kbd, "_record_losing_worker", boom)
    else:
        real = kb.write_txn

        @contextlib.contextmanager
        def failing_commit(c, **kw):
            with real(c, **kw):
                yield c
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(kb, "write_txn", failing_commit)
    assert kbd.adopt_worker_pid(conn, tid, run_id, 9393) is False


def test_an_owning_worker_still_sees_a_board_error(conn, monkeypatch):
    """Only the LOST verdict is shielded: an owning worker's write error still propagates (caller decides)."""
    tid = kb.create_task(conn, title="owner, board error", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    monkeypatch.setattr(kb, "_host_prefix", lambda: (claimed.claim_lock or "").split(":", 1)[0])
    monkeypatch.setattr(kbd, "_unrecorded_placement", lambda *a, **k: (_ for _ in ()).throw(
        sqlite3.OperationalError("database or disk is full")))
    with pytest.raises(sqlite3.OperationalError):
        kbd.adopt_worker_pid(conn, tid, claimed.current_run_id, 9494)


def _pid_only_legacy_run(conn, title):
    """A run whose only worker evidence is task_runs.worker_pid (no spawned / worker_registered event)."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    run_id = kb.claim_task(conn, tid).current_run_id
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET worker_pid = 4040 WHERE id = ?", (run_id,))
    return tid, run_id


def test_reaper_reads_worker_pid_before_clearing_it(conn, monkeypatch):
    """Prism 31803cfc0dc0 / operator c-cd19e655c8f5b36d: end_orphaned_terminal_runs nulls worker_pid in its close
    UPDATE, then stamped the class, so a pid-only legacy run read as `refused`."""
    tid, run_id = _pid_only_legacy_run(conn, "orphaned terminal run")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    assert run_id in kb.end_orphaned_terminal_runs(conn)
    assert _cls(conn, run_id) is None


def test_reclaim_dangling_run_reads_worker_pid_before_clearing_it(conn):
    """Same ordering in _reclaim_dangling_run."""
    tid, run_id = _pid_only_legacy_run(conn, "dangling run")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        kb._reclaim_dangling_run(conn, tid, statuses=("ready",), now=int(kb.time.time()), note="test")
    assert conn.execute("SELECT ended_at FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0] is not None
    assert _cls(conn, run_id) is None
