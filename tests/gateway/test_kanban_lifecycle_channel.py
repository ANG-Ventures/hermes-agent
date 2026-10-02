"""kanban.lifecycle_channel re-routes done / ready-for-review / blocked lines
(t_f7bba206). Driven through config.yaml in the sandboxed HERMES_HOME and one
real notifier tick."""
import asyncio

import yaml

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kbn
from hermes_constants import get_hermes_home

LOG = "log-chan"


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text})

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


def _runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _set_channel(value):
    path = get_hermes_home() / "config.yaml"
    cfg = yaml.safe_load(path.read_text()) if path.exists() else {}
    cfg = cfg or {}
    cfg.setdefault("kanban", {})["lifecycle_channel"] = value
    path.write_text(yaml.safe_dump(cfg))


def _card(conn, priority=0, mode="notify+wake"):
    tid = kb.create_task(conn, title="card", assignee="worker", priority=priority)
    kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="origin",
                      chat_type="group", user_id="u1", delivery_mode=mode)
    return tid


def _run(tmp_path, monkeypatch, channel, make):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "lc.db"))
    kb.init_db()
    if channel is not None:
        _set_channel(channel)
    with kb.connect_closing() as conn:
        tid = make(conn)
    adapter = RecordingAdapter()
    asyncio.run(_tick(monkeypatch, _runner(adapter)))
    return tid, adapter


def _complete(conn):
    tid = _card(conn)
    kb.complete_task(conn, tid, summary="shipped")
    return tid


def test_done_line_goes_to_lifecycle_channel_and_wake_stays(tmp_path, monkeypatch):
    tid, adapter = _run(tmp_path, monkeypatch, f"telegram:{LOG}", _complete)
    assert [(m["chat_id"], tid in m["text"] and " done" in m["text"]) for m in adapter.sent] == [(LOG, True)]
    assert len(adapter.handled) == 1, "the subscriber's agent is still woken"
    assert adapter.handled[0].source.chat_id == "origin"


def test_unset_keeps_subscriber_chat(tmp_path, monkeypatch):
    tid, adapter = _run(tmp_path, monkeypatch, None, _complete)
    assert [m["chat_id"] for m in adapter.sent] == ["origin"]


def test_malformed_value_keeps_subscriber_chat(tmp_path, monkeypatch):
    tid, adapter = _run(tmp_path, monkeypatch, "no-colon", _complete)
    assert [m["chat_id"] for m in adapter.sent] == ["origin"]


def test_review_and_plain_block_are_routed(tmp_path, monkeypatch):
    def make(conn):
        a = _card(conn)
        kb.request_review(conn, a, summary="PR ready", force=True)
        b = _card(conn)
        kb.block_task(conn, b, reason="waiting on parent", kind="dependency")
        return a, b

    (a, b), adapter = _run(tmp_path, monkeypatch, f"telegram:{LOG}", make)
    by_chat = {m["chat_id"] for m in adapter.sent}
    assert by_chat == {LOG}, adapter.sent
    texts = " ".join(m["text"] for m in adapter.sent)
    assert "ready for review" in texts and f"{b} blocked" in texts


def test_needs_input_block_on_p100_card_stays_in_origin(tmp_path, monkeypatch):
    def make(conn):
        hi = _card(conn, priority=100)
        kb.block_task(conn, hi, reason="need a ruling", kind="needs_input")
        lo = _card(conn, priority=99)
        kb.block_task(conn, lo, reason="need a ruling", kind="needs_input")
        return hi, lo

    (hi, lo), adapter = _run(tmp_path, monkeypatch, f"telegram:{LOG}", make)
    where = {m["text"].split("Kanban ")[1].split()[0]: m["chat_id"] for m in adapter.sent}
    assert where == {hi: "origin", lo: LOG}


def test_failure_lines_stay_in_origin(tmp_path, monkeypatch):
    def make(conn):
        tid = _card(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "stalled", {"progress_age_seconds": 900})
        return tid

    tid, adapter = _run(tmp_path, monkeypatch, f"telegram:{LOG}", make)
    assert [m["chat_id"] for m in adapter.sent] == ["origin"]


def test_lifecycle_channel_target_rules():
    from gateway.kanban_watchers import lifecycle_channel_target, parse_lifecycle_channel

    ch = parse_lifecycle_channel(" Discord:123 ")
    assert ch == ("discord", "123")
    assert parse_lifecycle_channel("") is None and parse_lifecycle_channel("discord:") is None

    class T:
        priority = 150

    assert lifecycle_channel_target(ch, "completed", {}, T()) == ch
    assert lifecycle_channel_target(ch, "blocked", {"kind": "needs_input"}, T()) is None
    assert lifecycle_channel_target(ch, "blocked", {"kind": "capability"}, T()) == ch
    assert lifecycle_channel_target(ch, "crashed", {}, T()) is None
    assert lifecycle_channel_target(None, "completed", {}, T()) is None
