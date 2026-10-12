"""t_b7b31563: a downgraded wake for a HUMAN-owned card is DEFERRED, never dropped.

Incident 2026-10-12 05:38: ``wake for t_ac5f359a on discord/… sent as notify
(host load 184)`` on a review_requested handoff to human:apollo; no turn ran
for 25 min. Drives the real ``_kanban_notifier_watcher`` with a WakeGate fed a
fixed load and a fake dispatcher LoadGate state, then the real
``kanban_wake_defer.tick``.

Cases (card body): downgraded review wake -> row written; gate clears -> fired
once; ceiling -> fired once; owner comment in between -> dropped; restart
between downgrade and fire -> still fires. Plus the gate-input case: load1 184
with lane headroom and no proc_paused -> no downgrade for a human-owner wake.
"""
import asyncio
import json

import pytest

from gateway import kanban_wake_defer as defer
from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kbn
from hermes_cli.kanban_wake_gate import WakeGate


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append(text)

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


async def _tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


class FakeDispatcher:
    """The dispatcher LoadGate's published state (``load_gate.json``)."""

    def __init__(self, state="admitting"):
        self.state = state

    def __call__(self):
        return {"state": self.state}


def _runner(adapter, load, tmp_path, *, dispatcher=None):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner._kanban_wake_gate = WakeGate(
        {"kanban": {"dispatch_load_gate": {"pause_above": 64, "resume_below": 48}}},
        ncpu=32, loadavg=lambda: (load, load, load), lane_probe=lambda p: None,
        dispatcher_state=dispatcher or FakeDispatcher("admitting"),
        state_file=tmp_path / "wake_gate.json",
    )
    runner._kanban_pending_wakes = defer.PendingWakes(tmp_path / "gateway" / "kanban_pending_wakes.json")
    return runner


def _human_review_task(*, summary="PR ready: https://example.invalid/pr/7\nfull details below"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="E2 chain slice", assignee="worker",
                             session_id="agent:main:telegram:dm:chat-1")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                          chat_type="dm", delivery_mode="notify+wake")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.request_review(conn, tid, summary=summary, reviewer="human:apollo",
                                 expected_run_id=run_id) is True
        return tid
    finally:
        conn.close()


def _set_load(runner, load):
    runner._kanban_wake_gate._loadavg = lambda: (load, load, load)
    runner._kanban_wake_gate._sampled_at = None  # force a re-sample


def _pending(runner):
    return runner._kanban_pending_wakes.rows


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "defer.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    return tmp_path


# --- (3) gate input -----------------------------------------------------------


def test_load1_alone_does_not_downgrade_a_human_owner_review_wake(board, monkeypatch, caplog):
    """load1 184, lane headroom fine, dispatcher NOT proc_paused -> a real wake."""
    tid = _human_review_task()
    adapter = RecordingAdapter()
    runner = _runner(adapter, 184.0, board, dispatcher=FakeDispatcher("admitting"))
    with caplog.at_level("INFO"):
        asyncio.run(_tick(monkeypatch, runner))
    assert len(adapter.sent) == 1 and "(notify:" not in adapter.sent[0]
    assert len(adapter.handled) == 1, "load1 alone must not silence a human-owner wake"
    assert "PR ready: https://example.invalid/pr/7" in adapter.handled[0].text
    assert _pending(runner) == {}
    assert "sent as notify" not in caplog.text


