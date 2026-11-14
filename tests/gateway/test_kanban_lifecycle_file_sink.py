"""``kanban.lifecycle_channel: "file:<path>"`` keeps the lifecycle digest off chat (t_89bfcd1f).

Ace 2026-11-14: the hourly "kanban lifecycle" digest in Discord #logs is agent bookkeeping.
With a ``file:`` channel the digest is appended to a local file, one JSON line per digest,
same rendered text; no adapter ``send`` reaches the chat platform. Driven through one real
notifier tick and a digest flush, like test_kanban_lifecycle_channel.py.
"""
import asyncio
import json

import yaml

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kbn
from hermes_constants import get_hermes_home


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text})

    async def handle_message(self, event):
        self.handled.append(event)


def _config(channel, window):
    path = get_hermes_home() / "config.yaml"
    cfg = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
    cfg.setdefault("kanban", {})["lifecycle_channel"] = channel
    cfg["kanban"]["lifecycle_digest_seconds"] = window
    path.write_text(yaml.safe_dump(cfg))


def _card(conn):
    tid = kb.create_task(conn, boomerang_wake=False, title="card", assignee="worker")
    kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="origin",
                      chat_type="group", user_id="u1", delivery_mode="notify+wake")
    return tid


def _three(conn):
    a = _card(conn)
    kb.complete_task(conn, a, summary="one")
    b = _card(conn)
    kb.request_review(conn, b, summary="two", force=True)
    c = _card(conn)
    kb.block_task(conn, c, reason="three", kind="needs_input")
    return a, b, c


def _tick(monkeypatch, runner, clock):
    import gateway.kanban_watchers as kw

    monkeypatch.setattr(kw.time, "time", lambda: clock[0])
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    asyncio.run(runner._kanban_notifier_watcher(interval=1))


def _run(tmp_path, monkeypatch, channel, window):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "lc.db"))
    kb.init_db()
    _config(channel, window)
    with kb.connect_closing() as conn:
        ids = _three(conn)
    adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter, Platform.DISCORD: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    clock = [1000.0]
    _tick(monkeypatch, runner, clock)
    return ids, adapter, runner, clock


def _records(path):
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_digest_run_writes_one_local_record_and_sends_nothing(tmp_path, monkeypatch):
    out = tmp_path / "state" / "kanban-lifecycle-digest.jsonl"
    (a, b, c), adapter, runner, clock = _run(tmp_path, monkeypatch, f"file:{out}", 900)
    assert adapter.sent == [], "no line may reach a chat platform"
    assert len(adapter.handled) == 3, "the subscribers' wakes are unchanged"
    clock[0] += 900
    asyncio.run(runner._kanban_lifecycle_digest.flush(clock[0]))
    assert adapter.sent == [], adapter.sent
    recs = _records(out)
    assert len(recs) == 1, recs
    text = recs[0]["text"]
    assert "3 transition(s)" in text and "family=digest:kanban-lifecycle" in text
    assert f"{a} done" in text and "ready for review" in text and f"{c} blocked" in text
    assert recs[0]["family"] == "digest:kanban-lifecycle" and recs[0]["ts"]


def test_relative_file_path_resolves_under_hermes_home(tmp_path, monkeypatch):
    _ids, adapter, runner, clock = _run(tmp_path, monkeypatch, "file:state/kl.jsonl", 0)
    assert adapter.sent == []
    recs = _records(get_hermes_home() / "state" / "kl.jsonl")
    assert len(recs) == 3, "window 0 = one record per line, as one post per line on chat"


def test_chat_channel_still_posts_to_chat(tmp_path, monkeypatch):
    _ids, adapter, runner, clock = _run(tmp_path, monkeypatch, "discord:logs", 900)
    clock[0] += 900
    asyncio.run(runner._kanban_lifecycle_digest.flush(clock[0]))
    assert [m["chat_id"] for m in adapter.sent] == ["logs"]
