"""``superseded`` as a first-class terminal outcome for an already-satisfied premise.

A worker that verifies its card's work already landed has no honest verb today:
``complete_task`` demands evidence of work it did not do and ``block_task`` demands a
blocker that does not exist, so it exits rc=0 and the dispatcher books a protocol
violation + retries. ``superseded_by`` closes the card ``done`` with
``outcome='superseded'`` and a mandatory evidence pointer.
"""

from __future__ import annotations

import json
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


def _drive_clean_exit(
    conn, tid, fake_pid, worker_output, *, model_turn_completed=True,
):
    """One clean-exit (rc=0) reaper pass driving the REAL append-mode producer.

    Deliberately does NOT monkeypatch ``_worker_log_stderr_tail`` or the
    segmenter: it stamps the run boundary exactly as ``_default_spawn`` does,
    appends ``worker_output`` to the real per-task log the same way the child's
    inherited fd would, publishes the real run-scoped exit-receipt shape, and
    lets ``detect_crashed_workers`` read both back. The previous mocked version
    made per-run output identical/distinct BY CONSTRUCTION, which is precisely
    how the append-mode defects survived.
    """
    import hermes_cli.kanban_db as _kb
    from hermes_cli.kanban_worker_exit import exit_file

    host_prefix = _kb._claimer_id().split(":", 1)[0]
    assert _kb.claim_task(conn, tid, claimer=f"{host_prefix}:mock") is not None
    task = _kb.get_task(conn, tid)
    assert task is not None and task.current_run_id is not None
    _kb._set_worker_pid(conn, tid, fake_pid)

    log_dir = _kb.worker_logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{tid}.log"
    _kb._stamp_worker_log_run_boundary(log_path)
    if worker_output:
        with open(log_path, "ab") as fh:
            fh.write(worker_output.encode("utf-8") + b"\n")

    db_path = Path(
        next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
    )
    receipt = exit_file(db_path, tid, task.current_run_id)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(
        json.dumps({
            "exit_code": 0,
            "failure_reason": None,
            "exit_class": None,
            "model_turn_completed": model_turn_completed,
        }),
        encoding="utf-8",
    )
    _kb._record_worker_exit(fake_pid, 0)  # receipt outranks this PID fallback
    original_alive = _kb._pid_alive
    _kb._pid_alive = lambda p: False
    try:
        return _kb.detect_crashed_workers(conn)
    finally:
        _kb._pid_alive = original_alive


# Realistic per-run shape, measured from the incident logs
# (~/.hermes/kanban/logs/t_e484ec2d.log): a CONSTANT startup banner, then a
# session_id that is unique to every run, then the worker's body. Runs on that
# board measured 1936-3343 B each, which is exactly the size range the previous
# tail-window fingerprint could not see.
_BANNER = (
    "Warning: Unknown toolsets: rl\n"
    "⚠️  Normalized model 'claude-apr/claude-opus-5' to 'claude-opus-5' for \n"
    "claude-apr.\n\n"
)


def _run_output(body: str, session_id: str, pad_to: int = 2600) -> str:
    """A realistically-shaped, realistically-sized run of worker output."""
    head = f"{_BANNER}session_id: {session_id}\n"
    filler_line = "thinking about the card and reading the repo. " * 2 + "\n"
    text = head + body + "\n"
    while len(text.encode("utf-8")) < pad_to:
        text += filler_line
    return text + body + "\n"


