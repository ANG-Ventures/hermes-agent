"""A queued follow-up is released at turn end whether or not the final stream was confirmed.

Regression for t_138865b4. On Discord (``display.platforms.discord.streaming: false``) the stream
consumer is interim-only, so it never confirms the final and every queued follow-up logged
"final stream delivery not confirmed", which read as a delivery failure holding the user's input.
It never held it: the queued lane sends the first response and then runs the follow-up, even when
that send is refused or raises. These tests pin that release, and they pin that the "not confirmed"
line is kept for a consumer that really streamed the final.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform

SK = "agent:main:discord:thread:1564352776253997189:1564352776253997189"


def _consumer(*, stream_deltas_enabled):
    return SimpleNamespace(final_response_sent=False, message_id=None,
                           stream_deltas_enabled=stream_deltas_enabled,
                           has_durably_delivered_text=lambda _text: False)


def _setup(*, deliver, consumer):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._MAX_INTERRUPT_DEPTH = 8
    runner._run_agent = AsyncMock(return_value={"final_response": "follow-up answer", "messages": []})
    runner._is_goal_continuation_event = MagicMock(return_value=False)
    runner._session_key_for_source = MagicMock(return_value=SK)
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value="ace follow-up")
    runner._reply_anchor_for_event = MagicMock(return_value=None)
    runner._delivery_adapter_for = MagicMock(return_value=None)
    runner._intake_adapter_for = MagicMock(return_value=None)
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._pop_post_delivery_callback = MagicMock(return_value=None)
    runner._is_intentional_silence = MagicMock(return_value=False)
    runner._deliver_queued_first_response = deliver
    source = SimpleNamespace(platform=Platform.DISCORD, chat_id="1564352776253997189",
                             thread_id="1564352776253997189", chat_type="thread", parent_chat_id=None)
    from gateway.turn_context import TurnContext
    turn_ctx = TurnContext(
        source=source, session_id="sid", session_key=SK, run_generation=1, _interrupt_depth=0,
        history=[], _status_thread_metadata=None, context_prompt=None, result_holder=[None],
        mute_notification_reply=False, stream_consumer_holder=[consumer],
        persist_user_display_kind=None, reply_expected=None, event_message_id="1",
        inbound_message_id="1")
    pending_event = SimpleNamespace(source=source, message_id="2", channel_prompt=None,
                                    message_type=None, internal=False, metadata={}, reply_expected=None)
    return GatewayRunner, runner, turn_ctx, pending_event


def _run(GatewayRunner, runner, turn_ctx, pending_event):
    from gateway.queue_mgmt import mark_inflight
    mark_inflight(runner, turn_ctx.session_key, pending_event)   # as the drain does after its pop
    asyncio.run(GatewayRunner._run_agent_queued_followup(
        runner, turn_ctx, adapter=MagicMock(), pending="ace follow-up", pending_event=pending_event,
        response={"final_response": "first answer"}, result={"messages": []}, stream_task=None))


@pytest.mark.parametrize("deliver", [
    AsyncMock(return_value=True),                          # sent
    AsyncMock(return_value=False),                         # refused (flood control, dead transport)
    AsyncMock(side_effect=RuntimeError("discord 503")),    # raised
], ids=["sent", "refused", "raised"])
@pytest.mark.parametrize("streamed", [True, False], ids=["streaming", "interim_only"])
def test_unconfirmed_final_never_holds_the_followup(deliver, streamed):
    GatewayRunner, runner, turn_ctx, pending_event = _setup(
        deliver=deliver, consumer=_consumer(stream_deltas_enabled=streamed))

    _run(GatewayRunner, runner, turn_ctx, pending_event)

    runner._run_agent.assert_awaited_once()
    assert runner._run_agent.await_args.kwargs["inbound_message_id"] == "2"


@pytest.mark.parametrize("consumer, unconfirmed", [
    (_consumer(stream_deltas_enabled=True), True),
    (_consumer(stream_deltas_enabled=False), False),
    (None, False),
], ids=["streamed_final", "interim_only", "no_consumer"])
def test_not_confirmed_is_logged_only_when_the_final_was_streamed(consumer, unconfirmed, caplog):
    GatewayRunner, runner, turn_ctx, pending_event = _setup(
        deliver=AsyncMock(return_value=True), consumer=consumer)

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        _run(GatewayRunner, runner, turn_ctx, pending_event)

    assert ("final stream delivery not confirmed" in caplog.text) is unconfirmed
    runner._deliver_queued_first_response.assert_awaited_once()
