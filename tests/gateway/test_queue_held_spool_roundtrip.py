"""Held /queue items survive a gateway restart as HELD, never as a turn (spec 2026-10-12 D22/I13).

Drives the real shutdown sweep (``_spool_adapter_pending_for_restart``) and the real boot loader
(``_load_restart_followups``) through the real restart-follow-up spool on disk.
"""

import importlib
import os
import time
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.fork_ext import restart_followups as rf
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource

KEY = "agent:main:telegram:dm:99"


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True, message_id="m1")

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="99", chat_type="dm")


def _runner():
    gateway_run = importlib.import_module("gateway.run")
    runner = object.__new__(gateway_run.GatewayRunner)
    adapter = _Adapter()
    runner.adapters = {adapter.platform: adapter}
    runner._running_agents = {}
    runner.config = SimpleNamespace(thread_sessions_per_user=False, group_sessions_per_user=False)
    runner._restore_queue = []
    runner._queue_startup_restore_event = lambda ev: runner._restore_queue.append(ev)
    runner._status_action_label = lambda: "restart"
    return runner, adapter


def _ev(text):
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=_source())


@pytest.mark.asyncio
async def test_held_queue_spool_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    before, adapter = _runner()
    before._session_state(KEY).conversation.held_queue.extend([_ev("a"), _ev("b")])
    adapter._pending_messages[KEY] = _ev("active-1")

    spooled = await before._spool_adapter_pending_for_restart()
    assert spooled == 3
    assert list(before._session_state(KEY).conversation.held_queue) == []      # removed once spooled
    files = sorted(rf.spool_dir().glob("*.json"))
    assert len(files) == 3

    after, _ = _runner()                                                         # the next process
    await after._load_restart_followups()
    assert [e.text for e in after._session_state(KEY).conversation.held_queue] == ["a", "b"]
    assert [e.text for e in after._restore_queue] == ["active-1"]                # replays as a turn
    assert len(sorted(rf.spool_dir().glob("*.json"))) == 1                       # held acked; active
                                                                                 # acked on replay


@pytest.mark.asyncio
async def test_held_records_are_never_stale(tmp_path, monkeypatch):
    """Downtime beyond MAX_REPLAY_AGE_S must not strand a held item as *.stale."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    before, _ = _runner()
    before._session_state(KEY).conversation.held_queue.append(_ev("old"))
    await before._spool_adapter_pending_for_restart()
    later = time.time() + rf.MAX_REPLAY_AGE_S + 3600
    records, stale = rf.take_followups(now=later)
    assert stale == 0 and [r["text"] for r in records] == ["old"]


@pytest.mark.asyncio
async def test_held_items_spool_before_drain_survive_forced_exit(tmp_path, monkeypatch):
    """The shutdown watchdog can os._exit a wedged drain before the late sweep runs. Held items
    must already be on disk once the teardown's notify phase is done (2026-10-26 03:11: a /stop-held
    voice note was missed by the spool and re-injected by restart backfill as a new turn)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    before, _ = _runner()
    before._session_state(KEY).conversation.held_queue.extend([_ev("voice"), _ev("link")])

    async def _noop(*a, **k):
        return None

    before._restart_requested = True
    before._cancel_secondary_profile_reconnect_tasks = _noop
    before._notify_active_sessions_of_shutdown = _noop
    before._cancel_pending_boot_resumes_for_shutdown = _noop
    before._mark_api_runs_shutdown_requested = lambda: None
    before._clear_plugin_message_injector = lambda: None
    ctx = SimpleNamespace(elapsed=lambda: 0.0, started_at=None)
    await before._stop_begin_teardown(ctx)
    # Simulated forced exit: nothing after _stop_begin_teardown runs.
    assert list(before._session_state(KEY).conversation.held_queue) == []
    assert len(sorted(rf.spool_dir().glob("*.json"))) == 2

    after, _ = _runner()
    await after._load_restart_followups()
    assert [e.text for e in after._session_state(KEY).conversation.held_queue] == ["voice", "link"]
    assert after._restore_queue == []                                             # never a turn
    assert await before._spool_held_queues_for_restart() == 0                     # late sweep no-op
