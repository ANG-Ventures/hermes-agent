"""A burst of externally-ended, still-heartbeating workers is ONE event.

Provenance: card t_0c1ebbae, 2026-09-23 15:58 PT. An outside reaper SIGTERM'd 17
kanban workers in one second. Each was accounted separately: 6 as "pid not
alive" crashes (the same-fingerprint systemic rule then blocked cards on their
FIRST failure) and 11 as clean-exit "protocol violations" (the kanban SIGTERM
path os._exit(0)'d without a receipt, and Popen reported rc=0).

Pinned here:
  * >= 3 externally-ended workers with fresh heartbeats in one tick -> cohort
    death: requeued, no failure counted, one page;
  * below the threshold, or with stale heartbeats, the normal crash path holds;
  * a worker's signal receipt (128+signum, exit_class "signaled") is classified
    as ``signaled``, never as a clean exit / protocol violation.
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


@pytest.fixture
def pages(monkeypatch):
    sent = []
    monkeypatch.setattr(kb, "_page_cohort_death", lambda conn, ids: sent.append(list(ids)))
    return sent


def spawn(conn, n, *, heartbeat_age=5):
    tasks = []
    now = int(time.time())
    for i in range(n):
        tid = kb.create_task(conn, title=f"cohort probe {i}", assignee="worker")
        task = kb.claim_task(conn, tid)
        kb._set_worker_pid(conn, tid, 99999900 + i)
        conn.execute("UPDATE tasks SET last_heartbeat_at=? WHERE id=?",
                     (now - heartbeat_age, tid))
        tasks.append(task)
    return tasks


def receipt(task, code, exit_class=None):
    path = kb.kanban_db_path().parent / "runs" / f"{task.id}.{task.current_run_id}.exit.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"exit_code": code, "failure_reason": None,
                                "exit_class": exit_class, "ts": time.time()}))


def outcomes(conn, tasks):
    return [
        conn.execute("SELECT outcome FROM task_runs WHERE id=?",
                     (t.current_run_id,)).fetchone()["outcome"]
        for t in tasks
    ]


def test_cohort_is_requeued_without_blaming_any_card(board, monkeypatch, pages):
    tasks = spawn(board, 4)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)

    crashed = kb.detect_crashed_workers(board)

    assert crashed == [], "a cohort death is not N crashes"
    for t in tasks:
        cur = kb.get_task(board, t.id)
        assert cur.status == "ready"
        assert cur.consecutive_failures == 0
    assert outcomes(board, tasks) == ["cohort_death"] * 4
    assert sorted(kb.detect_crashed_workers._last_cohort_deaths) == sorted(t.id for t in tasks)
    assert kb.detect_crashed_workers._last_auto_blocked == []
    gave_up = board.execute(
        "SELECT count(*) FROM task_events WHERE kind='gave_up'").fetchone()[0]
    assert gave_up == 0, "the systemic-fingerprint rule must not fire on a cohort"
    assert len(pages) == 1, "exactly one page per cohort"
    assert sorted(pages[0]) == sorted(t.id for t in tasks)


def test_signal_receipts_join_the_cohort(board, monkeypatch, pages):
    """The 15:58 shape: dispatcher children carried SIGTERM receipts, orphans none."""
    tasks = spawn(board, 3)
    receipt(tasks[0], 128 + 15, "signaled")
    receipt(tasks[1], 128 + 15, "signaled")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    kb.detect_crashed_workers(board)
    assert outcomes(board, tasks) == ["cohort_death"] * 3
    assert len(pages) == 1


def test_below_threshold_is_still_a_crash(board, monkeypatch, pages):
    tasks = spawn(board, 2)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    crashed = kb.detect_crashed_workers(board)
    assert sorted(crashed) == sorted(t.id for t in tasks)
    assert outcomes(board, tasks) == ["crashed"] * 2
    assert pages == []


def test_stale_heartbeats_are_not_a_cohort(board, monkeypatch, pages):
    """Workers that had already gone silent died of something else."""
    tasks = spawn(board, 3, heartbeat_age=kb._COHORT_DEATH_HEARTBEAT_WINDOW_SECONDS + 60)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    kb.detect_crashed_workers(board)
    assert outcomes(board, tasks) == ["crashed"] * 3
    assert pages == []


def test_clean_exits_are_not_a_cohort(board, monkeypatch, pages):
    """rc=0 receipts are the worker's own doing (protocol violation), not a kill."""
    tasks = spawn(board, 3)
    for t in tasks:
        receipt(t, 0)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    kb.detect_crashed_workers(board)
    assert "cohort_death" not in outcomes(board, tasks)
    assert pages == []


def test_signal_receipt_is_classified_signaled_not_clean(board):
    [task] = spawn(board, 1)
    receipt(task, 128 + 15, "signaled")
    assert kb._classify_run_exit(board, task.id, task.current_run_id, 99999900) == (
        "signaled", 15)


def test_single_signaled_worker_is_a_crash_not_a_protocol_violation(board, monkeypatch, pages):
    [task] = spawn(board, 1)
    receipt(task, 128 + 15, "signaled")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    kb.detect_crashed_workers(board)
    run = board.execute("SELECT outcome, error FROM task_runs WHERE id=?",
                        (task.current_run_id,)).fetchone()
    assert run["outcome"] == "crashed"
    assert "killed by signal 15" in run["error"]
    assert kb._protocol_violation_streak(board, task.id) == 0


def test_mutation_disabling_the_guard_reblames_the_cards(board, monkeypatch, pages):
    """Load-bearing proof: without the guard the cohort is N crashes and the
    systemic rule blocks them on the first failure (the 15:58 damage)."""
    tasks = spawn(board, 4)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kb, "_COHORT_DEATH_MIN", 10**9)
    kb.detect_crashed_workers(board)
    assert outcomes(board, tasks) == ["crashed"] * 4
    assert all(kb.get_task(board, t.id).consecutive_failures >= 1 for t in tasks)
    assert pages == []


def test_worker_sigterm_path_leaves_a_signal_receipt_before_exiting():
    """Source contract on cli.py's kanban SIGTERM branch: the receipt and the
    last-words line are written BEFORE os._exit — os._exit skips atexit, so
    anything after it (or only in atexit) never happens."""
    src = (Path(__file__).resolve().parents[2] / "cli.py").read_text(encoding="utf-8")
    start = src.index("def _signal_handler_q(")
    end = src.index("raise KeyboardInterrupt()", start)
    handler = src[start:end]
    receipt_at = handler.index('write_exit_status(128 + int(signum), exit_class="signaled")')
    words_at = handler.index("[kanban-worker] pid")
    exit_at = handler.rindex("os._exit(0)")
    assert receipt_at < exit_at and words_at < exit_at
