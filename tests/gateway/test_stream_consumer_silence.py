"""Streaming intentional-silence suppression.

When the agent chooses not to reply it emits a bare control marker
(``NO_REPLY`` / ``[SILENT]`` / …).  The gateway's whole-response filter
(``gateway/response_filters.is_intentional_silence_agent_result``) suppresses
this on the non-streaming delivery path, but the *streaming* path
(``GatewayStreamConsumer``) previously had no silence awareness: it edited the
raw marker onto the screen delta-by-delta and finalized it *before* the
whole-response filter could run.  On any streaming-capable adapter (Slack,
Telegram, Discord, …) users saw a literal ``NO_REPLY`` bubble.

These tests pin the two halves of the fix:

* ``is_partial_silence_marker`` — the mid-stream hold-back predicate.
* ``GatewayStreamConsumer`` — an exact-marker final buffer is suppressed and
  any already-shown preview is retracted, while substantive prose that merely
  mentions a marker is delivered normally.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.response_filters import (
    is_intentional_silence_response,
    is_partial_silence_marker,
)
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


# --------------------------------------------------------------------------
# is_partial_silence_marker — mid-stream hold-back predicate
# --------------------------------------------------------------------------

def test_partial_predicate_agrees_with_exact_on_full_markers():
    """Every exact silence marker is also a (trivial) partial of itself."""
    from gateway.response_filters import LIVE_GATEWAY_SILENT_MARKERS

    for marker in LIVE_GATEWAY_SILENT_MARKERS:
        assert is_partial_silence_marker(marker) is True
        assert is_intentional_silence_response(marker) is True


# --------------------------------------------------------------------------
# GatewayStreamConsumer — end-to-end suppression through run()
# --------------------------------------------------------------------------

def _make_adapter(*, supports_delete: bool = True) -> MagicMock:
    """Minimal MagicMock adapter wired for send/edit/delete."""
    adapter = MagicMock()
    adapter.REQUIRES_EDIT_FINALIZE = False
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(return_value=SimpleNamespace(
        success=True, message_id="preview_1",
    ))
    adapter.edit_message = AsyncMock(return_value=SimpleNamespace(
        success=True, message_id="preview_1",
    ))
    if supports_delete:
        adapter.delete_message = AsyncMock(return_value=True)
    else:
        del adapter.delete_message  # type: ignore[attr-defined]
    return adapter


def _sent_and_edited(adapter):
    texts = []
    for call in adapter.send.call_args_list:
        texts.append(call.kwargs.get("content", ""))
    if getattr(adapter, "edit_message", None) is not None:
        for call in adapter.edit_message.call_args_list:
            texts.append(call.kwargs.get("content", ""))
    return texts


@pytest.mark.asyncio
async def test_human_fold_releases_internal_commentary_before_final():
    from gateway.run_busy import GatewayBusySessionMixin
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource
    from gateway.turn_context import TurnContext

    runner = GatewayBusySessionMixin()
    turn = SimpleNamespace(agent=None, event=None, ctx=None)
    runner._session_state = lambda key: SimpleNamespace(turn=turn)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="test")
    agent = object()
    turn.agent = agent
    turn.event = MessageEvent(text="wake", source=source, internal=True)
    adapter = _make_adapter()
    consumer = GatewayStreamConsumer(adapter, "test", StreamConsumerConfig(internal_event=True))
    ctx = TurnContext(persist_user_display_kind="internal_notification",
                      interim_assistant_messages_enabled=True, _run_still_current=lambda: True)
    ctx.stream_consumer_holder[0] = consumer
    turn.ctx = ctx
    consumer.on_commentary("old commentary")
    consumer._drain_queue()
    assert consumer._internal_held == ["old commentary"]
    human = MessageEvent(text="please answer", source=source, reply_expected=True)
    await asyncio.to_thread(runner._fold_into_running_turn, agent, "test", human)
    await asyncio.to_thread(runner._fold_into_running_turn, agent, "test", human)
    consumer.on_commentary("new commentary")
    delivered = asyncio.Event()
    original_send = adapter.send.side_effect
    async def send(**kwargs):
        if kwargs.get("content") == "new commentary":
            delivered.set()
        return SimpleNamespace(success=True, message_id="sent")
    adapter.send.side_effect = send
    task = asyncio.create_task(consumer.run())
    try:
        await asyncio.wait_for(delivered.wait(), 2)
        assert _sent_and_edited(adapter) == ["old commentary", "new commentary"]
        assert ctx.persist_user_display_kind == "internal_notification"
        assert ctx.effective_display_kind is None
    finally:
        task.cancel()
        await task
        adapter.send.side_effect = original_send


@pytest.mark.asyncio
@pytest.mark.parametrize("depth_cap", [False, True])
async def test_receiving_turn_policy_survives_queued_return(monkeypatch, tmp_path, depth_cap):
    from tests.gateway.test_queued_followup_turn_clock_and_leftover_steer import _drive, SESSION_KEY, _source
    from gateway.platforms.event import MessageEvent
    from gateway.turn_context import TurnContext

    contexts = []
    runner_ref = []
    previous = TurnContext(persist_user_display_kind="internal_notification")

    def prepare(runner):
        runner_ref.append(runner)
        runner._MAX_INTERRUPT_DEPTH = 0 if depth_cap else 5
        runner._session_state(SESSION_KEY).turn.event = MessageEvent(
            text="opening", source=_source(), reply_expected=False, internal=True)
        build = runner._run_agent_build_turn_context

        def build_turn(*args, **kwargs):
            # Reused agent identity can still be published while its old ctx is present.
            runner._session_state(SESSION_KEY).turn.ctx = previous
            kwargs["persist_user_display_kind"] = "internal_notification"
            kwargs["reply_expected"] = True
            ctx, turn_runner, adapter = build(*args, **kwargs)
            contexts.append(ctx)
            return ctx, turn_runner, adapter

        runner._run_agent_build_turn_context = build_turn

    def script(idx, queue_followup):
        runner = runner_ref[0]
        ctx = contexts[-1]
        # Do not wait for the 50ms tracker: steer can arrive before its next tick.
        agent = ctx.agent_holder[0]
        runner._session_state(SESSION_KEY).turn.agent = agent
        runner._fold_into_running_turn(agent, SESSION_KEY, MessageEvent(
            text="human steer", source=_source(), reply_expected=False))
        if idx == 0:
            queue_followup("queued")
        return {"final_response": "NO_REPLY", "messages": [], "api_calls": 1}

    _, _, _, result = await _drive(monkeypatch, tmp_path, script, prepare=prepare)
    assert not previous.human_engaged, "steer promoted the previous cached-agent context"
    assert all(ctx.human_engaged for ctx in contexts)
    assert all(ctx.reply_expected is True for ctx in contexts), "opening event overwrote receiving obligation"
    assert all(ctx.persist_user_display_kind == "internal_notification" for ctx in contexts)
    prefix = "effective" if depth_cap else "queued_terminal"
    assert result[prefix + "_display_kind"] is None
    assert result[prefix + "_reply_expected"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["depth_cap", "discarded", "recursive"])
async def test_cleanup_steer_updates_only_returned_owner_policy(monkeypatch, tmp_path, path):
    from tests.gateway.test_queued_followup_turn_clock_and_leftover_steer import _drive, SESSION_KEY, _source
    from gateway.platforms.event import MessageEvent

    contexts = []
    runner_ref = []

    def prepare(runner):
        runner_ref.append(runner)
        runner._MAX_INTERRUPT_DEPTH = 0 if path == "depth_cap" else 5
        build = runner._run_agent_build_turn_context
        cleanup = runner._run_agent_cleanup_turn_tasks

        def build_turn(*args, **kwargs):
            kwargs["persist_user_display_kind"] = "internal_notification"
            kwargs["reply_expected"] = False
            ctx, worker, adapter = build(*args, **kwargs)
            contexts.append(ctx)
            return ctx, worker, adapter

        async def cleanup_turn(ctx, **kwargs):
            # Accepted human steer during an awaited cleanup, after return selection.
            await asyncio.sleep(0)
            if ctx is contexts[0]:
                agent = ctx.agent_holder[0]
                runner._session_state(SESSION_KEY).turn.agent = agent
                runner._fold_into_running_turn(agent, SESSION_KEY, MessageEvent(
                    text="late human steer", source=_source(), reply_expected=True))
            await cleanup(ctx, **kwargs)

        runner._run_agent_build_turn_context = build_turn
        runner._run_agent_cleanup_turn_tasks = cleanup_turn
        if path == "discarded":
            async def discard(**kwargs):
                return None
            runner._prepare_profile_scoped_inbound_message_text = discard

    def script(idx, queue_followup):
        if idx == 0:
            if path == "recursive":
                # Parent is human-engaged; the terminal internal child must stay quiet.
                runner = runner_ref[0]
                agent = contexts[0].agent_holder[0]
                runner._session_state(SESSION_KEY).turn.agent = agent
                runner._fold_into_running_turn(agent, SESSION_KEY, MessageEvent(
                    text="parent human steer", source=_source(), reply_expected=True))
            queue_followup("queued")
        return {"final_response": "NO_REPLY", "messages": [], "api_calls": 1}

    _, _, calls, result = await _drive(monkeypatch, tmp_path, script, prepare=prepare)
    assert all(ctx.persist_user_display_kind == "internal_notification" for ctx in contexts)
    if path == "recursive":
        assert len(calls) == 2
        assert result["queued_terminal_display_kind"] == "internal_notification"
        assert result["queued_terminal_reply_expected"] is False
        assert result["effective_reply_expected"] is False
    else:
        assert len(calls) == 1
        assert contexts[0].human_engaged
        assert result["effective_display_kind"] is None
        assert result["effective_reply_expected"] is True


@pytest.mark.parametrize("case", ["internal", "heartbeat", "stale", "wrong_agent", "muted", "scheduled", "display_off"])
def test_fold_promotion_guards(case):
    from gateway.run_busy import GatewayBusySessionMixin
    from gateway.platforms.event import MessageEvent
    from gateway.turn_context import TurnContext
    from gateway.config import Platform
    from gateway.session import SessionSource

    runner = GatewayBusySessionMixin()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="test")
    agent = object()
    ctx = TurnContext(persist_user_display_kind="internal_notification",
                      _run_still_current=lambda: case != "stale",
                      mute_notification_reply=case == "muted",
                      scheduled_heartbeat=case == "scheduled")
    consumer = GatewayStreamConsumer(_make_adapter(), "test", StreamConsumerConfig(internal_event=True),
                                     run_still_current=ctx._run_still_current)
    if case != "display_off":
        ctx.stream_consumer_holder[0] = consumer
    opening = MessageEvent(text="wake", source=source, internal=True)
    turn = SimpleNamespace(agent=agent, ctx=ctx, event=opening)
    runner._session_state = lambda key: SimpleNamespace(turn=turn)
    event = MessageEvent(text="request", source=source, internal=case == "internal", reply_expected=True)
    if case == "heartbeat":
        event._heartbeat_session_id = "heartbeat"
    runner._fold_into_running_turn(object() if case == "wrong_agent" else agent, "test", event)
    assert ctx.human_engaged == (case in {"muted", "scheduled", "display_off"})
    assert consumer._queue.empty()
    assert opening.internal is True
    assert ctx.persist_user_display_kind == "internal_notification"


@pytest.mark.asyncio
async def test_promoted_reply_cannot_silently_disappear():
    from gateway.run_turn import GatewayTurnMixin
    from gateway.config import Platform

    runner = GatewayTurnMixin()
    runner.async_session_store = SimpleNamespace(clear_resume_pending=AsyncMock())
    runner._clear_restart_failure_count = AsyncMock()
    result = {"final_response": "NO_REPLY", "effective_display_kind": None, "messages": []}
    response, silent, _ = await runner._hmwa_shape_agent_response(
        result, SimpleNamespace(chat_id="test", platform=Platform.TELEGRAM), [],
        SimpleNamespace(session_id="test"), None, None, 0, "test", "telegram", 0,
        persist_user_display_kind="internal_notification", reply_expected=True)
    assert not silent
    assert response and response != "NO_REPLY"


@pytest.mark.parametrize("quiet", ["off", "mute", "heartbeat", "enabled"])
def test_promoted_stream_setup_preserves_display_switches(quiet):
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext
    from gateway.config import StreamingConfig

    adapter = _make_adapter()
    ctx = TurnContext(
        human_engaged=True, persist_user_display_kind="internal_notification",
        source=SimpleNamespace(chat_id="test"), user_config={},
        resolve_display_setting=lambda *args: False,
        interim_assistant_messages_enabled=quiet != "off",
        mute_notification_reply=quiet == "mute", scheduled_heartbeat=quiet == "heartbeat",
        _run_still_current=lambda: True)
    runner = SimpleNamespace(
        config=SimpleNamespace(streaming=StreamingConfig()),
        _delivery_adapter_for=lambda source: adapter,
        _build_stream_consumer_config=lambda *a, **k: (StreamConsumerConfig(), None))
    consumer, _, _, enabled = TurnRunner(runner, ctx)._setup_stream_consumer("telegram")
    if quiet == "enabled":
        assert enabled and consumer is not None
        assert consumer._drain_queue().got_human_engagement
    else:
        assert consumer is None and not enabled


@pytest.mark.asyncio
async def test_queued_promotion_is_abandoned_after_generation_dies():
    current = True
    adapter = _make_adapter()
    consumer = GatewayStreamConsumer(adapter, "test", StreamConsumerConfig(internal_event=True),
                                     run_still_current=lambda: current)
    consumer.on_commentary("never deliver")
    consumer._drain_queue()
    consumer.on_human_engagement()
    current = False
    await consumer.run()
    assert not adapter.send.called
    assert consumer.cfg.internal_event


class TestStreamedSilenceSuppression:
    @pytest.mark.asyncio
    async def test_no_reply_only_stream_is_fully_suppressed(self):
        """A stream whose entire content is NO_REPLY sends nothing visible."""
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_1",
            StreamConsumerConfig(edit_interval=0.01, buffer_threshold=1),
        )
        consumer.on_delta("NO_REPLY")
        consumer.finish()
        await consumer.run()

        # No marker text ever reached the platform.
        for text in _sent_and_edited(adapter):
            assert "NO_REPLY" not in text, f"marker leaked: {text!r}"

        # Delivery flags stay False so the gateway does not treat the marker
        # as a delivered reply (its whole-response filter then drops it too).
        assert consumer.final_response_sent is False
        assert consumer.final_content_delivered is False
        assert consumer.already_sent is False

    @pytest.mark.asyncio
    async def test_partial_marker_preview_is_retracted(self):
        """A marker flushed mid-stream as a preview is deleted on completion."""
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_1",
            StreamConsumerConfig(edit_interval=0.01, buffer_threshold=1),
        )
        # Force a mid-stream preview: pretend "NO_REPLY" was already put on
        # screen (the pre-fix behaviour) before got_done runs.
        consumer._message_id = "preview_1"
        consumer._preview_message_ids = {"preview_1"}
        consumer._already_sent = True

        consumer.on_delta("NO_REPLY")
        consumer.finish()
        await consumer.run()

        # The stale preview was best-effort deleted.
        adapter.delete_message.assert_awaited_once_with("chat_1", "preview_1")
        assert consumer.final_content_delivered is False
        assert consumer.already_sent is False




class TestInternalEventStreamSilence:
    """Internal-event turns (bg-process completions) use the autonomous rule."""

    NOTE = "Already merged on that result. No new information.\n\nNO_REPLY"

    @pytest.mark.asyncio
    async def test_internal_note_plus_marker_never_reaches_platform(self):
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_1",
            StreamConsumerConfig(
                edit_interval=0.0, buffer_threshold=1, internal_event=True,
            ),
        )
        # Feed deltas while run() is live so interval ticks actually fire:
        # without the internal-event hold the note would be edited on screen.
        task = asyncio.create_task(consumer.run())
        for chunk in ("Already merged on that result. ", "No new information.",
                      "\n\n", "NO_REPLY"):
            consumer.on_delta(chunk)
            await asyncio.sleep(0.1)
        consumer.finish()
        await asyncio.wait_for(task, timeout=5)

        assert _sent_and_edited(adapter) == []
        assert consumer.final_content_delivered is False
        assert consumer.already_sent is False

    @pytest.mark.asyncio
    async def test_human_turn_note_plus_marker_is_delivered(self):
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_1",
            StreamConsumerConfig(edit_interval=0.01, buffer_threshold=1),
        )
        consumer.on_delta(self.NOTE)
        consumer.finish()
        await consumer.run()

        assert any("No new information" in t for t in _sent_and_edited(adapter))

    @pytest.mark.asyncio
    async def test_internal_real_report_mentioning_marker_is_delivered(self):
        adapter = _make_adapter()
        text = "CI failed: the NO_REPLY filter test regressed, see run 123."
        consumer = GatewayStreamConsumer(
            adapter, "chat_1",
            StreamConsumerConfig(
                edit_interval=0.01, buffer_threshold=1, internal_event=True,
            ),
        )
        consumer.on_delta(text)
        consumer.finish()
        await consumer.run()

        assert any("run 123" in t for t in _sent_and_edited(adapter))


class TestInternalEventHoldAcrossBoundaries:
    """The internal-event hold spans segment breaks and commentary (t_4f0e196a).

    Prism P1 on #1514: the hold required ``not got_segment_break`` and
    ``commentary_text is None``, so a preamble before a tool boundary (or an
    interim commentary) was delivered even when the turn ended in NO_REPLY.
    """

    @staticmethod
    def _consumer(adapter):
        return GatewayStreamConsumer(
            adapter, "chat_1",
            StreamConsumerConfig(
                edit_interval=0.0, buffer_threshold=1, internal_event=True,
            ),
        )

    @pytest.mark.asyncio
    async def test_segment_break_before_marker_never_reaches_platform(self):
        adapter = _make_adapter()
        consumer = self._consumer(adapter)
        task = asyncio.create_task(consumer.run())
        consumer.on_delta("Checking the build log first.")
        await asyncio.sleep(0.1)
        consumer.on_segment_break()  # tool call boundary
        await asyncio.sleep(0.1)
        consumer.on_delta("NO_REPLY")
        await asyncio.sleep(0.1)
        consumer.finish("NO_REPLY")
        await asyncio.wait_for(task, timeout=5)

        assert _sent_and_edited(adapter) == []
        assert consumer.final_content_delivered is False
        assert consumer.already_sent is False

    @pytest.mark.asyncio
    async def test_commentary_before_marker_never_reaches_platform(self):
        adapter = _make_adapter()
        consumer = self._consumer(adapter)
        task = asyncio.create_task(consumer.run())
        consumer.on_commentary("Let me check whether this is new.")
        await asyncio.sleep(0.1)
        consumer.finish("NO_REPLY")
        await asyncio.wait_for(task, timeout=5)

        assert _sent_and_edited(adapter) == []

    @pytest.mark.asyncio
    async def test_held_preamble_is_delivered_when_final_is_not_silent(self):
        adapter = _make_adapter()
        consumer = self._consumer(adapter)
        task = asyncio.create_task(consumer.run())
        consumer.on_delta("Checking the build log first.")
        await asyncio.sleep(0.1)
        consumer.on_segment_break()
        consumer.on_commentary("Log fetched.")
        await asyncio.sleep(0.1)
        consumer.on_delta("CI is red: test_foo failed in run 123.")
        await asyncio.sleep(0.1)
        consumer.finish("CI is red: test_foo failed in run 123.")
        await asyncio.wait_for(task, timeout=5)

        texts = _sent_and_edited(adapter)
        joined = "\n".join(texts)
        assert "Checking the build log first." in joined
        assert "Log fetched." in joined
        assert "run 123" in joined
        # Held interim content lands before the final answer.
        first_final = next(i for i, t in enumerate(texts) if "run 123" in t)
        assert any("Checking the build log" in t for t in texts[:first_final])
        assert consumer.has_delivered_text("Checking the build log first.")

    @pytest.mark.asyncio
    async def test_flush_barrier_releases_held_content_in_order(self):
        adapter = _make_adapter()
        consumer = self._consumer(adapter)
        task = asyncio.create_task(consumer.run())
        consumer.on_delta("Preamble before the tool.")
        consumer.on_segment_break()
        consumer.on_delta("Question context.")
        flushed = await asyncio.to_thread(consumer.flush_pending_sync, 5.0)
        consumer.finish("Which branch?")
        await asyncio.wait_for(task, timeout=5)

        assert flushed is True
        sends = [c.kwargs.get("content", "") for c in adapter.send.call_args_list]
        pre = next(i for i, t in enumerate(sends) if "Preamble" in t)
        ctx = next(i for i, t in enumerate(sends) if "Question context" in t)
        assert pre < ctx


class TestHumanTurnTrailingMarkerStrip:
    """Human turn, exact-marker rule: the note streams, the trailing token does not.

    2026-10-03: a kanban lifecycle line pasted into a Telegram DM drew a
    "note + NO_REPLY" reply.  Not internal, so delivery is right — but the
    literal token was edited onto the screen.  The turn-final text drops a
    marker alone on its final line (``strip_trailing_silence_marker`` config,
    set for non-raw surfaces); the silence predicates keep reading the raw
    buffer so an exact / autonomous marker is still suppressed.
    """

    NOTE = "Routine closer digest, nothing for you.\n\nNO_REPLY"

    def test_clean_for_display_is_media_only(self):
        # Per-chunk display cleaning never removes text: the trailing-marker
        # strip is a turn-final decision (Prism f2a2a5636e1e).
        assert GatewayStreamConsumer._clean_for_display(self.NOTE) == self.NOTE

    @pytest.mark.asyncio
    async def test_human_note_plus_marker_streams_without_token(self):
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_1", _strip_cfg(),
        )
        consumer.on_delta(self.NOTE)
        consumer.finish()
        await consumer.run()

        texts = _sent_and_edited(adapter)
        assert any("nothing for you" in t for t in texts)
        for text in texts:
            assert "NO_REPLY" not in text, f"marker leaked: {text!r}"
        assert consumer.final_content_delivered is True

    @pytest.mark.asyncio
    async def test_human_note_plus_marker_authoritative_final_reconciles(self):
        """The gateway's delivered_final_matches sees the same stripped text."""
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_1", _strip_cfg(),
        )
        consumer.on_delta(self.NOTE)
        consumer.finish(self.NOTE)
        await consumer.run()

        assert consumer.delivered_final_matches(self.NOTE) is True
        for text in _sent_and_edited(adapter):
            assert "NO_REPLY" not in text

    @pytest.mark.asyncio
    async def test_internal_note_plus_marker_still_fully_suppressed(self):
        """Regression guard: the display strip must not defeat autonomous silence."""
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_1",
            StreamConsumerConfig(
                edit_interval=0.01, buffer_threshold=1, internal_event=True,
            ),
        )
        consumer.on_delta(self.NOTE)
        consumer.finish()
        await consumer.run()

        assert _sent_and_edited(adapter) == []
        assert consumer.final_content_delivered is False


