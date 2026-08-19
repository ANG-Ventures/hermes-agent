"""Regression tests: split surrogates in Anthropic tool-input JSON deltas.

Incident 2026-08-18 (recurrence of the 2026-08-09 fault-4 class): the text
and thinking delta lanes were repaired by ``_SurrogateSplicer`` (PR #535),
but tool-call argument JSON travels on a third lane —
``input_json_delta.partial_json`` — that no splicer covered.  The Anthropic
SDK's ``accumulate_event`` executes ``bytes(partial_json, "utf-8")`` inside
``MessageStream.__stream__`` *before* the harness event loop sees the event,
so a relay-split emoji in tool arguments raised ``UnicodeEncodeError`` deep
inside the SDK iterator, killed the stream after partial delivery, salvaged
0 chars, and announced a dishonest "connection issue" fallback.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.anthropic_stream_repair import (
    _ToolJsonRepairingRawStream,
    install_tool_json_surrogate_repair,
)
from agent.errors import ProviderStreamParseError


def _delta_event(fragment: str, index: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        type="content_block_delta",
        index=index,
        delta=SimpleNamespace(type="input_json_delta", partial_json=fragment),
    )


def _stop_event(index: int = 0) -> SimpleNamespace:
    return SimpleNamespace(type="content_block_stop", index=index)


class TestToolJsonRepairingRawStream:
    def test_recombines_pair_split_across_fragments(self):
        events = [
            _delta_event('{"message": "done \ud83d'),
            _delta_event('\ude00 ok"}'),
            _stop_event(),
        ]
        wrapped = _ToolJsonRepairingRawStream(iter(events))
        out = list(wrapped)

        joined = out[0].delta.partial_json + out[1].delta.partial_json
        joined.encode("utf-8")  # must not raise
        assert json.loads(joined) == {"message": "done 😀 ok"}

    def test_every_fragment_is_utf8_encodable(self):
        events = [
            _delta_event('{"a": "x\ud83d'),
            _delta_event('\ude00y", "b": "\ud83e'),
            _delta_event('\uddd0"}'),
            _stop_event(),
        ]
        for event in _ToolJsonRepairingRawStream(iter(events)):
            fragment = getattr(getattr(event, "delta", None), "partial_json", None)
            if fragment is not None:
                fragment.encode("utf-8")

    def test_orphan_surrogate_floored_to_replacement_char(self):
        events = [_delta_event('{"a": "bad \udd34 x"}'), _stop_event()]
        out = list(_ToolJsonRepairingRawStream(iter(events)))

        fragment = out[0].delta.partial_json
        fragment.encode("utf-8")
        assert json.loads(fragment) == {"a": "bad \ufffd x"}

    def test_per_block_index_isolation(self):
        # A high surrogate ending block 0 must NOT pair with a low
        # surrogate starting block 1.
        events = [
            _delta_event('{"a": "x\ud83d', index=0),
            _delta_event('{"b": "\ude00y"}', index=1),
        ]
        out = list(_ToolJsonRepairingRawStream(iter(events)))

        # Block 0's high surrogate is held pending (not emitted).
        assert out[0].delta.partial_json == '{"a": "x'
        # Block 1's leading low surrogate is an orphan in ITS stream.
        assert out[1].delta.partial_json == '{"b": "\ufffdy"}'
        for event in out:
            event.delta.partial_json.encode("utf-8")

    def test_text_deltas_pass_through_untouched(self):
        text_event = SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="text_delta", text="raw \ud83d"),
        )
        out = list(_ToolJsonRepairingRawStream(iter([text_event])))
        # The text lane has its own stateful splicer downstream; this
        # repair must leave it alone so genuinely-split pairs can rejoin.
        assert out[0].delta.text == "raw \ud83d"

    def test_repair_failure_is_fail_open(self):
        class ExplodingDelta:
            type = "input_json_delta"

            @property
            def partial_json(self):
                raise RuntimeError("shape drift")

        event = SimpleNamespace(
            type="content_block_delta", index=0, delta=ExplodingDelta()
        )
        out = list(_ToolJsonRepairingRawStream(iter([event])))
        assert out == [event]

    def test_delegates_attributes_and_close(self):
        closed = {"yes": False}

        class Inner:
            response = "resp-sentinel"

            def close(self):
                closed["yes"] = True

            def __iter__(self):
                return iter(())

        wrapped = _ToolJsonRepairingRawStream(Inner())
        assert wrapped.response == "resp-sentinel"
        wrapped.close()
        assert closed["yes"]


class TestInstallOnMessageStream:
    def test_installs_on_sdk_shaped_stream(self):
        stream = SimpleNamespace(_raw_stream=iter(()))
        assert install_tool_json_surrogate_repair(stream)
        assert isinstance(stream._raw_stream, _ToolJsonRepairingRawStream)

    def test_idempotent(self):
        stream = SimpleNamespace(_raw_stream=iter(()))
        assert install_tool_json_surrogate_repair(stream)
        first = stream._raw_stream
        assert not install_tool_json_surrogate_repair(stream)
        assert stream._raw_stream is first

    def test_unrecognized_shape_is_fail_open(self):
        assert not install_tool_json_surrogate_repair(SimpleNamespace())
        assert not install_tool_json_surrogate_repair(None)

    def test_sdk_accumulator_survives_split_surrogate_end_to_end(self):
        """The actual crash: real SDK accumulate_event over a wrapped stream."""
        anthropic_types = pytest.importorskip("anthropic.types")
        from anthropic.lib.streaming._messages import accumulate_event

        msg = anthropic_types.Message(
            id="m1",
            content=[],
            model="claude-opus-5",
            role="assistant",
            stop_reason=None,
            stop_sequence=None,
            type="message",
            usage=anthropic_types.Usage(input_tokens=1, output_tokens=1),
        )
        events = [
            anthropic_types.RawMessageStartEvent(
                message=msg, type="message_start"
            ),
            anthropic_types.RawContentBlockStartEvent(
                content_block=anthropic_types.ToolUseBlock(
                    id="t1", input={}, name="discord", type="tool_use"
                ),
                index=0,
                type="content_block_start",
            ),
            anthropic_types.RawContentBlockDeltaEvent(
                delta=anthropic_types.InputJSONDelta(
                    partial_json='{"message": "done \ud83d',
                    type="input_json_delta",
                ),
                index=0,
                type="content_block_delta",
            ),
            anthropic_types.RawContentBlockDeltaEvent(
                delta=anthropic_types.InputJSONDelta(
                    partial_json='\ude00"}',
                    type="input_json_delta",
                ),
                index=0,
                type="content_block_delta",
            ),
        ]

        # Unwrapped: this is the incident (guards the repro stays valid).
        snapshot = None
        with pytest.raises(UnicodeEncodeError):
            for event in events:
                snapshot = accumulate_event(
                    event=event, current_snapshot=snapshot
                )

        # Wrapped: accumulates cleanly and yields the real emoji.
        snapshot = None
        for event in _ToolJsonRepairingRawStream(iter(events)):
            snapshot = accumulate_event(event=event, current_snapshot=snapshot)
        assert snapshot.content[0].input == {"message": "done 😀"}


class TestToolArgumentSurrogateChokePoint:
    def test_parse_tool_arguments_splices_raw_string(self):
        from agent.tool_executor import _parse_tool_arguments

        args, err = _parse_tool_arguments('{"content": "a\ud83d\ude00b"}')
        assert err is None
        json.dumps(args)  # must not raise on later re-serialization
        assert args["content"] == "a😀b"
        args["content"].encode("utf-8")

    def test_parse_tool_arguments_floors_orphan_escape(self):
        from agent.tool_executor import _parse_tool_arguments

        # json.loads decodes the ESCAPE \\udd34 into a lone surrogate
        # code point even when the raw string was cleanly encodable.
        args, err = _parse_tool_arguments('{"content": "x\\udd34y"}')
        assert err is None
        assert args["content"] == "x\ufffdy"
        args["content"].encode("utf-8")

    def test_parse_tool_arguments_normal_path_unchanged(self):
        from agent.tool_executor import _parse_tool_arguments

        args, err = _parse_tool_arguments('{"a": 1, "b": "text"}')
        assert err is None
        assert args == {"a": 1, "b": "text"}


class TestBlackboxScrubSurrogates:
    def test_scrub_and_truncate_produces_encodable_text(self):
        from plugins.blackbox.store import scrub_and_truncate

        out = scrub_and_truncate("turn text \ud83d\ude00 with pair and \udd34 orphan")
        out.encode("utf-8")
        assert "😀" in out
        assert "\udd34" not in out

    def test_scrub_and_truncate_clean_text_untouched(self):
        from plugins.blackbox.store import scrub_and_truncate

        assert scrub_and_truncate("plain text") == "plain text"


class TestHonestLabelAfterPartialDelivery:
    def test_surrogate_unicode_error_wraps_to_stream_parse(self):
        """The not-retrying branch must hand the classifier a
        ProviderStreamParseError, not a raw UnicodeEncodeError that
        resolves to the 'connection issue' unknown floor."""
        from agent.error_classifier import FailoverReason, classify_api_error

        wrapped = ProviderStreamParseError(
            "'utf-8' codec can't encode character '\\ud83d' in position 58"
        )
        result = classify_api_error(
            wrapped, provider="claude-apr", model="claude-opus-5"
        )
        assert result.reason == FailoverReason.stream_parse
