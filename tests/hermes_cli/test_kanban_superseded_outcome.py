"""``superseded`` as a first-class terminal outcome for an already-satisfied premise.

A worker that verifies its card's work already landed has no honest verb today:
``complete_task`` demands evidence of work it did not do and ``block_task`` demands a
blocker that does not exist, so it exits rc=0 and the dispatcher books a protocol
violation + retries. ``superseded_by`` closes the card ``done`` with
``outcome='superseded'`` and a mandatory evidence pointer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="already fixed upstream") -> str:
    tid = kb.create_task(conn, title=title, assignee="coder")
    assert kb.claim_task(conn, tid, claimer=kb._claimer_id()) is not None
    return tid


# ---------------------------------------------------------------------------
# Kernel: the honest verb
# ---------------------------------------------------------------------------


def test_superseded_closes_done_with_pointer_and_no_work_evidence(kanban_home):
    """The pointer IS the evidence: no summary/result needed, one run, no failures."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.complete_task(conn, tid, superseded_by="t_0c5ac29a -> #889") is True

        row = conn.execute(
            "SELECT status, consecutive_failures FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert row["status"] == "done"
        assert row["consecutive_failures"] == 0

        runs = kb.list_runs(conn, tid)
        assert len(runs) == 1, "a superseded close must not need a retry"
        assert runs[0].outcome == "superseded"
        assert (runs[0].metadata or {}).get("superseded_by") == "t_0c5ac29a -> #889"

        completed = [e for e in kb.list_events(conn, tid) if e.kind == "completed"]
        assert len(completed) == 1
        assert (completed[0].payload or {}).get("superseded_by") == "t_0c5ac29a -> #889"


def test_superseded_without_pointer_is_refused(kanban_home):
    """A superseded card with no pointer is just a silent delete of work."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        for blank in ("", "   "):
            with pytest.raises(kb.EmptySupersedeError):
                kb.complete_task(conn, tid, superseded_by=blank)
        assert conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (tid,)
        ).fetchone()["status"] == "running"
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "completion_blocked_empty_supersede" in kinds
        assert "completed" not in kinds


def test_superseded_keeps_an_explicit_summary(kanban_home):
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.complete_task(
            conn, tid, summary="verified on main: the test file is already fixed",
            superseded_by="https://github.com/o/r/pull/889",
        ) is True
        run = kb.list_runs(conn, tid)[0]
        assert run.outcome == "superseded"
        assert run.summary == "verified on main: the test file is already fixed"


def test_superseded_run_counts_as_a_success_downstream(kanban_home):
    """Parent handoff context and the respawn guard must see a superseded run."""
    with kb.connect_closing() as conn:
        parent = _running_task(conn, title="parent already landed")
        assert kb.complete_task(conn, parent, superseded_by="#889") is True
        child = kb.create_task(conn, title="child", assignee="coder", parents=[parent])

        ctx = kb.build_worker_context(conn, child)
        assert parent in ctx
        assert "(no result recorded)" not in ctx

        # A card closed superseded must not be immediately respawned.
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
        conn.commit()
        assert kb.check_respawn_guard(conn, parent) is not None


def test_plain_completion_outcome_is_unchanged(kanban_home):
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.complete_task(conn, tid, summary="did the work") is True
        run = kb.list_runs(conn, tid)[0]
        assert run.outcome == "completed"
        assert "superseded_by" not in (run.metadata or {})


# ---------------------------------------------------------------------------
# Dispatcher: a REPRODUCED clean exit stops retrying instead of burning 3 slots
# ---------------------------------------------------------------------------


def _drive_clean_exit(conn, tid, fake_pid, worker_output):
    """One clean-exit (rc=0) reaper pass whose worker log says ``worker_output``."""
    import hermes_cli.kanban_db as _kb

    host_prefix = _kb._claimer_id().split(":", 1)[0]
    assert _kb.claim_task(conn, tid, claimer=f"{host_prefix}:mock") is not None
    _kb._set_worker_pid(conn, tid, fake_pid)
    _kb._record_worker_exit(fake_pid, 0)  # os.W_EXITCODE(0, 0) == 0
    original_alive = _kb._pid_alive
    original_tail = _kb._worker_log_stderr_tail
    _kb._pid_alive = lambda p: False
    _kb._worker_log_stderr_tail = lambda task_id, board=None: worker_output or None
    try:
        return _kb.detect_crashed_workers(conn)
    finally:
        _kb._pid_alive = original_alive
        _kb._worker_log_stderr_tail = original_tail


def test_reproduced_clean_exit_stops_after_two_runs(kanban_home):
    """The measured bug: 3 identical clean exits per card, booked as a crash.

    The second IDENTICAL clean exit is a reproduced no-op; a third spawn buys
    nothing, so the card is held for input after TWO runs, not three.
    """
    nothing_to_do = "Nothing to implement: the file is already fixed on main by #889."
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="already landed", assignee="coder")

        _drive_clean_exit(conn, tid, 992001, nothing_to_do)
        assert kb.get_task(conn, tid).status == "ready", "first clean exit still retries"

        _drive_clean_exit(conn, tid, 992002, nothing_to_do)
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", "an identical second clean exit must stop the loop"

        gave_up = [e for e in kb.list_events(conn, tid) if e.kind == "gave_up"]
        assert len(gave_up) == 1
        payload = gave_up[0].payload or {}
        assert payload.get("stopped_early") == "reproduced_clean_exit"
        assert payload.get("identical_violations") == 2
        assert "--superseded-by" in (task.last_failure_error or "")
        assert len(kb.list_runs(conn, tid)) == 2, "must not burn a third worker slot"


def test_differing_clean_exits_keep_the_full_retry_budget(kanban_home):
    """Only a REPRODUCED no-op trips early; genuinely different exits still retry."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="flaky paperwork", assignee="coder")
        for i, pid in enumerate((993001, 993002)):
            _drive_clean_exit(conn, tid, pid, f"ran step {i}, forgot the handoff")
            assert kb.get_task(conn, tid).status == "ready"
        _drive_clean_exit(conn, tid, 993003, "third distinct ending")
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", "budget still bounded at the violation limit"
        payload = [e for e in kb.list_events(conn, tid) if e.kind == "gave_up"][0].payload or {}
        assert payload.get("stopped_early") is None
        assert payload.get("protocol_violations") == kb._PROTOCOL_VIOLATION_FAILURE_LIMIT


def test_missing_worker_output_never_counts_as_identical(kanban_home):
    """Two runs with no recorded output say nothing about each other."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="no logs", assignee="coder")
        _drive_clean_exit(conn, tid, 994001, "")
        _drive_clean_exit(conn, tid, 994002, "")
        assert kb.get_task(conn, tid).status == "ready", (
            "absent output must not be read as a reproduced no-op"
        )