def test_reproduced_clean_exit_stops_after_two_runs(kanban_home):
    """The measured bug: 3 identical clean exits per card, booked as a crash.

    The second IDENTICAL clean exit is a reproduced no-op; a third spawn buys
    nothing, so the card is held for input after TWO runs, not three.

    Driven through the real append-mode log at a realistic ~2.6 KB per run —
    the size band where a raw tail-window fingerprint provably cannot fire.
    """
    nothing_to_do = "Nothing to implement: the file is already fixed on main by #889."
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="already landed", assignee="coder")

        _drive_clean_exit(conn, tid, 992001, _run_output(nothing_to_do, "20260922_151224_163932"))
        assert kb.get_task(conn, tid).status == "ready", "first clean exit still retries"

        # Same work, DIFFERENT session_id — the per-run-varying preamble must
        # not defeat the match.
        _drive_clean_exit(conn, tid, 992002, _run_output(nothing_to_do, "20260922_154017_6cbd0d"))
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
    """Only a REPRODUCED no-op trips early; genuinely different exits still retry.

    The regression arm: with a head-anchored window into the appended log, the
    constant startup banner made these three DIFFERENT runs compare equal and
    the card was stopped a retry early with a "nothing to do" message about a
    worker that did three different things.
    """
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="flaky paperwork", assignee="coder")
        for i, pid in enumerate((993001, 993002)):
            _drive_clean_exit(
                conn, tid, pid,
                _run_output(f"ran step {i}, forgot the handoff", f"2026092{i}_000000_aaaaa{i}"),
            )
            assert kb.get_task(conn, tid).status == "ready", (
                f"run {i + 1} did different work — it must not read as a reproduced no-op"
            )
        _drive_clean_exit(
            conn, tid, 993003,
            _run_output("third distinct ending", "20260923_000000_ccccc3"),
        )
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", "budget still bounded at the violation limit"
        payload = [e for e in kb.list_events(conn, tid) if e.kind == "gave_up"][0].payload or {}
        assert payload.get("stopped_early") is None
        assert payload.get("protocol_violations") == kb._PROTOCOL_VIOLATION_FAILURE_LIMIT


@pytest.mark.parametrize(
    "infra_abort",
    [
        (
            "⚠️  Primary auth failed — switching to fallback: claude-apr / "
            "claude-opus-5\nAPI call failed after 3 retries: HTTP 429 Too Many Requests"
        ),
        "API call failed after 3 retries: HTTP 503 Service Unavailable",
        'API call failed after 3 retries: HTTP 503 {"error":"no eligible sub"}',
        'API call failed after 3 retries: HTTP 503 {"error":"pool at capacity"}',
    ],
    ids=["auth-fallback-429", "upstream-503", "no-eligible-sub", "pool-at-capacity"],
)
def test_pre_model_infra_abort_never_trips_reproduced_clean_exit(
    kanban_home, infra_abort,
):
    """Identical pre-model failures are not evidence of reproduced worker work.

    The discriminator is positive and class-wide: the run-scoped receipt says a
    model turn completed. Error wording is deliberately not parsed here, so 429,
    503, relay-pool exhaustion, and future pre-model aborts all remain ineligible.
    The append-mode producer and segment reader are real; only the process itself
    is represented by the same exit receipt it atomically publishes in production.
    """
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="provider unavailable", assignee="coder")
        for i in range(2):
            _drive_clean_exit(
                conn,
                tid,
                997100 + i,
                _run_output(infra_abort, f"20260922_00000{i}_infra{i}"),
                model_turn_completed=False,
            )
            task = kb.get_task(conn, tid)
            assert task is not None and task.status == "ready"

        gave_up = [e for e in kb.list_events(conn, tid) if e.kind == "gave_up"]
        assert gave_up == []
        runs = kb.list_runs(conn, tid)
        assert len(runs) == 2
        assert all(
            not (run.metadata or {}).get("run_output_fingerprint") for run in runs
        )


@pytest.mark.parametrize("per_run_bytes", [1024, 2048, 2600, 3343, 4096, 8192])
def test_reproduced_trip_is_independent_of_run_size(kanban_home, per_run_bytes):
    """No dead band: the trip must fire at EVERY realistic per-run output size.

    The predecessor fingerprint fired 4/4 below 2048 B, 0/8 between 2048 and
    4096 B, and 3/3 at or above 4096 B, because it was a fixed 4096-byte window
    into a growing append-mode file. Real runs measured 1936-3343 B — inside the
    dead band — so the headline "stop at 2" never actually held in production.
    """
    body = "Nothing to implement: already satisfied on main."
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title=f"sized {per_run_bytes}", assignee="coder")
        _drive_clean_exit(
            conn, tid, 995000 + per_run_bytes,
            _run_output(body, "20260922_000001_aaaaaa", pad_to=per_run_bytes),
        )
        assert kb.get_task(conn, tid).status == "ready"
        _drive_clean_exit(
            conn, tid, 996000 + per_run_bytes,
            _run_output(body, "20260922_000002_bbbbbb", pad_to=per_run_bytes),
        )
        assert kb.get_task(conn, tid).status == "blocked", (
            f"identical clean exits at {per_run_bytes} B/run must trip at 2"
        )
        assert len(kb.list_runs(conn, tid)) == 2


