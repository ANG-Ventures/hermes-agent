"""t_54f4fde6 D1: a handback with a still-open PR waits on a MACHINE holder (``kanban.review_hold_assignee``)
instead of the operator; the land evaluator (hermes-home) hands it to the operator once CI and Prism settle.
Knob unset = today's routing, event for event."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_open_pr as op

PR_URL = "https://github.com/ANG-Ventures/example-home/pull/670"
HOLD = "review-hold"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(op, "_default_query", lambda: (lambda repo, n: {"state": "OPEN"}))
    monkeypatch.setattr(kb, "configured_review_assignee", lambda: "human:apollo")
    kb.init_db()
    return home


def _set_knob(home: Path, value):
    (home / "config.yaml").write_text(
        "kanban:\n  review_assignee: human:apollo\n" + (f"  review_hold_assignee: {value}\n" if value else ""))


def _handback(conn):
    tid = kb.create_task(conn, title="slice", assignee="worker")
    kb.claim_task(conn, tid)
    run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    assert kb.complete_task(conn, tid, summary=f"shipped {PR_URL}", metadata={"pr_url": PR_URL},
                            expected_run_id=run_id) is True
    return tid


def _stream(conn, tid):
    """(kind, payload minus volatile ids) for every event after the claim."""
    rows = conn.execute("SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id", (tid,)).fetchall()
    out = []
    for r in rows:
        p = json.loads(r["payload"]) if r["payload"] else None
        if isinstance(p, dict):
            p = {k: v for k, v in p.items() if k not in ("lock", "expires", "run_id", "review_exit_id")}
        out.append((r["kind"], p))
    return out


# Captured from the routing BEFORE this change (fork main d86b0e48, same fixture): the knob-unset stream.
TODAY = [
    ("created", None), ("claimed", None),
    ("review_requested", {"summary": "auto-routed: PR ANG-Ventures/example-home#670 still open",
                          "implementer": "worker", "reviewer": "human:apollo"}),
    ("completion_routed_to_review", {"open_prs": ["ANG-Ventures/example-home#670"],
                                     "note": "auto-routed: PR ANG-Ventures/example-home#670 still open"}),
    ("commented", None),
]


def _shape(stream):
    return [(k, p if k in ("review_requested", "completion_routed_to_review") else None) for k, p in stream]


def test_knob_unset_stream_equals_today(kanban_home):
    _set_knob(kanban_home, None)
    with kb.connect() as conn:
        tid = _handback(conn)
        assert _shape(_stream(conn, tid)) == TODAY
        row = conn.execute("SELECT status, assignee FROM tasks WHERE id=?", (tid,)).fetchone()
        assert (row["status"], row["assignee"]) == ("review", "human:apollo")


@pytest.mark.parametrize("name", ["worker", "human:apollo"])
def test_knob_naming_an_installed_profile_or_human_lane_routes_as_unset(kanban_home, monkeypatch, caplog, name):
    """t_1e4649b7: a holder that is (or later becomes) a real profile would be spawned as a reviewer; a
    human:* name is the operator lane. Either way the knob is treated as unset."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda n: n == "worker")
    _set_knob(kanban_home, name)
    assert kb.configured_review_hold_assignee() is None
    assert not kb.is_review_hold_assignee(name)
    with kb.connect() as conn:
        tid = _handback(conn)
        assert _shape(_stream(conn, tid)) == TODAY
        row = conn.execute("SELECT status, assignee FROM tasks WHERE id=?", (tid,)).fetchone()
        assert (row["status"], row["assignee"]) == ("review", "human:apollo")
    if name == "worker":
        assert "review_hold_assignee" in caplog.text


def test_knob_set_parks_on_holder_and_is_not_awaiting_a_human(kanban_home):
    _set_knob(kanban_home, HOLD)
    with kb.connect() as conn:
        tid = _handback(conn)
        row = conn.execute("SELECT status, assignee FROM tasks WHERE id=?", (tid,)).fetchone()
        assert (row["status"], row["assignee"]) == ("review", HOLD)
        rr = [p for k, p in _stream(conn, tid) if k == "review_requested"]
        assert rr[-1]["reviewer"] == HOLD and rr[-1]["implementer"] == "worker"
        assert "completion_routed_to_review" in [k for k, _ in _stream(conn, tid)]
        # the kernel's operator-review alarm (review_awaiting_human -> review_stale_alerted) never sees it
        assert [e["task_id"] for e in kb.review_awaiting_human(conn, stale_minutes=0)] == []
        assert kb.arm_review_stale_alerts(conn, kb.review_awaiting_human(conn, stale_minutes=0)) == []
        assert "review_stale_alerted" not in [k for k, _ in _stream(conn, tid)]


def test_knob_set_negative_handoff_still_goes_to_operator(kanban_home, monkeypatch):
    _set_knob(kanban_home, HOLD)
    from hermes_cli import kanban_negative_handoff as neg
    monkeypatch.setattr(neg, "match", lambda texts, metadata: "not landed")
    monkeypatch.setattr(kb, "configured_negative_handoff_review", lambda: True)
    with kb.connect() as conn:
        tid = _handback(conn)
        row = conn.execute("SELECT assignee FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["assignee"] == neg.REVIEWER


def test_holder_hands_to_operator_with_open_pr_and_keeps_implementer(kanban_home):
    _set_knob(kanban_home, HOLD)
    with kb.connect() as conn:
        tid = _handback(conn)
        # the open-PR refusal (ReviewHoldRequired) does not apply to the hold -> operator hand-off
        assert kb.assign_task(conn, tid, "human:apollo") is True
        row = conn.execute("SELECT status, assignee FROM tasks WHERE id=?", (tid,)).fetchone()
        assert (row["status"], row["assignee"]) == ("review", "human:apollo")
        rr = [p for k, p in _stream(conn, tid) if k == "review_requested"][-1]
        assert (rr["reviewer"], rr["implementer"], rr["handed_from"]) == ("human:apollo", "worker", HOLD)
        # t_9000e509: assign_task reads no checks, so the summary claims nothing about CI (it was "checks settled"
        # while the head's CI was still running)
        assert rr["summary"] == "review hold: handed to human:apollo for an operator record"
        # a later send-back still restores the worker
        ok, _detail = kb.request_sendback(conn, tid, payload={"source": "prism", "reason": "prism:P1:x",
                                                                "summary": "P1 x"})
        assert ok
        assert conn.execute("SELECT assignee FROM tasks WHERE id=?", (tid,)).fetchone()["assignee"] == "worker"


def test_operator_review_card_is_still_refused_without_request_changes(kanban_home):
    _set_knob(kanban_home, HOLD)
    with kb.connect() as conn:
        tid = _handback(conn)
        assert kb.assign_task(conn, tid, "human:apollo") is True
        with pytest.raises(kb.ReviewHoldRequired):
            kb.assign_task(conn, tid, "human:other")
