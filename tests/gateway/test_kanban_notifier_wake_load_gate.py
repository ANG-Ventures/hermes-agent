"""Notifier: a wake under host contention is delivered as a notify (t_74bf5296).

Drives the real ``_kanban_notifier_watcher`` for one tick with a WakeGate fed
a fixed load. Above the band: the line is posted with the marker, no wake
turn, and the subscription row keeps notify+wake. Below: a normal wake.
"""
import asyncio

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


async def _tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _runner(adapter, load, tmp_path):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner._kanban_wake_gate = WakeGate(
        {"kanban": {"dispatch_load_gate": {"pause_above": 64, "resume_below": 48}}},
        ncpu=32, loadavg=lambda: (load, load, load), lane_probe=lambda p: None,
        state_file=tmp_path / "wake_gate.json",
    )
    return runner


def _blocked_task():
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="gate task", assignee="worker",
                             session_id="agent:main:telegram:dm:chat-1")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                          chat_type="dm", delivery_mode="notify+wake")
        kb.block_task(conn, tid, reason="need a decision")
        return tid
    finally:
        conn.close()


def _mode(tid):
    conn = kb.connect()
    try:
        return [s["delivery_mode"] for s in kbn.list_notify_subs(conn, tid)]
    finally:
        conn.close()


def test_wake_downgraded_to_notify_above_the_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "hot.db"))
    kb.init_db()
    tid = _blocked_task()
    adapter = RecordingAdapter()
    asyncio.run(_tick(monkeypatch, _runner(adapter, 71.0, tmp_path)))
    assert len(adapter.sent) == 1 and "(notify: host load 71)" in adapter.sent[0]
    assert adapter.handled == [], "no wake turn under contention"
    assert _mode(tid) == ["notify+wake"], "the subscription itself is unchanged"


def test_wake_delivered_below_the_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "cool.db"))
    kb.init_db()
    _blocked_task()
    adapter = RecordingAdapter()
    asyncio.run(_tick(monkeypatch, _runner(adapter, 10.0, tmp_path)))
    assert len(adapter.sent) == 1 and "(notify:" not in adapter.sent[0]
    assert len(adapter.handled) == 1, "a real wake below the gate"