class TestInternalEventLongReplyDelivered:
    """t_bb4059d2: on an internal turn the stream consumer uses the same predicate as the final
    send.  A long reply beside a marker streams (marker stripped) and is never retracted; a short
    note is held and never shown."""

    LONG = ("PR 4791 merged and the dispatcher picked up the new placement rule on the next tick. "
            "Two cards that were parked on local_full respawned on ci-box and both finished green "
            "within ten minutes.")

    @staticmethod
    def _cfg(words=12):
        return StreamConsumerConfig(
            edit_interval=0.0, buffer_threshold=1, internal_event=True,
            strip_trailing_silence_marker=True, internal_short_note_words=words,
        )

    async def _stream(self, adapter, cfg, chunks, final):
        consumer = GatewayStreamConsumer(adapter, "chat_1", cfg)
        task = asyncio.create_task(consumer.run())
        for chunk in chunks:
            consumer.on_delta(chunk)
            await asyncio.sleep(0.05)
        consumer.finish(final)
        await asyncio.wait_for(task, timeout=5)
        return consumer

    @pytest.mark.asyncio
    @pytest.mark.parametrize("marker_first", [False, True], ids=["marker-last", "marker-first"])
    async def test_long_reply_streams_without_marker_and_is_not_retracted(self, marker_first):
        adapter = _make_adapter()
        raw = ("NO_REPLY\n\n" + self.LONG) if marker_first else (self.LONG + "\n\nNO_REPLY")
        chunks = [raw[i:i + 40] for i in range(0, len(raw), 40)]
        consumer = await self._stream(adapter, self._cfg(), chunks, raw)

        texts = _sent_and_edited(adapter)
        assert texts and texts[-1].strip() == self.LONG
        for text in texts:
            assert "NO_REPLY" not in text, f"marker leaked: {text!r}"
        adapter.delete_message.assert_not_called()
        assert consumer.final_content_delivered is True
        assert consumer.delivered_final_matches(self.LONG) is True

    @pytest.mark.asyncio
    async def test_native_stream_marker_first_long_reply_drops_marker(self):
        """Prism 52ccd9c3b209 on #2060: a native-stream adapter opens the stream with an empty
        seed frame before any text, so the leading strip must key on visible text pushed, not on
        the stream being open."""
        from tests.gateway.test_stream_consumer_wecom_native import (
            _make_native_streaming_adapter,
        )
        adapter = _make_native_streaming_adapter()
        raw = "NO_REPLY\n\n" + self.LONG
        chunks = [raw[i:i + 40] for i in range(0, len(raw), 40)]
        consumer = await self._stream(adapter, self._cfg(), chunks, raw)

        frames = [f["text"] for f in adapter.frames]
        assert frames and frames[0] == ""  # the seed opened the stream first
        visible = [t for t in frames if t] + _sent_and_edited(adapter)
        assert visible, "long reply was not delivered"
        for text in visible:
            assert "NO_REPLY" not in text, f"marker leaked: {text!r}"
        assert visible[-1].strip() == self.LONG
        assert consumer.final_content_delivered is True
        assert consumer.delivered_final_matches(self.LONG) is True

    @pytest.mark.asyncio
    async def test_short_note_is_held_and_never_shown(self):
        adapter = _make_adapter()
        raw = "Already reported.\nNO_REPLY"
        consumer = await self._stream(adapter, self._cfg(), ["Already reported.", "\nNO_REPLY"], raw)

        assert _sent_and_edited(adapter) == []
        assert consumer.final_content_delivered is False

    @pytest.mark.asyncio
    async def test_zero_words_keeps_the_loose_rule(self):
        adapter = _make_adapter()
        raw = self.LONG + "\n\nNO_REPLY"
        consumer = await self._stream(adapter, self._cfg(0), [raw], raw)

        assert _sent_and_edited(adapter) == []
        assert consumer.final_content_delivered is False


