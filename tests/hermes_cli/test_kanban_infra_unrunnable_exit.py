"""Exit 127 from a kanban worker is INFRA, not a task failure.

Measured 2026-09-21 on the Mac Studio: ``~/.hermes/runtime/hermes-agent`` was
absent for ~38 minutes. The ``hermes`` on PATH is a shim that execs that tree's
venv CLI, so every worker spawned in the window died at exit 127 before its
agent loop started. The dispatcher recorded those runs as ``crashed``, counted
them against ``consecutive_failures``, and tripped the breaker — flipping
healthy cards (t_671fd52c, t_6cb36405, t_25dbd0f7) to ``blocked`` for an
outage that had nothing to do with them. They self-recovered to ``review`` once
the tree came back, proving the blocked state was pure noise.

These tests pin the corrected behaviour: 127 classifies as ``infra_unrunnable``
and is requeued WITHOUT counting a failure, exactly like the rate-limit
sentinel.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

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
    path.write_text(json.dumps({"exit_code": code, "ts": time.time()}))
    return path


def test_classifier_maps_127_to_infra_unrunnable():
    """The pid-registry classifier names 127 distinctly, not as nonzero_exit."""
    kb._recent_worker_exits.clear()
    kb._record_worker_exit(4242, 127 << 8)
    assert kb._classify_worker_exit(4242) == ("infra_unrunnable", 127)
    # Neighbouring codes must NOT be swept in — only the exec-failure code.
    kb._record_worker_exit(4243, 126 << 8)
    assert kb._classify_worker_exit(4243) == ("nonzero_exit", 126)
    kb._recent_worker_exits.clear()


@pytest.mark.parametrize(
    "code,outcome,failures",
    [(127, "infra_unrunnable", 0), (1, "crashed", 1)],
)
def test_127_requeues_without_counting_a_failure(board, monkeypatch, code, outcome, failures):
    """AC-3: exit 127 releases the card and does NOT touch consecutive_failures."""
    task = claim(board)
    receipt(task, code)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)

    kb.detect_crashed_workers(board)

    current = kb.get_task(board, task.id)
    assert current.status == "ready", "the card must go back to its source phase"
    assert current.consecutive_failures == failures
    run = board.execute(
        "SELECT * FROM task_runs WHERE id=?", (task.current_run_id,)
    ).fetchone()
    assert run["outcome"] == outcome
    assert json.loads(run["metadata"])["exit_code"] == code

    if code == 127:
        # Deferred by the respawn guard on its own cooldown reason, so the
        # board re-probes cheaply instead of burning a slot every tick.
        assert current.next_eligible_at >= int(time.time()) + 295
        assert kb.check_respawn_guard(board, task.id) == "infra_cooldown"
        assert task.id in kb.detect_crashed_workers._last_infra_unrunnable
        assert task.id not in kb.detect_crashed_workers._last_rate_limited


def test_repeated_127_never_trips_the_breaker(board, monkeypatch):
    """The regression itself: a whole deploy window must not block the card.

    DEFAULT_FAILURE_LIMIT is 2, and the live incident produced three 127 runs
    per card in ~3 minutes. Drive more attempts than the limit and assert the
    card is still dispatchable.
    """
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    # No cooldown, so each attempt is immediately re-claimable — this models
    # the dispatcher retrying straight through the outage window. Patch the
    # resolver directly: the config default would otherwise win over the env.
    monkeypatch.setattr(kb, "_resolve_rate_limit_cooldown_seconds", lambda: 0)

    task = claim(board, title="argus run during deploy window")
    tid = task.id
    for attempt in range(kb.DEFAULT_FAILURE_LIMIT + 2):
        if attempt:
            assert kb.get_task(board, tid).status == "ready"
            task = kb.claim_task(board, tid)
            kb._set_worker_pid(board, tid, 99999999)
        receipt(task, 127)
        kb.detect_crashed_workers(board)

    final = kb.get_task(board, tid)
    assert final.status == "ready", (
        f"card was {final.status} — an infra outage blamed the task"
    )
    assert final.consecutive_failures == 0


def test_plain_crash_still_trips_the_breaker(board, monkeypatch):
    """Mutation guard: the infra class must not soften REAL failures."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    task = claim(board, title="genuinely failing task")
    tid = task.id
    for attempt in range(kb.DEFAULT_FAILURE_LIMIT):
        if attempt:
            if kb.get_task(board, tid).status != "ready":
                break
            task = kb.claim_task(board, tid)
            kb._set_worker_pid(board, tid, 99999999)
        receipt(task, 1)
        kb.detect_crashed_workers(board)

    final = kb.get_task(board, tid)
    assert final.status == "blocked", "a real crash streak must still block"
    assert final.consecutive_failures >= kb.DEFAULT_FAILURE_LIMIT


def test_infra_run_is_neutral_for_the_protocol_violation_streak(board, monkeypatch):
    """An infra run must neither consume nor break the violation budget."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kb, "_resolve_rate_limit_cooldown_seconds", lambda: 0)

    task = claim(board, title="violation then outage")
    tid = task.id
    receipt(task, 0)                     # clean exit, still running = violation
    kb.detect_crashed_workers(board)

    task = kb.claim_task(board, tid)
    kb._set_worker_pid(board, tid, 99999999)
    receipt(task, 127)                   # infra outage on top
    kb.detect_crashed_workers(board)

    # The violation streak walks newest-first; the infra run must be SKIPPED
    # (neutral), leaving the earlier violation still counted.
    assert kb._protocol_violation_streak(board, tid) == 1
