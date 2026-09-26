"""Kanban failure notices: one message per failure event, one per lane-wide cause.

Regression for the 2026-09-25 Codex-cooldown storm (t_a7202bf6): four cards on
one lane each posted ``crashed`` + ``gave_up`` every spawn cycle — 40+ chat
messages in 20 minutes for a single root cause.
"""

import asyncio

from gateway import kanban_notify_failures as knf
from hermes_cli import kanban_db as kb
from tests.gateway.test_kanban_notifier import (
    RecordingAdapter,
    _make_runner,
    _run_one_notifier_tick,
)

_COOLDOWN_TAIL = (
    "[hermes-kanban-run-boundary 138bbc4f52611224]\nWarning: Unknown toolsets: rl\n"
    "Codex credential is in cooldown.\r\n\n"
    "[hermes-kanban-run-boundary f15948e954fe4a9f]\nWarning: Unknown toolsets: rl\n"
    "Codex credential is in cooldown."
)


def _replay_incident_cycle(tids, stderr_tail=_COOLDOWN_TAIL):
    """One dispatcher tick of the incident: crashed then gave_up per card."""
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            for i, tid in enumerate(tids):
                pid = 94199 + i
                kb._append_event(conn, tid, "crashed", {
                    "pid": pid, "exit_kind": "nonzero_exit", "exit_code": 1,
                    "stderr_tail": stderr_tail, "retry_status": "ready",
                })
            for i, tid in enumerate(tids):
                kb._append_event(conn, tid, "gave_up", {
                    "failures": 1, "effective_limit": 1,
                    "error": f"pid {94199 + i} exited with code 1",
                    "trigger_outcome": "crashed", "stderr_tail": stderr_tail,
                })


def _lane_cards(n, assignee="daedalus-sol"):
    with kb.connect_closing() as conn:
        tids = []
        for i in range(n):
            tid = kb.create_task(conn, title=f"card {i}", assignee=assignee)
            kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
            tids.append(tid)
        return tids


def _tick(monkeypatch, runner):
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))


def test_incident_replay_posts_exactly_one_lane_message(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "storm.db"))
    kb.init_db()
    tids = _lane_cards(4)
    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    _replay_incident_cycle(tids)
    _tick(monkeypatch, runner)
    assert len(adapter.sent) == 1, [m["text"] for m in adapter.sent]
    text = adapter.sent[0]["text"]
    assert "daedalus-sol" in text
    assert "Codex credential is in cooldown." in text
    assert all(tid in text for tid in tids)
    assert "will retry" not in text

    # The next spawn cycle inside the window is the same cause: stay quiet.
    _replay_incident_cycle(tids)
    _tick(monkeypatch, runner)
    assert len(adapter.sent) == 1


def test_genuine_crash_still_posts(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "real.db"))
    kb.init_db()
    (tid,) = _lane_cards(1, assignee="worker")
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "crashed", {
                "pid": 7, "exit_kind": "nonzero_exit", "exit_code": 1,
                "stderr_tail": "[hermes-kanban-run-boundary aa]\nTraceback ...\nKeyError: 'model'",
            })
    adapter = RecordingAdapter()
    _tick(monkeypatch, _make_runner(adapter))
    assert len(adapter.sent) == 1
    assert "worker crashed" in adapter.sent[0]["text"]
    assert "KeyError: 'model'" in adapter.sent[0]["text"]


def test_gave_up_is_never_hidden_by_an_earlier_crash_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "later.db"))
    kb.init_db()
    (tid,) = _lane_cards(1, assignee="worker")
    tail = "[hermes-kanban-run-boundary aa]\nKeyError: 'model'"
    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "crashed", {"pid": 7, "stderr_tail": tail})
    _tick(monkeypatch, runner)
    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "gave_up", {"failures": 2, "stderr_tail": tail})
    _tick(monkeypatch, runner)
    assert [("gave up after 2 spawn failures" in m["text"]) for m in adapter.sent] == [False, True]


# --- mutation arms: each switch-off must re-open the storm -------------------


def test_mutant_dedupe_off_yields_one_message_per_card(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "mut-dedupe.db"))
    kb.init_db()
    tids = _lane_cards(4)
    monkeypatch.setattr(knf, "_lane_key", lambda d, kind, c: (d["sub"]["task_id"], kind, c))
    adapter = RecordingAdapter()
    _replay_incident_cycle(tids)
    _tick(monkeypatch, _make_runner(adapter))
    assert len(adapter.sent) == 4


def test_mutant_pair_collapse_off_yields_two_messages_per_card(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "mut-pair.db"))
    kb.init_db()
    (tid,) = _lane_cards(1)
    monkeypatch.setattr(knf, "collapse_retry_pairs", lambda events: events)
    adapter = RecordingAdapter()
    _replay_incident_cycle([tid])
    _tick(monkeypatch, _make_runner(adapter))
    assert len(adapter.sent) == 2
