"""Exit 126/127 is INFRASTRUCTURE, not a task failure.

Provenance: card t_263e7303, incident 2026-09-21 12:08→12:47 PT. The deploy-only
runtime tree went missing for ~38 minutes. ``~/.local/bin/hermes`` execs
``<runtime>/venv/bin/hermes``; with that path absent the shim exited 127, so EVERY
worker spawned in the window died before running a single line of task code.
The dispatcher classified those as ``nonzero_exit`` → ``crashed``, counted a
failure against each task, and flipped innocent CARDS (t_671fd52c, t_6cb36405,
t_25dbd0f7) to blocked. The cards recovered on their own once the venv returned,
proving the blocked state was pure noise.

These tests pin the class: an exit that happened BEFORE the worker started cannot
be attributed to the task.
"""
from __future__ import annotations

import json
from pathlib import Path
import time

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb.init_db()
    kb._recent_worker_exits.clear()
    with kb.connect_closing() as conn:
        yield conn
    kb._recent_worker_exits.clear()


def claim(conn, title="infra probe"):
    tid = kb.create_task(conn, title=title, assignee="worker")
    task = kb.claim_task(conn, tid)
    kb._set_worker_pid(conn, tid, 99999999)
    return task


def receipt(task, code):
    path = kb.kanban_db_path().parent / "runs" / f"{task.id}.{task.current_run_id}.exit.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"exit_code": code, "failure_reason": None, "ts": time.time()}))
    return path


# --------------------------------------------------------------- classifier
@pytest.mark.parametrize("code", [126, 127])
def test_classifier_names_the_infra_class(code):
    """Both shell "could not execute" codes map to infra, not a crash."""
    assert kb._classify_worker_exit.__module__  # sanity: symbol exists
    kb._record_worker_exit(4242, code << 8)
    try:
        assert kb._classify_worker_exit(4242) == ("infra_unavailable", code)
    finally:
        kb._recent_worker_exits.pop(4242, None)


@pytest.mark.parametrize("code", [1, 2, 3, 70, 125, 128])
def test_neighbouring_codes_are_still_real_failures(code):
    """The class must stay NARROW: only 126/127 are pre-start failures."""
    kb._record_worker_exit(4243, code << 8)
    try:
        kind, got = kb._classify_worker_exit(4243)
        assert (kind, got) == ("nonzero_exit", code)
    finally:
        kb._recent_worker_exits.pop(4243, None)


# --------------------------------------------------------------- reclaim path
@pytest.mark.parametrize("code", [126, 127])
def test_infra_exit_requeues_without_blaming_the_card(board, monkeypatch, code):
    """The load-bearing assertion: no failure counted, card not blocked."""
    task = claim(board)
    receipt(task, code)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    kb.detect_crashed_workers(board)

    current = kb.get_task(board, task.id)
    assert current.status == "ready", "an infra outage must not park the card"
    assert current.consecutive_failures == 0, "the task did not fail — it never ran"

    run = board.execute(
        "SELECT * FROM task_runs WHERE id=?", (task.current_run_id,)
    ).fetchone()
    assert run["outcome"] == "infra_unavailable"
    meta = json.loads(run["metadata"])
    assert meta["exit_code"] == code
    assert meta["exit_class"] == "infra_unavailable"

    assert task.id in kb.detect_crashed_workers._last_infra_unavailable
    assert task.id not in kb.detect_crashed_workers._last_rate_limited, (
        "an infra outage is not a quota wall; the board must not say it is"
    )
    assert task.id not in kb.detect_crashed_workers._last_auto_blocked


def test_infra_exit_emits_its_own_event_kind(board, monkeypatch):
    task = claim(board)
    receipt(task, 127)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    kb.detect_crashed_workers(board)
    kinds = [
        r["kind"] for r in board.execute(
            "SELECT kind FROM task_events WHERE task_id=?", (task.id,)
        ).fetchall()
    ]
    assert "infra_unavailable" in kinds
    assert "crashed" not in kinds, "a deploy-window outage is not a crash"


def test_infra_exit_defers_the_respawn(board, monkeypatch):
    """Requeue, but don't spin: re-spawning instantly just re-hits the outage."""
    task = claim(board)
    receipt(task, 127)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    kb.detect_crashed_workers(board)
    current = kb.get_task(board, task.id)
    assert current.next_eligible_at >= int(time.time()) + 295
    assert kb.check_respawn_guard(board, task.id) == "rate_limit_cooldown"


def test_repeated_infra_exits_never_trip_the_breaker(board, monkeypatch):
    """A 38-minute outage is many ticks. None of them may block the card."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    tid = kb.create_task(board, title="long outage", assignee="worker")
    for _ in range(8):
        board.execute(
            "UPDATE tasks SET status='ready', next_eligible_at=NULL WHERE id=?", (tid,)
        )
        task = kb.claim_task(board, tid)
        kb._set_worker_pid(board, tid, 99999999)
        receipt(task, 127)
        kb.detect_crashed_workers(board)
    current = kb.get_task(board, tid)
    assert current.consecutive_failures == 0
    assert current.status == "ready"


def test_infra_run_is_neutral_for_the_protocol_violation_streak(board, monkeypatch):
    """An un-runnable harness must neither consume nor extend the violation budget."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    tid = kb.create_task(board, title="streak probe", assignee="worker")

    # one real protocol violation (clean exit, still running)
    task = kb.claim_task(board, tid)
    kb._set_worker_pid(board, tid, 99999999)
    receipt(task, 0)
    kb.detect_crashed_workers(board)
    assert kb._protocol_violation_streak(board, tid) == 1

    # an infra exit on top of it is NEUTRAL — the streak is unchanged
    board.execute(
        "UPDATE tasks SET status='ready', next_eligible_at=NULL WHERE id=?", (tid,)
    )
    task = kb.claim_task(board, tid)
    kb._set_worker_pid(board, tid, 99999999)
    receipt(task, 127)
    kb.detect_crashed_workers(board)
    assert kb._protocol_violation_streak(board, tid) == 1


def test_status_fallback_classifies_infra_without_a_receipt(board, monkeypatch):
    """Legacy path: no exit receipt, only the reaped wait status."""
    task = claim(board)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    kb._record_worker_exit(99999999, 127 << 8)
    kb.detect_crashed_workers(board)
    assert kb.get_task(board, task.id).consecutive_failures == 0
    assert task.id in kb.detect_crashed_workers._last_infra_unavailable


# --------------------------------------------------------------- mutation guard
def test_mutation_removing_the_class_reblames_the_card(board, monkeypatch):
    """Prove the gate is LOAD-BEARING: with 127 out of the infra set, the card
    goes back to being blamed (failure counted, crashed outcome). If this test
    ever passes with the class removed, the class is not doing anything."""
    task = claim(board)
    receipt(task, 127)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kb, "KANBAN_INFRA_EXIT_CODES", frozenset())

    kb.detect_crashed_workers(board)

    current = kb.get_task(board, task.id)
    assert current.consecutive_failures == 1, (
        "mutant did not change behaviour — the infra class is inert"
    )
    run = board.execute(
        "SELECT * FROM task_runs WHERE id=?", (task.current_run_id,)
    ).fetchone()
    assert run["outcome"] == "crashed"
