"""Spooled restart follow-ups must survive the REAL GatewayRunner.start() path.

Argus r1 (t_e253d9d5): start() loaded the spool into ``_startup_restore_queue``
and then rebound that list to ``[]`` one statement later, so every follow-up
spooled while draining was deleted from disk AND dropped (lost=4/4). The unit
test called ``_load_restart_followups()`` directly and never saw it.

Oracle is independent of the production loader: the fake adapter's
``handle_message`` receipts plus the spool dir on disk.
"""

import asyncio

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.fork_ext import restart_followups as rf
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource


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


@pytest.mark.asyncio
async def test_spooled_followups_reach_handle_message_through_real_start(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    (tmp_path / "logs").mkdir()

    # previous life: 4 follow-ups spooled while draining (incident shape)
    expected = []
    for i in range(4):
        src = SessionSource(
            platform=Platform.TELEGRAM, chat_id=str(100 + i), chat_type="dm", user_id="u1"
        )
        assert rf.spool_followup(f"agent:main:telegram:dm:{100 + i}", f"follow-up {i}", src.to_dict())
        expected.append((str(100 + i), f"follow-up {i}"))

    # next life: real start()
    received: list = []
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

    try:
        await asyncio.wait_for(runner.start(), timeout=60)
        for _ in range(100):
            if len(received) >= len(expected):
                break
            await asyncio.sleep(0.1)
    finally:
        try:
            await asyncio.wait_for(runner.stop(), timeout=30)
        except Exception:
            pass

    assert received == expected, f"delivered {received!r}, spool now {list(rf.spool_dir().glob('*'))!r}"
