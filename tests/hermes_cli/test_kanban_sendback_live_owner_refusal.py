"""request_changes' live-owner refusal on a parked ``review`` card (t_2911d7bc).

t_c9bcf95a: a human send-back was refused twice with "a prior worker run on
this task is still alive" and the operator could not tell which pid/run the
gate meant, nor whether it believed the pid was the recorded worker. These
tests drive the REAL identity probe (no ``_pid_started_in_claim`` stub) with
real processes:

* a recycled pid (live, but started long after the recorded spawn) never
  blocks the send-back;
* a genuine live prior worker still blocks it, and the refusal names the
  run id, the pid and the identity verdict the gate acted on.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_review_schema import REQUIRED_REVIEW_LENSES

COVERAGE = json.dumps({
    "lenses": {name: "done" for name in REQUIRED_REVIEW_LENSES},
    "findings": 1,
    "items": ["F1 boundary test missing"],
    "review_minutes": 1,
    "batch_id": "t_2911d7bc",
    "head_sha": "0123456789abcdef0123456789abcdef01234567",
})


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_PROFILE", "apollo")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    from hermes_cli import kanban_identity as _ki
    monkeypatch.setattr(_ki, "_runs_under_operator_gateway", lambda name: True)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    # The real identity probe, never the conftest stub.
    monkeypatch.setattr(kb, "_pid_started_in_claim", kb._real_pid_started_in_claim)
    with kb.connect() as c:
        yield c


def _parked_review_with_spawn(conn, pid: int) -> tuple[str, int]:
    """builder's run spawned ``pid``, then handed off to a human reviewer."""
    tid = kb.create_task(conn, title="parked review", assignee="builder")
    impl = kb.claim_task(conn, tid, claimer=f"{kb._claimer_id().split(':', 1)[0]}:1")
    assert impl is not None
    kbd._set_worker_pid(conn, tid, pid)
    assert kb.request_review(
        conn, tid, summary="ready", reviewer="human",
        expected_run_id=impl.current_run_id,
    ) is True
    assert kb.get_task(conn, tid).status == "review"
    return tid, int(impl.current_run_id)


def test_recycled_pid_does_not_block_send_back(conn):
    # The run recorded THIS pid, but a process that started 10 min earlier
    # than the one now holding it: the live occupant is a stranger.
    tid, _run = _parked_review_with_spawn(conn, os.getpid())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_events SET created_at = created_at - 600, payload = "
            "json_set(payload, '$.start_token', json_extract(payload, '$.start_token') - 600) "
            "WHERE task_id = ? AND kind IN ('claimed', 'spawned')", (tid,))
    assert kb._prior_worker_still_alive(conn, tid) is None
    ok, detail = kb.request_changes(
        conn, tid, reason="fix", claimer="apollo", coverage=COVERAGE)
    assert ok, detail


def test_live_prior_worker_refusal_names_run_pid_and_identity(conn):
    proc = subprocess.Popen(["/bin/sleep", "30"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.05)
        tid, run_id = _parked_review_with_spawn(conn, proc.pid)
        ok, detail = kb.request_changes(
            conn, tid, reason="fix", claimer="apollo", coverage=COVERAGE)
        assert not ok
        assert detail.startswith("a prior worker run on this task is still alive")
        assert f"run {run_id}" in detail
        assert f"pid {proc.pid}" in detail
        assert "identity verified" in detail
        # Refused before any claim churn: still parked in review.
        assert kb.get_task(conn, tid).status == "review"
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    # Control: once that worker is gone the same send-back goes through.
    ok, detail = kb.request_changes(
        conn, tid, reason="fix", claimer="apollo", coverage=COVERAGE)
    assert ok, detail
