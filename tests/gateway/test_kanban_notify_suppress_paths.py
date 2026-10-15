"""Notifier + owner-wake integration of kanban.notify_suppress (t_4c7da648).

The predicate's unit coverage is ``test_kanban_notify_suppress.py``; this file drives
the real notifier tick and the owner-wake tick against a temp board.
"""
from __future__ import annotations

import asyncio
import json

import pytest
import yaml

from gateway import kanban_home_route as hr
from gateway import kanban_notify_suppress as ns
from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_constants import get_hermes_home

HOME_SID = "20261011_000000_home"
CHAT = "chat-home"
THREAD_SID = "20261011_000000_thread"
THREAD = "1558761637211803680"


class Adapter:
    def __init__(self):
        self.sent, self.handled = [], []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text})

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


def _cfg(**kanban):
    path = get_hermes_home() / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
    cfg.setdefault("kanban", {}).update(kanban)
    path.write_text(yaml.safe_dump(cfg))
    from hermes_cli import config as _c
    for name in ("_invalidate_config_cache", "invalidate_config_cache"):
        fn = getattr(_c, name, None)
        if callable(fn):
            fn()


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    for var in ("HERMES_KANBAN_BOARD", "HERMES_SESSION_ID", "HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    # the home session's state.db row: a discord group chat on the default profile
    rows = {HOME_SID: {"session_key": f"agent:main:discord:group:{CHAT}:u1", "profile_name": "default",
                       "origin_json": json.dumps({"platform": "discord", "chat_id": CHAT, "chat_type": "group",
                                                  "user_id": "u1"})},
            # t_d654c294: a Discord thread session; origin and sub rows both carry thread_id == chat_id
            THREAD_SID: {"session_key": f"agent:main:discord:thread:{THREAD}:{THREAD}", "profile_name": "default",
                         "origin_json": json.dumps({"platform": "discord", "chat_id": THREAD,
                                                    "thread_id": THREAD, "chat_type": "thread",
                                                    "user_id": "u1"})}}
    monkeypatch.setattr(hr, "read_session_row", lambda sid: rows.get(sid))
    return tmp_path


def _runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner._active_profile_name = lambda: "default"
    return runner


async def _one_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _card(conn, *, chat=CHAT):
    tid = kb.create_task(conn, title="self close", assignee="worker", session_id=HOME_SID)
    kbn.add_notify_sub(conn, task_id=tid, platform="discord", chat_id=chat, chat_type="group",
                       user_id="u1", delivery_mode="notify+wake")
    return tid


def _events(conn, tid, kind):
    return [e for e in kb.list_events(conn, tid) if e.kind == kind]


def test_operator_self_close_posts_nothing_and_leaves_receipt(board, monkeypatch):
    conn = kbc.connect()
    try:
        tid = _card(conn)
        with kb.mutation_actor(session_ids=[HOME_SID], profile="default", surface="gateway"):
            kb.complete_task(conn, tid, summary="closed by the operator itself")
        assert _events(conn, tid, "completed")[0].actor_session_id == HOME_SID
    finally:
        conn.close()
    adapter = Adapter()
    asyncio.run(_one_tick(monkeypatch, _runner(adapter)))
    assert adapter.sent == [], adapter.sent          # no chat line
    assert adapter.handled == []                     # no wake turn
    conn = kbc.connect()
    try:
        receipts = _events(conn, tid, ns.EVENT_KIND)
        assert len(receipts) == 1
        assert receipts[0].payload["reason"] in ("self-session", "self-operator")
        assert receipts[0].payload["path"] == "notifier"
        # the cursor advanced: the event is consumed, not re-claimed next tick
        _, unseen = kbn.unseen_events_for_sub(conn, task_id=tid, platform="discord", chat_id=CHAT,
                                              kinds=["completed"])
        assert unseen == []
    finally:
        conn.close()


def test_worker_review_requested_still_posts_and_wakes(board, monkeypatch):
    conn = kbc.connect()
    try:
        tid = _card(conn)
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        with kb.mutation_actor(session_ids=["20261011_000001_worker"], profile="worker", surface="worker"):
            assert kb.request_review(conn, tid, summary="PR open", expected_run_id=run_id) is True
    finally:
        conn.close()
    adapter = Adapter()
    asyncio.run(_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.sent) == 1 and "review" in adapter.sent[0]["text"].lower()
    assert len(adapter.handled) == 1
    conn = kbc.connect()
    try:
        assert _events(conn, tid, ns.EVENT_KIND) == []
    finally:
        conn.close()


def test_kill_switch_restores_the_echo(board, monkeypatch):
    _cfg(notify_suppress={"enabled": False})
    conn = kbc.connect()
    try:
        tid = _card(conn)
        with kb.mutation_actor(session_ids=[HOME_SID], profile="default", surface="gateway"):
            kb.complete_task(conn, tid, summary="closed by the operator itself")
    finally:
        conn.close()
    adapter = Adapter()
    asyncio.run(_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.sent) == 1                    # the passive line is back
    conn = kbc.connect()
    try:
        assert _events(conn, tid, ns.EVENT_KIND) == []
    finally:
        conn.close()


def test_worker_completed_is_not_suppressed(board, monkeypatch):
    conn = kbc.connect()
    try:
        tid = _card(conn)
        with kb.mutation_actor(session_ids=["20261011_000001_worker"], profile="worker", surface="worker"):
            kb.complete_task(conn, tid, summary="worker finished")
    finally:
        conn.close()
    adapter = Adapter()
    asyncio.run(_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.sent) == 1 and len(adapter.handled) == 1


def test_foreign_subscriber_still_gets_the_home_operators_close(board, monkeypatch):
    """Prism 03f8b8156055: another chat subscribed to the card is NOT the home; the home
    operator's close is news to it. Line + wake delivered, no receipt."""
    conn = kbc.connect()
    try:
        tid = _card(conn, chat="chat-other")
        with kb.mutation_actor(session_ids=[HOME_SID], profile="default", surface="gateway"):
            kb.complete_task(conn, tid, summary="closed by the home operator")
    finally:
        conn.close()
    adapter = Adapter()
    asyncio.run(_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.sent) == 1 and adapter.sent[0]["chat_id"] == "chat-other"
    assert len(adapter.handled) == 1
    conn = kbc.connect()
    try:
        assert _events(conn, tid, ns.EVENT_KIND) == []
    finally:
        conn.close()


def test_kill_switch_is_read_from_the_real_config_file(board, monkeypatch):
    """Prism 2637ae1a04f9: the loader import must resolve; prove the knob through config.yaml."""
    _cfg(notify_suppress={"enabled": False})
    assert ns.resolve_config().enabled is False
    _cfg(notify_suppress={"enabled": True, "self_window_min": 5, "kinds": ["completed"]})
    cfg = ns.resolve_config()
    assert cfg.enabled is True and cfg.self_window_s == 300 and cfg.kinds == {"completed"}


def test_receipt_failure_keeps_the_event_deliverable(board, monkeypatch):
    """Prism 91fac1e92097: no persisted notify_suppressed row -> the line and wake go out."""
    monkeypatch.setattr(ns, "record_suppressed", lambda *a, **k: False)
    conn = kbc.connect()
    try:
        tid = _card(conn)
        with kb.mutation_actor(session_ids=[HOME_SID], profile="default", surface="gateway"):
            kb.complete_task(conn, tid, summary="closed by the operator itself")
    finally:
        conn.close()
    adapter = Adapter()
    asyncio.run(_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.sent) == 1 and len(adapter.handled) == 1


def _thread_card(conn, *, chat=THREAD, thread=THREAD):
    tid = kb.create_task(conn, title="thread self close", assignee="worker", session_id=THREAD_SID)
    kbn.add_notify_sub(conn, task_id=tid, platform="discord", chat_id=chat, thread_id=thread,
                       chat_type="thread", user_id="u1", delivery_mode="notify+wake")
    return tid


def test_thread_home_operator_self_close_is_suppressed(board, monkeypatch):
    """t_d654c294: a Discord-thread sub keeps thread_id == chat_id while the home target
    normalises it to ""; the home check must still see the sub as the home chat."""
    conn = kbc.connect()
    try:
        tid = _thread_card(conn)
        with kb.mutation_actor(session_ids=[THREAD_SID], profile="default", surface="gateway"):
            kb.complete_task(conn, tid, summary="closed from the thread session")
    finally:
        conn.close()
    adapter = Adapter()
    asyncio.run(_one_tick(monkeypatch, _runner(adapter)))
    assert adapter.sent == [], adapter.sent
    assert adapter.handled == []
    conn = kbc.connect()
    try:
        receipts = _events(conn, tid, ns.EVENT_KIND)
        assert len(receipts) == 1
        assert receipts[0].payload["reason"] == "self-session"
        assert receipts[0].payload["path"] == "notifier"
    finally:
        conn.close()


def test_thread_home_foreign_actor_close_is_delivered(board, monkeypatch):
    conn = kbc.connect()
    try:
        tid = _thread_card(conn)
        with kb.mutation_actor(session_ids=["20261011_000001_worker"], profile="worker", surface="worker"):
            kb.complete_task(conn, tid, summary="worker finished")
    finally:
        conn.close()
    adapter = Adapter()
    asyncio.run(_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.sent) == 1 and len(adapter.handled) == 1
    conn = kbc.connect()
    try:
        assert _events(conn, tid, ns.EVENT_KIND) == []
    finally:
        conn.close()


def test_thread_home_close_still_reaches_a_foreign_thread_subscriber(board, monkeypatch):
    """Prism 03f8b8156055 holds for threads: a different thread is not the home."""
    conn = kbc.connect()
    try:
        tid = _thread_card(conn, chat="1558761637211800000", thread="1558761637211800000")
        with kb.mutation_actor(session_ids=[THREAD_SID], profile="default", surface="gateway"):
            kb.complete_task(conn, tid, summary="closed by the home operator")
    finally:
        conn.close()
    adapter = Adapter()
    asyncio.run(_one_tick(monkeypatch, _runner(adapter)))
    assert len(adapter.sent) == 1
    conn = kbc.connect()
    try:
        assert _events(conn, tid, ns.EVENT_KIND) == []
    finally:
        conn.close()
