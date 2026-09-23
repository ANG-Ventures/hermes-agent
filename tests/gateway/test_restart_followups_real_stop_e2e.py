"""Adapter-parked follow-ups must survive the REAL stop(restart=True) -> boot path.

Argus r3 (t_e253d9d5): stop() ran ``_bounded_adapter_teardown()`` first, whose
``BasePlatformAdapter.cancel_background_tasks()`` drained ``_pending_messages``
into the #72680 shutdown flush file (not replayable as a turn: no session_id),
and only afterwards swept the (now empty) slot into the restart spool.  Result:
spool 0, boot adapter receipts 0.

Oracle is independent of the production spool/loader: the boot adapter's
``handle_message`` receipts, plus the on-disk spool and flush dirs.
"""

import asyncio
import time

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.fork_ext import restart_followups as rf
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    TextDebounceState,
)
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.shutdown_flush import _get_flush_dir


class _RecordingAdapter(BasePlatformAdapter):
    def __init__(self, platform, received):
        super().__init__(PlatformConfig(enabled=True, token="t"), platform)
        self._received = received

    async def connect(self, *, is_reconnect: bool = False):
        if hasattr(self, "_mark_connected"):
            self._mark_connected()
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="1")

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}

    async def handle_message(self, event):
        self._received.append((event.source.chat_id, event.text))


def _runner(tmp_path, received):
    runner = GatewayRunner(
        GatewayConfig(
            platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")},
            sessions_dir=tmp_path / "sessions",
        )
    )
    runner._create_adapter = lambda platform, cfg: _RecordingAdapter(platform, received)

    async def _no_secondary():
        return 0

    runner._start_secondary_profile_adapters = _no_secondary
    return runner


def _event(chat_id, text):
    src = SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm", user_id="u" + chat_id)
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=src)


async def _restart_and_boot(tmp_path, park):
    first = _runner(tmp_path, [])
    await asyncio.wait_for(first.start(), timeout=60)
    park(first.adapters[Platform.TELEGRAM])
    await asyncio.wait_for(first.stop(restart=True, service_restart=False), timeout=60)
    spooled = len(list(rf.spool_dir().glob("*.json")))
    flushed = len(list(_get_flush_dir().glob("*.json")))

    received: list = []
    boot = _runner(tmp_path, received)
    try:
        await asyncio.wait_for(boot.start(), timeout=60)
        for _ in range(100):
            if received and not list(rf.spool_dir().glob("*.json")):
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.2)  # a duplicate replay would land here
    finally:
        await asyncio.wait_for(boot.stop(), timeout=30)
    return spooled, flushed, received


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    (tmp_path / "logs").mkdir()
    return tmp_path


@pytest.mark.asyncio
async def test_pending_slot_followup_survives_real_stop_and_replays_once(home):
    def park(adapter):
        adapter._pending_messages["agent:main:telegram:dm:101"] = _event("101", "queued during restart")

    spooled, flushed, received = await _restart_and_boot(home, park)

    assert received == [("101", "queued during restart")], (spooled, flushed, received)
    assert spooled == 1
    assert flushed == 0, "follow-up went to the non-replayable flush file"
    assert list(rf.spool_dir().glob("*.json")) == []


@pytest.mark.asyncio
async def test_debounce_buffered_followup_survives_real_stop(home):
    """Sibling carrier: queue-mode busy text still in the debounce buffer."""

    def park(adapter):
        now = time.monotonic()
        adapter._text_debounce_store()["agent:main:telegram:dm:202"] = TextDebounceState(
            event=_event("202", "debounced burst"), task=None, first_ts=now, last_ts=now,
        )

    spooled, flushed, received = await _restart_and_boot(home, park)

    assert received == [("202", "debounced burst")], (spooled, flushed, received)
    assert flushed == 0
