"""kanban_show on a card with a long history (t_ad44f109).

On t_94e91559 a worker's card read was 154,957 chars, so it spilled, and the operator's review
(the newest run) sat behind 150 KB of older runs the worker never got to. The read must stay under
the spill threshold and lead with the newest review, in full.
"""
from __future__ import annotations

import json

import pytest

REVIEW = ("REVIEW-START changes requested: the head 7bab7b62 still writes the token to the log; "
          + "fix detail line. " * 300 + "REVIEW-END")


@pytest.fixture
def long_card(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="long-history", assignee="test-worker")
        now = 1_790_000_000
        filler = "older handoff detail. " * 700  # ~15 KB per run
        with conn:
            for i in range(14):
                conn.execute(
                    "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, "
                    "summary, metadata) VALUES (?, 'test-worker', 'done', ?, ?, 'review_requested', ?, ?)",
                    (tid, now + i * 100, now + i * 100 + 50, f"handback {i}: " + filler,
                     json.dumps({"notes": filler[:6000]})))
            cur = conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, summary) "
                "VALUES (?, 'apollo', 'ready', ?, ?, 'changes_requested', ?)",
                (tid, now + 2000, now + 2050, REVIEW))
            review_run = cur.lastrowid
            kb._append_event(conn, tid, "changes_requested",
                             {"reason": REVIEW, "reviewer": "apollo", "implementer": "test-worker"},
                             run_id=review_run)
            for i in range(60):
                kb._append_event(conn, tid, "heartbeat", {"note": "x" * 3000})
        for i in range(40):
            kb.add_comment(conn, tid, "apollo", f"comment {i} " + "c" * 3000)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


def _history_chars(tid):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        return (sum(len(r.summary or "") + len(json.dumps(r.metadata or {})) for r in kb.list_runs(conn, tid))
                + sum(len(c.body) for c in kb.list_comments(conn, tid))
                + sum(len(json.dumps(e.payload or {})) for e in kb.list_events(conn, tid)))
    finally:
        conn.close()


def test_newest_review_in_full_within_first_20k(long_card):
    from tools import kanban_tools as kt
    assert _history_chars(long_card) > 150_000  # the fixture really is a 150 KB+ card

    out = kt._handle_show({})
    first = out[:20_000]
    assert "REVIEW-START" in first and "REVIEW-END" in first
    d = json.loads(out)
    assert d["latest_review"]["summary"] == REVIEW
    assert d["latest_review"]["reviewer"] == "apollo"
    assert d["latest_review"]["answered_by_later_handback"] is False
    assert d["runs"][0]["outcome"] == "changes_requested"  # newest first


def test_card_read_stays_under_the_spill_threshold(long_card):
    from tools import kanban_tools as kt
    from tools.budget_config import DEFAULT_RESULT_SIZE_CHARS
    out = kt._handle_show({})
    assert len(out) < DEFAULT_RESULT_SIZE_CHARS
    d = json.loads(out)
    assert d["omitted"]["comments"] >= 10 and d["omitted"]["events"] >= 10
    # The newest comment survives the cap.
    assert d["comments"][-1]["body"].startswith("comment 39 ")
