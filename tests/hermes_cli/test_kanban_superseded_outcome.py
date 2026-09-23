"""``superseded`` as a first-class terminal outcome for an already-satisfied premise.

A worker that verifies its card's work already landed has no honest verb today:
``complete_task`` demands evidence of work it did not do and ``block_task`` demands a
blocker that does not exist, so it exits rc=0 and the dispatcher books a protocol
violation + 3 retries. ``superseded_by`` closes the card ``done`` with
``outcome='superseded'`` and a mandatory evidence pointer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as c:
        yield c


def _running_task(conn, title="already fixed upstream") -> str:
    tid = kb.create_task(conn, title=title, assignee="coder")
    assert kb.claim_task(conn, tid, claimer=kb._claimer_id()) is not None
    return tid


def test_superseded_closes_done_with_pointer_and_no_work_evidence(conn):
    """The pointer IS the evidence: no summary/result needed, one run, no failures."""
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


def test_superseded_without_pointer_is_refused(conn):
    """A superseded card with no pointer is just a silent delete of work."""
    tid = _running_task(conn)
    for blank in ("", "   "):
        with pytest.raises(kb.EmptyCompletionError):
            kb.complete_task(conn, tid, superseded_by=blank)
    assert conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (tid,)
    ).fetchone()["status"] == "running"


def test_superseded_keeps_an_explicit_summary(conn):
    tid = _running_task(conn)
    assert kb.complete_task(
        conn, tid, summary="verified on main: the test file is already fixed",
        superseded_by="https://github.com/o/r/pull/889",
    ) is True
    run = kb.list_runs(conn, tid)[0]
    assert run.outcome == "superseded"
    assert run.summary == "verified on main: the test file is already fixed"


def test_superseded_run_counts_as_a_success_downstream(conn):
    """Parent handoff context and the respawn guard must see a superseded run."""
    from hermes_cli import kanban_db_dispatch as kbd

    parent = _running_task(conn, title="parent already landed")
    assert kb.complete_task(conn, parent, superseded_by="#889") is True
    child = kb.create_task(conn, title="child", assignee="coder", parents=[parent])

    ctx = kb.build_worker_context(conn, child)
    assert parent in ctx
    assert "(no result recorded)" not in ctx

    # A card closed superseded must not be immediately respawned by the dispatcher.
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
    conn.commit()
    assert kbd.check_respawn_guard(conn, parent) is not None


def test_plain_completion_outcome_is_unchanged(conn):
    tid = _running_task(conn)
    assert kb.complete_task(conn, tid, summary="did the work") is True
    run = kb.list_runs(conn, tid)[0]
    assert run.outcome == "completed"
    assert "superseded_by" not in (run.metadata or {})


# ---------------------------------------------------------------------------
# Dispatcher: a REPRODUCED clean exit stops retrying instead of burning 3 slots
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _drive_clean_exit(conn, tid, fake_pid, worker_output):
    """One clean-exit (rc=0) reaper pass whose worker log says ``worker_output``."""
    import hermes_cli.kanban_db as _kb
    from hermes_cli import kanban_db_dispatch as _kbd

    host_prefix = _kb._claimer_id().split(":", 1)[0]
    assert _kb.claim_task(conn, tid, claimer=f"{host_prefix}:mock") is not None
    _kbd._set_worker_pid(conn, tid, fake_pid)
    _kbd._record_worker_exit(fake_pid, 0)  # W_EXITCODE(0, 0)
    original_alive, original_output = _kb._pid_alive, _kbd._worker_final_output
    _kb._pid_alive = lambda p: False
    _kbd._worker_final_output = lambda task_id, board=None: worker_output
    try:
        return _kbd.detect_crashed_workers(conn)
    finally:
        _kb._pid_alive = original_alive
        _kbd._worker_final_output = original_output


def test_reproduced_clean_exit_stops_after_two_runs(kanban_home):
    """The measured bug: 3 identical clean exits per card, booked as a crash.

    The second IDENTICAL clean exit is a reproduced no-op; a third spawn buys
    nothing, so the card is held for input after TWO runs, not three.
    """
    nothing_to_do = "Nothing to implement: the file is already fixed on main by #889."
    with kbc.connect() as conn:
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
    from hermes_cli import kanban_db_dispatch as kbd

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="flaky paperwork", assignee="coder")
        for i, pid in enumerate((993001, 993002)):
            _drive_clean_exit(conn, tid, pid, f"ran step {i}, forgot the handoff")
            assert kb.get_task(conn, tid).status == "ready"
        _drive_clean_exit(conn, tid, 993003, "third distinct ending")
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", "budget still bounded at the violation limit"
        payload = [e for e in kb.list_events(conn, tid) if e.kind == "gave_up"][0].payload or {}
        assert payload.get("stopped_early") is None
        assert payload.get("protocol_violations") == kbd._PROTOCOL_VIOLATION_FAILURE_LIMIT


def test_missing_worker_output_never_counts_as_identical(kanban_home):
    """Two runs with no recorded output say nothing about each other."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="no logs", assignee="coder")
        _drive_clean_exit(conn, tid, 994001, "")
        _drive_clean_exit(conn, tid, 994002, "")
        assert kb.get_task(conn, tid).status == "ready", (
            "absent output must not be read as a reproduced no-op"
        )


# ---------------------------------------------------------------------------
# Notification wording: superseded and reproduced-no-op are not "crashed"
# ---------------------------------------------------------------------------


def _names(task_id="T-123"):
    from types import SimpleNamespace

    return SimpleNamespace(
        task_id=task_id, head=f"[board] Kanban {task_id}", title="Ship it",
        board_tag="[board] ", task=None,
    )


def _event(**payload):
    from types import SimpleNamespace

    return SimpleNamespace(payload=payload)


def test_superseded_completion_ping_names_the_evidence():
    from gateway.kanban_watchers_notifier import _EVENT_FORMATTERS

    msg, _wake, _reason = _EVENT_FORMATTERS["completed"](
        _event(summary="already on main", superseded_by="t_0c5ac29a -> #889"), _names())
    assert "superseded" in msg.lower()
    assert "t_0c5ac29a -> #889" in msg
    assert "done —" not in msg, "a superseded close must not read like ordinary work"

    plain, *_ = _EVENT_FORMATTERS["completed"](_event(summary="did the work"), _names())
    assert "done —" in plain and "superseded" not in plain.lower()


def test_reproduced_no_op_ping_does_not_read_like_a_crash():
    from gateway.kanban_watchers_notifier import _EVENT_FORMATTERS

    msg, _wake, _reason = _EVENT_FORMATTERS["gave_up"](
        _event(failures=2, identical_violations=2, stopped_early="reproduced_clean_exit",
               error="worker exited cleanly (rc=0) ..."), _names())
    assert "nothing to do" in msg.lower()
    assert "no crash" in msg.lower()
    assert "failed 2 times" not in msg
    assert "--superseded-by" in msg
