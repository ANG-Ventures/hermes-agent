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

# Buffers that could still resolve to a marker → held back while streaming.
PARTIAL_POSITIVE = [
    "N",
    "NO",
    "NO_",
    "NO_REP",
    "NO_REPLY",      # exact marker, not yet terminated by stream-end
    "NO REPLY",
    "no reply",      # canonicalized (case/space-insensitive)
    "  no_reply  ",  # surrounding whitespace stripped
    "[",
    "[SIL",
    "[SILENT]",
    "SILENT",
    "sil",
]

# Buffers that have already diverged from every marker → stream normally.
PARTIAL_NEGATIVE = [
    "",
    "   ",
    "No reply needed — here is the plan",   # diverged past the marker
    "NO_REPLYING",                           # superset, not a prefix
    "Nope",
    "Hello there",
    "The NO_REPLY token means silence",      # marker mentioned mid-prose
    "x" * 65,                                # over the 64-char cap
    "silence is golden",                     # 'SILENCE...' is not a marker prefix
]


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