def _strip_cfg(**kw):
    return StreamConsumerConfig(
        edit_interval=0.01, buffer_threshold=1,
        strip_trailing_silence_marker=True, **kw,
    )


class TestTrailingMarkerStripIsTurnFinalOnly:
    """Prism P1s f2a2a5636e1e / 0ed312b03e71 / c7fca8d2bc4c on #1668.

    The trailing-marker strip applies to the completed turn-final text only:
    a standalone token line that a platform split leaves at the END of a
    non-final chunk is real content and must be delivered.  Raw-text surfaces
    (``strip_trailing_silence_marker`` off) never have it removed, and prose
    like "No reply." is not a control token.
    """

    @pytest.mark.asyncio
    async def test_marker_line_at_non_final_chunk_boundary_is_delivered(self):
        adapter = _make_adapter()
        adapter.MAX_MESSAGE_LENGTH = 600  # safe limit 500
        consumer = GatewayStreamConsumer(adapter, "chat_1", _strip_cfg())
        # 480 chars + "NO_REPLY": the platform split ends chunk 1 on the
        # token line, chunk 2 carries the rest of the reply.
        head = ("a" * 79 + "\n") * 6
        body = head + "NO_REPLY\n" + ("b" * 79 + "\n") * 3 + "end"
        consumer.on_delta(body)
        consumer.finish()
        await consumer.run()

        sends = [c.kwargs.get("content", "") for c in adapter.send.call_args_list]
        assert len(sends) >= 2, sends  # the reply really was split
        assert sends[0].rstrip().endswith("NO_REPLY"), sends[0][-30:]
        delivered = "".join(_sent_and_edited(adapter))
        assert "NO_REPLY" in delivered, "mid-reply marker line was deleted"
        assert consumer.delivered_final_matches(body) is True

    @pytest.mark.asyncio
    async def test_trailing_marker_stripped_before_split_and_reconciles(self):
        """Completed turn longer than one message: the trailing token is
        removed from the WHOLE text before splitting, and the gateway's
        stripped response reconciles (no duplicate full re-send)."""
        from gateway.response_filters import strip_trailing_silence_marker

        adapter = _make_adapter()
        adapter.MAX_MESSAGE_LENGTH = 600  # safe limit 500
        consumer = GatewayStreamConsumer(adapter, "chat_1", _strip_cfg())
        body = ("a" * 79 + "\n") * 6 + "x" * 30 + "\nNO_REPLY"
        consumer.on_delta(body)
        consumer.finish()
        await consumer.run()

        sends = [c.kwargs.get("content", "") for c in adapter.send.call_args_list]
        assert len(sends) >= 2, sends
        delivered = "".join(_sent_and_edited(adapter))
        assert "NO_REPLY" not in delivered
        assert "x" * 30 in delivered
        assert consumer.delivered_final_matches(body) is True
        assert consumer.delivered_final_matches(
            strip_trailing_silence_marker(body)
        ) is True

    @pytest.mark.asyncio
    async def test_send_new_chunk_does_not_strip(self):
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(adapter, "chat_1", _strip_cfg())
        await consumer._send_new_chunk("prose\nNO_REPLY", None)
        assert adapter.send.call_args.kwargs["content"] == "prose\nNO_REPLY"

    @pytest.mark.asyncio
    async def test_raw_text_surface_streams_marker_verbatim(self):
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_1",
            StreamConsumerConfig(edit_interval=0.01, buffer_threshold=1),
        )
        consumer.on_delta("note\nNO_REPLY")
        consumer.finish("note\nNO_REPLY")
        await consumer.run()
        texts = _sent_and_edited(adapter)
        assert texts and texts[-1].rstrip().endswith("NO_REPLY")
        assert consumer.delivered_final_matches("note\nNO_REPLY") is True

    @pytest.mark.asyncio
    async def test_prose_last_line_no_reply_is_delivered(self):
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(adapter, "chat_1", _strip_cfg())
        text = "Checked Alice's thread for a response:\n\nNo reply."
        consumer.on_delta(text)
        consumer.finish(text)
        await consumer.run()
        assert _sent_and_edited(adapter)[-1].rstrip().endswith("No reply.")