def test_worker_owner_wake_still_downgrades_on_load1(board, monkeypatch):
    """The t_74bf5296 rule is unchanged for a worker-owned card."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="gate task", assignee="worker",
                             session_id="agent:main:telegram:dm:chat-1")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                          chat_type="dm", delivery_mode="notify+wake")
        kb.block_task(conn, tid, reason="need a decision")
    finally:
        conn.close()
    adapter = RecordingAdapter()
    runner = _runner(adapter, 184.0, board)
    asyncio.run(_tick(monkeypatch, runner))
    assert "(notify: host load 184)" in adapter.sent[0]
    assert adapter.handled == []
    assert _pending(runner) == {}, "a worker-owned card is not deferred (no human to wait on)"


# --- (1) pending wake ---------------------------------------------------------


def _deferred_runner(board, monkeypatch, caplog):
    tid = _human_review_task()
    adapter = RecordingAdapter()
    runner = _runner(adapter, 184.0, board, dispatcher=FakeDispatcher("proc_paused"))
    with caplog.at_level("INFO"):
        asyncio.run(_tick(monkeypatch, runner))
    return tid, adapter, runner


def test_downgraded_review_wake_writes_a_pending_row(board, monkeypatch, caplog):
    tid, adapter, runner = _deferred_runner(board, monkeypatch, caplog)
    assert len(adapter.sent) == 1 and "(notify: dispatcher proc_paused)" in adapter.sent[0]
    assert adapter.handled == [], "no wake turn while proc_paused"
    rows = list(_pending(runner).values())
    assert len(rows) == 1
    row = rows[0]
    assert row["task_id"] == tid and row["platform"] == "telegram" and row["chat_id"] == "chat-1"
    assert row["kinds"] == ["review_requested"] and row["owner"] == "human:apollo"
    assert row["reason"] == "dispatcher proc_paused" and row["cursor"] > 0
    assert "PR ready: https://example.invalid/pr/7" in row["text"]
    on_disk = json.loads((board / "gateway" / "kanban_pending_wakes.json").read_text())
    assert tid in json.dumps(on_disk), "the row is persisted in the gateway state dir"
    assert f"kanban wake deferred for {tid} on telegram/chat-1: dispatcher proc_paused" in caplog.text
    assert f"sent as notify (dispatcher proc_paused); deferred for human:apollo" in caplog.text


def test_gate_clears_fires_the_wake_once(board, monkeypatch, caplog):
    tid, adapter, runner = _deferred_runner(board, monkeypatch, caplog)
    # still paused: nothing fires
    assert asyncio.run(defer.tick(runner)) == 0 and adapter.handled == []
    runner._kanban_wake_gate._dispatcher_state = FakeDispatcher("admitting")
    _set_load(runner, 10.0)
    with caplog.at_level("INFO"):
        assert asyncio.run(defer.tick(runner)) == 1
    assert len(adapter.handled) == 1
    text = adapter.handled[0].text
    assert tid in text and "PR ready: https://example.invalid/pr/7" in text
    assert "(deferred 0m under dispatcher proc_paused)" in text
    assert _pending(runner) == {}
    assert f"kanban wake deferred-fire for {tid} on telegram/chat-1 after 0m: gate cleared (was: dispatcher proc_paused)" in caplog.text
    assert asyncio.run(defer.tick(runner)) == 0 and len(adapter.handled) == 1, "fired once"


def test_ceiling_fires_the_wake_once_while_still_contended(board, monkeypatch, caplog):
    tid, adapter, runner = _deferred_runner(board, monkeypatch, caplog)
    monkeypatch.setattr(defer, "ceiling_seconds", lambda: 1200)
    first_seen = next(iter(_pending(runner).values()))["first_seen"]
    assert asyncio.run(defer.tick(runner, now=first_seen + 1199)) == 0
    with caplog.at_level("INFO"):
        assert asyncio.run(defer.tick(runner, now=first_seen + 1200)) == 1
    assert len(adapter.handled) == 1
    assert "(deferred 20m under dispatcher proc_paused)" in adapter.handled[0].text
    assert f"kanban wake deferred-fire for {tid} on telegram/chat-1 after 20m: ceiling 1200s reached (still: dispatcher proc_paused)" in caplog.text
    assert asyncio.run(defer.tick(runner, now=first_seen + 1300)) == 0 and len(adapter.handled) == 1


def test_owner_comment_in_between_drops_the_row(board, monkeypatch, caplog):
    tid, adapter, runner = _deferred_runner(board, monkeypatch, caplog)
    conn = kb.connect()
    try:
        kb.add_comment(conn, tid, author="human:apollo", body="on it")
    finally:
        conn.close()
    runner._kanban_wake_gate._dispatcher_state = FakeDispatcher("admitting")
    _set_load(runner, 10.0)
    with caplog.at_level("INFO"):
        assert asyncio.run(defer.tick(runner)) == 0
    assert adapter.handled == [], "the owner already acted: no wake"
    assert _pending(runner) == {}
    assert f"kanban wake deferred dropped for {tid} on telegram/chat-1: owner human:apollo commented" in caplog.text


def test_restart_between_downgrade_and_fire_still_fires(board, monkeypatch, caplog):
    tid, adapter, runner = _deferred_runner(board, monkeypatch, caplog)
    # A new gateway process: fresh runner, state re-read from disk.
    adapter2 = RecordingAdapter()
    runner2 = _runner(adapter2, 10.0, board, dispatcher=FakeDispatcher("admitting"))
    assert tid in json.dumps(_pending(runner2)), "the row survived the restart"
    with caplog.at_level("INFO"):
        assert asyncio.run(defer.tick(runner2)) == 1
    assert len(adapter2.handled) == 1 and tid in adapter2.handled[0].text
    assert "(deferred 0m under dispatcher proc_paused)" in adapter2.handled[0].text
    assert _pending(runner2) == {}
    assert not json.loads((board / "gateway" / "kanban_pending_wakes.json").read_text())["pending"]


def test_card_done_while_deferred_drops_the_row(board, monkeypatch, caplog):
    tid, adapter, runner = _deferred_runner(board, monkeypatch, caplog)
    conn = kb.connect()
    try:
        kb.complete_task(conn, tid, summary="landed", force=True)
    finally:
        conn.close()
    with caplog.at_level("INFO"):
        assert asyncio.run(defer.tick(runner)) == 0
    assert adapter.handled == [] and _pending(runner) == {}
    assert f"kanban wake deferred dropped for {tid} on telegram/chat-1: card is done" in caplog.text


def test_ceiling_config_knob(monkeypatch):
    from hermes_cli import config as _cfg

    monkeypatch.setattr(_cfg, "load_config_readonly", lambda: {"kanban": {"wake_defer_ceiling_s": 300}})
    assert defer.ceiling_seconds() == 300
    monkeypatch.setattr(_cfg, "load_config_readonly", lambda: {"kanban": {"wake_defer_ceiling_s": "junk"}})
    assert defer.ceiling_seconds() == defer.DEFAULT_CEILING_S