def test_missing_worker_output_never_counts_as_identical(kanban_home):
    """Two runs with no recorded output say nothing about each other."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="no logs", assignee="coder")
        _drive_clean_exit(conn, tid, 994001, "")
        _drive_clean_exit(conn, tid, 994002, "")
        assert kb.get_task(conn, tid).status == "ready", (
            "absent output must not be read as a reproduced no-op"
        )


def test_unsegmentable_run_never_counts_as_identical(kanban_home):
    """A log with no run boundary (pre-upgrade, or rotated away) yields no match.

    Guards the migration edge: violation runs recorded before the boundary
    existed carry no per-run fingerprint, so they can never trip the early stop
    on the strength of a window that spans several of them.
    """
    import hermes_cli.kanban_db as _kb

    body = _run_output("identical text with no boundary", "20260922_000001_aaaaaa")
    original = _kb._stamp_worker_log_run_boundary
    _kb._stamp_worker_log_run_boundary = lambda log_path: None
    try:
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="legacy log", assignee="coder")
            _drive_clean_exit(conn, tid, 997001, body)
            _drive_clean_exit(conn, tid, 997002, body)
            assert kb.get_task(conn, tid).status == "ready", (
                "an unsegmentable run must not be fingerprinted"
            )
    finally:
        _kb._stamp_worker_log_run_boundary = original


def test_run_segment_isolates_this_run_from_the_appended_history(kanban_home):
    """Unit-level proof of the producer: the segment is ONE run, not the file."""
    import hermes_cli.kanban_db as _kb

    log_dir = _kb.worker_logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "t_segment.log"
    for i in range(3):
        _kb._stamp_worker_log_run_boundary(log_path)
        with open(log_path, "ab") as fh:
            fh.write(f"run {i} output\n".encode("utf-8"))

    segment = _kb._worker_log_run_segment("t_segment")
    assert segment == "run 2 output"
    assert "run 0" not in segment and "run 1" not in segment
    # Every boundary is unique, so a stale one can never be re-matched.
    raw = log_path.read_text()
    boundaries = [ln for ln in raw.splitlines() if _kb._RUN_BOUNDARY_PREFIX in ln]
    assert len(boundaries) == 3 and len(set(boundaries)) == 3


def test_run_fingerprint_ignores_per_run_session_id(kanban_home):
    """Same work + different session_id must fingerprint the same; different work must not."""
    import hermes_cli.kanban_db as _kb

    a = _kb._run_output_fingerprint(_run_output("did the same thing", "20260922_111111_aaaaaa"))
    b = _kb._run_output_fingerprint(_run_output("did the same thing", "20260922_222222_bbbbbb"))
    c = _kb._run_output_fingerprint(_run_output("did something else", "20260922_222222_bbbbbb"))
    assert a and a == b
    assert a != c
    assert _kb._run_output_fingerprint(None) == ""
    assert _kb._run_output_fingerprint("") == ""


def test_stderr_tail_alone_never_trips_the_early_stop(kanban_home):
    """The append-window field must not be a fingerprint fallback.

    ``stderr_tail`` spans every run of the task. Reading it as a per-run
    identity is what produced BOTH defects, so a violation run carrying only
    ``stderr_tail`` must contribute no match at all.
    """
    shared_window = "banner\nbanner\nrun A did one thing\nrun B did another\n"
    assert kb._violation_output_fingerprint({"stderr_tail": shared_window}) == ""
    assert kb._violation_output_fingerprint({"worker_output": shared_window}) == ""
    assert kb._violation_output_fingerprint({"run_output_fingerprint": "abc"}) == "abc"


def test_goal_run_status_maps_superseded_to_done(kanban_home):
    """A superseded close is a completion, not an ownership loss.

    Without the mapping the literal 'superseded' falls through and collides
    with goal_run_status's OWN use of that string for "a successor claimed the
    task", so goals.py files a real completion under the catch-all "stopped".
    """
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.complete_task(conn, tid, superseded_by="#889") is True
        assert kb.goal_run_status(conn, tid, run_id) == "done"