class TestOverflowSplitKeepsNonterminalMarker:
    """A token line that ends a sealed overflow HEAD chunk is mid-response.

    Prism #1668 0ed312b03e71 (fixed by #1669; regression guard from
    t_21da8a38): the strip must apply to the completed reply, never to a
    non-final split chunk, or the sealed head loses a line the ledger keeps.
    """

    @staticmethod
    def _reply():
        # safe_limit = 700: head + token fits, the first tail line crosses it,
        # so the newline split lands right after ``NO_REPLY``.
        head = "\n".join(f"line {i:03d} " + "x" * 40 for i in range(12))
        tail = "\n".join(f"tail {i:03d} " + "y" * 100 for i in range(8))
        return head + "\nNO_REPLY\n" + tail

    @staticmethod
    def _consumer():
        adapter = _make_adapter()
        adapter.MAX_MESSAGE_LENGTH = 800
        ids = iter(f"m{i}" for i in range(100))
        adapter.send = AsyncMock(side_effect=lambda **kw: SimpleNamespace(
            success=True, message_id=next(ids),
        ))
        consumer = GatewayStreamConsumer(
            adapter, "chat_1",
            StreamConsumerConfig(
                edit_interval=0.0, buffer_threshold=1, cursor="",
                strip_trailing_silence_marker=True,
            ),
        )
        return adapter, consumer

    @pytest.mark.asyncio
    async def test_existing_preview_split_keeps_marker_line(self):
        reply = self._reply()
        split_head = reply[:reply.index("\nNO_REPLY\n") + len("\nNO_REPLY")]
        assert len(split_head) < 700 < len(split_head) + 110
        adapter, consumer = self._consumer()
        consumer._message_id = "m_prev"
        consumer._last_sent_text = "line 000"
        consumer._already_sent = True
        consumer.on_delta(reply)
        consumer.finish()
        await consumer.run()

        texts = _sent_and_edited(adapter)
        assert sum("NO_REPLY" in t for t in texts) == 1, texts
        assert consumer.delivered_final_matches(reply) is True

    @pytest.mark.asyncio
    async def test_first_send_split_keeps_marker_line(self):
        reply = self._reply()
        adapter, consumer = self._consumer()
        consumer.on_delta(reply)
        consumer.finish()
        await consumer.run()

        assert any("NO_REPLY" in t for t in _sent_and_edited(adapter))
        assert consumer.delivered_final_matches(reply) is True

    @pytest.mark.asyncio
    async def test_repeated_trailing_tokens_stream_without_token(self):
        """Turn-final strip removes every trailing token line (c7fca8d2bc4c)."""
        adapter, consumer = self._consumer()
        reply = "short note\nNO_REPLY\nNO_REPLY"
        consumer.on_delta(reply)
        consumer.finish()
        await consumer.run()

        texts = _sent_and_edited(adapter)
        assert any("short note" in t for t in texts)
        for text in texts:
            assert "NO_REPLY" not in text, f"marker leaked: {text!r}"
        assert consumer.delivered_final_matches(reply) is True
