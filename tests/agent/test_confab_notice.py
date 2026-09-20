"""Consumer tests for the out-of-band ``hermes_confab_notice`` extension.

Contract source: ``claude-bpx/docs/SPEC-confab-marker-out-of-band.md`` v1.

The signal is load-bearing for triage: a reply carrying a catch must still be
identifiable as confirmed self-confabulation. Out of band must not mean
invisible — but it must also never reach a model, which is what the replay
tests pin.
"""

import json
from types import SimpleNamespace

import pytest

from agent.confab_notice import (
    CONFAB_NOTICE_DISPLAY_KIND,
    CONFAB_NOTICE_FIELD,
    CONFAB_NOTICE_KEY,
    CONFAB_NOTICE_TEXT,
    extract_confab_notice,
    validate_confab_notice,
)


VALID = {
    "version": 1,
    "kind": "scaffold_confab_removed",
    "request_id": "3b264082",
    "scope": "visible",
    "grammar": "inbound",
}


class TestValidator:
    @pytest.mark.parametrize("kind", [
        "scaffold_confab_removed", "tool_call_unparseable", "tool_call_as_text",
    ])
    def test_all_v1_kinds_round_trip(self, kind):
        notice = {**VALID, "kind": kind}
        assert validate_confab_notice(notice) == notice

    @pytest.mark.parametrize("kind", ["tool_call_unparseable", "tool_call_as_text"])
    @pytest.mark.parametrize("scope", ["intermediate", "both"])
    def test_tool_call_scope_must_be_visible(self, kind, scope):
        assert validate_confab_notice({**VALID, "kind": kind, "scope": scope}) is None

    def test_valid_v1_notice_round_trips(self):
        assert validate_confab_notice(dict(VALID)) == VALID

    def test_grammar_may_be_null_when_multiple_catches(self):
        raw = {**VALID, "grammar": None}
        assert validate_confab_notice(raw)["grammar"] is None

    def test_absent_grammar_normalizes_to_null(self):
        raw = {k: v for k, v in VALID.items() if k != "grammar"}
        assert validate_confab_notice(raw)["grammar"] is None

    @pytest.mark.parametrize("scope", ["visible", "intermediate", "both"])
    def test_all_contract_scopes_accepted(self, scope):
        assert validate_confab_notice({**VALID, "scope": scope})["scope"] == scope

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "scaffold_confab_removed",
            123,
            [],
            {},
            {**VALID, "version": 2},          # unknown version fails closed
            {**VALID, "version": "1"},        # string version is not 1
            {**VALID, "version": True},       # bool is an int subclass
            {**VALID, "kind": "something_else"},
            {**VALID, "kind": None},
            {**VALID, "request_id": ""},
            {**VALID, "request_id": "   "},
            {**VALID, "request_id": None},
            {**VALID, "request_id": 3264082},
            {**VALID, "request_id": "x" * 257},
            {**VALID, "scope": "everything"},
            {**VALID, "scope": None},
            {**VALID, "grammar": ""},
            {**VALID, "grammar": 7},
            {**VALID, "grammar": "g" * 257},
        ],
    )
    def test_invalid_payloads_fail_closed(self, raw):
        assert validate_confab_notice(raw) is None

    def test_extra_provider_fields_are_not_carried_through(self):
        """A provider cannot smuggle arbitrary keys into display_metadata."""
        out = validate_confab_notice({**VALID, "evil": {"b": 1}, "content": "secret"})
        assert set(out) == {"version", "kind", "request_id", "scope", "grammar"}

    def test_validator_returns_a_copy_not_the_input(self):
        raw = dict(VALID)
        out = validate_confab_notice(raw)
        out["request_id"] = "mutated"
        assert raw["request_id"] == "3b264082"


class TestExtraction:
    def test_reads_top_level_attribute(self):
        obj = SimpleNamespace(**{CONFAB_NOTICE_FIELD: dict(VALID)})
        assert extract_confab_notice(obj) == VALID

    def test_reads_openai_sdk_model_extra_bag(self):
        """Unknown top-level fields land in model_extra on a pydantic model."""
        obj = SimpleNamespace(model_extra={CONFAB_NOTICE_FIELD: dict(VALID)})
        assert extract_confab_notice(obj) == VALID

    def test_absent_field_is_none(self):
        assert extract_confab_notice(SimpleNamespace(model_extra={})) is None
        assert extract_confab_notice(SimpleNamespace()) is None
        assert extract_confab_notice(None) is None

    def test_invalid_payload_on_the_wire_is_dropped(self):
        obj = SimpleNamespace(**{CONFAB_NOTICE_FIELD: {"version": 99}})
        assert extract_confab_notice(obj) is None


