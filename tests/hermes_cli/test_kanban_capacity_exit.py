"""Upstream provider CAPACITY overload must not burn a kanban retry (#655 follow-on).

Pool exhaustion (our relay has no eligible sub) and upstream capacity (the
vendor is full) are different causes with the same remedy. Both route to the
retry-preserving EX_TEMPFAIL exit, through SEPARATE named predicates, so the
receipt / event telemetry can still tell them apart. The widening must stop
short of app-level 500s, assertions, and OOMs — those stay real crashes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_worker_exit import (
    EXIT_CLASS_POOL_EXHAUSTED,
    EXIT_CLASS_QUOTA,
    EXIT_CLASS_UPSTREAM_CAPACITY,
    WorkerExit,
    is_pool_exhausted_exit,
    is_upstream_capacity_exit,
    report_exit,
    worker_exit_class,
)

# Verbatim crash text from chiron t_1811c4d0 run 87 (2026-09-09 06:52).
INCIDENT_TEXT = (
    "API call failed after 3 retries: Our servers are currently overloaded. "
    "Please try again later."
)


@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    path = tmp_path / "run.exit.json"
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_probe")
    monkeypatch.setenv("HERMES_KANBAN_OWNER_PID", str(os.getpid()))
    monkeypatch.setenv("HERMES_KANBAN_EXIT_FILE", str(path))
    return path


# ---------------------------------------------------------------------------
# Direction 1: capacity overload -> quota-style exit, retries preserved.
# ---------------------------------------------------------------------------

CAPACITY_ERRORS = [
    INCIDENT_TEXT,
    "HTTP 529: Overloaded",
    "Error code: 529 - {'type': 'overloaded_error'}",
    "HTTP 503 service unavailable",
    "Error code: 503 - Service Temporarily Unavailable",
    "The service may be temporarily overloaded, please try again later",
    "model is at capacity",
]


@pytest.mark.parametrize("error", CAPACITY_ERRORS)
def test_upstream_capacity_is_a_separate_predicate(error):
    assert is_upstream_capacity_exit("overloaded", error)
    assert not is_pool_exhausted_exit("overloaded", error)
    assert worker_exit_class("overloaded", error) == EXIT_CLASS_UPSTREAM_CAPACITY


def test_pool_exhaustion_is_not_upstream_capacity():
    err = 'HTTP 503 {"error":"no eligible sub"}'
    assert is_pool_exhausted_exit("overloaded", err)
    assert not is_upstream_capacity_exit("overloaded", err)
    assert worker_exit_class("overloaded", err) == EXIT_CLASS_POOL_EXHAUSTED
    assert worker_exit_class("pool_exhausted", "") == EXIT_CLASS_POOL_EXHAUSTED
    assert worker_exit_class("rate_limit", "HTTP 429") == EXIT_CLASS_QUOTA
    assert worker_exit_class("billing", "insufficient credits") == EXIT_CLASS_QUOTA


@pytest.mark.parametrize("error", CAPACITY_ERRORS)
def test_capacity_overload_exits_tempfail_with_class_in_receipt(worker_env, error):
    exc = WorkerExit({"failed": True, "failure_reason": "overloaded", "error": error})
    assert exc.code == kb.KANBAN_RATE_LIMIT_EXIT_CODE
    assert exc.exit_class == EXIT_CLASS_UPSTREAM_CAPACITY
    report_exit(exc)
    payload = json.loads(worker_env.read_text(encoding="utf-8"))
    assert payload["exit_code"] == 75
    assert payload["failure_reason"] == "overloaded"
    assert payload["exit_class"] == EXIT_CLASS_UPSTREAM_CAPACITY
    assert "error" not in payload


# ---------------------------------------------------------------------------
# Direction 2: real crashes stay real crashes.
# ---------------------------------------------------------------------------

REAL_CRASHES = [
    ("unknown", "ZeroDivisionError: division by zero"),
    ("unknown", "AssertionError: expected 3 == 4"),
    ("unknown", "MemoryError"),
    ("unknown", "Killed: 9 (out of memory)"),
    ("server_error", "HTTP 500: Internal Server Error"),
    ("server_error", "HTTP 502: Bad Gateway"),
    ("tool_error", "tool execution failed"),
    # A 500 whose body happens to say "try again later" is still an app 500:
    # the predicate keys on the CLASSIFIED reason, not the phrase alone.
    ("server_error", "HTTP 500: something broke, try again later"),
    # An overloaded verdict with an unrecognised body stays a crash — the
    # predicate widens on named patterns only.
    ("overloaded", "weird proxy body with no known signal"),
    (None, INCIDENT_TEXT),
]


@pytest.mark.parametrize("reason,error", REAL_CRASHES)
def test_real_failures_are_not_capacity(reason, error):
    assert not is_upstream_capacity_exit(reason, error)
    assert worker_exit_class(reason, error) is None


@pytest.mark.parametrize("reason,error", REAL_CRASHES)
def test_real_failures_still_exit_one(worker_env, reason, error):
    exc = WorkerExit({"failed": True, "failure_reason": reason, "error": error})
    assert exc.code == 1
    assert exc.exit_class is None
    report_exit(exc)
    payload = json.loads(worker_env.read_text(encoding="utf-8"))
    assert payload["exit_code"] == 1
    assert payload["exit_class"] is None


def test_capacity_exit_needs_task_grant_and_authority(worker_env, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    exc = WorkerExit({"failed": True, "failure_reason": "overloaded", "error": INCIDENT_TEXT})
    assert exc.code == 1
    assert exc.exit_class is None


# ---------------------------------------------------------------------------
# Dispatcher side: park + cooldown, no failure counted, class in telemetry.
# ---------------------------------------------------------------------------

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


def _claim_with_receipt(conn, payload):
    tid = kb.create_task(conn, title="capacity probe", assignee="worker")
    task = kb.claim_task(conn, tid)
    kb._set_worker_pid(conn, tid, 99999999)
    path = kb.kanban_db_path().parent / "runs" / f"{task.id}.{task.current_run_id}.exit.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return task


@pytest.mark.parametrize("exit_class,label", [
    (EXIT_CLASS_UPSTREAM_CAPACITY, "provider capacity overload"),
    (EXIT_CLASS_POOL_EXHAUSTED, "sub pool capped"),
    (EXIT_CLASS_QUOTA, "quota wall"),
    (None, "quota wall"),  # legacy #655 receipt without a class
])
def test_capacity_receipt_parks_with_cooldown_without_failure(board, monkeypatch, exit_class, label):
    task = _claim_with_receipt(board, {
        "exit_code": 75, "failure_reason": "overloaded",
        "exit_class": exit_class, "ts": time.time(),
    })
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    kb.detect_crashed_workers(board)
    current = kb.get_task(board, task.id)
    assert current.status == "ready"
    assert current.consecutive_failures == 0
    assert current.next_eligible_at >= int(time.time()) + 295
    assert kb.check_respawn_guard(board, task.id) == "rate_limit_cooldown"
    assert label in current.last_failure_error
    run = board.execute("SELECT * FROM task_runs WHERE id=?", (task.current_run_id,)).fetchone()
    assert run["outcome"] == "rate_limited"
    meta = json.loads(run["metadata"])
    assert meta.get("exit_class") == exit_class
    assert task.id in kb.detect_crashed_workers._last_rate_limited


def test_exit_one_receipt_with_capacity_class_is_still_a_crash(board, monkeypatch):
    # The CODE decides the kind; a class label cannot promote a real crash.
    task = _claim_with_receipt(board, {
        "exit_code": 1, "failure_reason": "server_error",
        "exit_class": EXIT_CLASS_UPSTREAM_CAPACITY, "ts": time.time(),
    })
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    kb.detect_crashed_workers(board)
    current = kb.get_task(board, task.id)
    assert current.consecutive_failures == 1
    run = board.execute("SELECT * FROM task_runs WHERE id=?", (task.current_run_id,)).fetchone()
    assert run["outcome"] == "crashed"
