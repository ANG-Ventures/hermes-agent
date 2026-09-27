"""The "possible duplicate send" diagnostic must only fire when a duplicate is possible.

Live 2026-09-24..25: 707 warnings in gateway.log, every one ``streamed=False
previewed=False content_delivered=False``, 701 on Discord where
``display.platforms.discord.streaming: false`` makes the consumer interim-only.
0 same-chat final sends within 2 s across 720 Discord sends. An interim-only
consumer never sets ``already_sent`` (commentary is excluded, #10454), so the
normal final send is the only copy of the reply — not a duplicate risk.

These drive the REAL ``GatewayStreamConsumer`` (only the adapter is faked).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.run import _final_send_duplicate_risk
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


def _adapter():
    adapter = MagicMock()
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="m1"))
    adapter.edit_message = AsyncMock(return_value=SimpleNamespace(success=True))
    adapter.MAX_MESSAGE_LENGTH = 4096
    return adapter


def test_no_consumer_is_no_risk():
    assert _final_send_duplicate_risk(None) is False


@pytest.mark.asyncio
async def test_interim_only_consumer_is_not_a_duplicate_risk():
    """Streaming off: the consumer only posts commentary. The final text has not
    been sent by anyone, so the normal send is the single copy."""
    adapter = _adapter()
    consumer = GatewayStreamConsumer(adapter, "chat", StreamConsumerConfig())
    assert await consumer._send_commentary("Checking the logs now.") is True
    assert adapter.send.await_count == 1  # commentary really went out
    assert consumer.already_sent is False
    assert consumer.final_content_delivered is False
    assert _final_send_duplicate_risk(consumer) is False


@pytest.mark.asyncio
async def test_consumer_that_streamed_response_text_is_a_duplicate_risk():
    """The WeCom shape: the consumer put response text on the wire but its
    final delivery was never confirmed — a normal send can double it."""
    adapter = _adapter()
    consumer = GatewayStreamConsumer(
        adapter, "chat", StreamConsumerConfig(edit_interval=0.01, buffer_threshold=5)
    )
    consumer.on_delta("Hello world, streamed reply")
    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.08)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert consumer.already_sent is True
    assert _final_send_duplicate_risk(consumer) is True