class TestOpenAISDKUnknownFieldFixture:
    """Spec test 1 — prove the SDK retains the extension in both shapes."""

    def test_completion_model_retains_unknown_top_level_field(self):
        from openai.types.chat import ChatCompletion

        completion = ChatCompletion.model_validate(
            {
                "id": "cmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": "claude-bpx",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "All good here."},
                        "finish_reason": "stop",
                    }
                ],
                CONFAB_NOTICE_FIELD: dict(VALID),
            }
        )

        assert completion.model_extra[CONFAB_NOTICE_FIELD] == VALID
        assert extract_confab_notice(completion) == VALID

    def test_chunk_model_retains_unknown_top_level_field(self):
        from openai.types.chat import ChatCompletionChunk

        chunk = ChatCompletionChunk.model_validate(
            {
                "id": "cmpl-1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "claude-bpx",
                "choices": [],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
                CONFAB_NOTICE_FIELD: dict(VALID),
            }
        )

        assert chunk.model_extra[CONFAB_NOTICE_FIELD] == VALID
        assert extract_confab_notice(chunk) == VALID


class TestNonStreamTransport:
    """Spec §consumer step 2 — normalize_response preserves it in provider_data."""

    @pytest.fixture
    def transport(self):
        import agent.transports.chat_completions  # noqa: F401
        from agent.transports import get_transport

        return get_transport("chat_completions")

    def _response(self, notice, content="All good here."):
        kwargs = {}
        if notice is not None:
            kwargs[CONFAB_NOTICE_FIELD] = notice
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content, tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=None,
            **kwargs,
        )

    def test_valid_notice_reaches_provider_data(self, transport):
        normalized = transport.normalize_response(self._response(dict(VALID)))

        assert normalized.provider_data[CONFAB_NOTICE_KEY] == VALID
        assert normalized.confab_notice == VALID

    def test_assistant_content_is_byte_identical_to_cleaned_model_text(self, transport):
        """Spec test 3 — the notice must not alter content in any way."""
        normalized = transport.normalize_response(self._response(dict(VALID)))
        assert normalized.content == "All good here."

    def test_clean_turn_has_no_notice(self, transport):
        normalized = transport.normalize_response(self._response(None))
        assert normalized.confab_notice is None
        assert not (normalized.provider_data or {}).get(CONFAB_NOTICE_KEY)

    def test_unvalidated_payload_is_never_promoted(self, transport):
        """Spec test 7 — a non-bpx provider attempting the extension."""
        normalized = transport.normalize_response(
            self._response({"version": 1, "kind": "totally_made_up", "request_id": "x",
                            "scope": "visible", "grammar": None})
        )
        assert normalized.confab_notice is None


class TestAssistantRowStamping:
    """Spec §consumer step 3 — display_kind / display_metadata on the row."""

    def _agent(self):
        agent = SimpleNamespace(
            verbose_logging=False,
            reasoning_callback=None,
            stream_delta_callback=None,
            _stream_callback=None,
            _extract_reasoning=lambda _m: None,
            _strip_think_blocks=lambda t: t,
            _needs_thinking_reasoning_pad=lambda: False,
        )
        return agent

    def _build(self, notice):
        from agent.chat_completion_helpers import build_assistant_message
        from agent.transports.types import NormalizedResponse

        normalized = NormalizedResponse(
            content="All good here.",
            tool_calls=None,
            finish_reason="stop",
            provider_data={CONFAB_NOTICE_KEY: notice} if notice else None,
        )
        return build_assistant_message(self._agent(), normalized, "stop")

    def test_row_carries_display_kind_and_versioned_metadata(self):
        msg = self._build(dict(VALID))

        assert msg["display_kind"] == CONFAB_NOTICE_DISPLAY_KIND
        assert msg["display_metadata"][CONFAB_NOTICE_KEY] == VALID
        assert msg["display_metadata"][CONFAB_NOTICE_KEY]["version"] == 1

    def test_content_is_not_suffixed_with_any_notice_text(self):
        msg = self._build(dict(VALID))

        assert msg["content"] == "All good here."
        assert "confab" not in msg["content"].lower()
        assert VALID["request_id"] not in msg["content"]

    def test_clean_turn_row_is_unchanged(self):
        """Today's bridge (in-band marker, no field): behavior unchanged."""
        msg = self._build(None)

        assert "display_kind" not in msg
        assert "display_metadata" not in msg


class TestStatusTextIsActionable:
    def test_status_text_names_the_catch(self):
        """Out of band != invisible. The line must be triage-actionable."""
        assert "onfab" in CONFAB_NOTICE_TEXT
        assert CONFAB_NOTICE_TEXT.strip()
