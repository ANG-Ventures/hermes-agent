"""Core ContextCompressor must never re-send content a route's safeguards refused.

t_0970eb0b (non-LCM sibling of t_6c01fd8e / #1501): a 400 ``safeguard_refusal``
from claude-bpx#394 used to fall back to the main model once, then re-send the
same turns on every compaction after a 60 s cooldown, forever. These tests
count ``call_llm`` sends across repeated ``compress()`` passes.
"""
import copy
from unittest.mock import patch

import pytest

from agent.context_compressor import (
    ContextCompressor,
    _SUMMARY_REFUSALS,
    _is_summary_safeguard_refusal,
)


class _SafeguardRefusal(Exception):
    """Shape of openai.BadRequestError for the claude-bpx#394 400."""

    status_code = 400
    code = "safeguard_refusal"

    def __init__(self):
        super().__init__(
            "Error code: 400 - {'error': {'type': 'invalid_request_error', "
            "'code': 'safeguard_refusal', 'message': \"Claude Code's "
            "safeguards flagged this message\"}}"
        )


def _messages(n=12, tag="Observatory inventory item"):
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"{tag} {i}"}
        for i in range(n)
    ]


def _compressor(summary_model="configured-summary"):
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        c = ContextCompressor(
            model="main-model", provider="claude-apr", protect_first_n=2,
            protect_last_n=2, abort_on_summary_failure=False, quiet_mode=True,
        )
    c.summary_model = summary_model
    return c


def _passes(compressor, messages_per_pass, side_effect):
    """Run one compress() per message list; return call_llm sends per pass."""
    sends = []
    results = []
    for messages in messages_per_pass:
        compressor._clear_compression_failure_cooldown()  # the 60 s elapsed
        with patch("agent.context_compressor.call_llm", side_effect=side_effect) as call:
            results.append(compressor.compress(messages, current_tokens=999999, force=True))
        sends.append(call.call_count)
    return sends, results


def _refuse(**_kwargs):
    raise _SafeguardRefusal()


def test_classifier_matches_code_and_text():
    assert _is_summary_safeguard_refusal(_SafeguardRefusal())
    assert _is_summary_safeguard_refusal(RuntimeError("HTTP 500: safeguards flagged this message"))
    assert not _is_summary_safeguard_refusal(RuntimeError("HTTP 400: context too long"))


def test_repeated_compress_sends_refused_turns_once():
    compressor = _compressor()
    messages = _messages()
    original = copy.deepcopy(messages)
    sends, results = _passes(compressor, [messages, messages, messages], _refuse)
    # Before the fix: 2 sends (summary model, then main-model fallback), then
    # 1 per later pass forever. After: one send total, then no resend.
    assert sends == [1, 0, 0]
    assert all(r == original for r in results)  # session preserved, not rotated
    assert compressor._last_compress_aborted
    assert compressor._last_summary_refusal_failure
    assert "already refused" in (compressor._last_summary_error or "")
    assert compressor.summary_model == "configured-summary"  # no main fallback


def test_grown_window_still_not_resent():
    """The middle window grows at its end between passes; the refused turns
    are still inside it, so the grown input is not re-sent either."""
    compressor = _compressor()
    base = _messages(12)
    grown = base[:-2] + _messages(4, tag="Later turn") + base[-2:]
    sends, _ = _passes(compressor, [base, grown], _refuse)
    assert sends == [1, 0]


def test_other_content_on_same_route_is_still_sent():
    compressor = _compressor()
    ok = {"choices": [{"finish_reason": "stop", "message": {"content": "Checked the telescope inventory."}}]}
    sends, _ = _passes(compressor, [_messages()], _refuse)
    assert sends == [1]
    sends, results = _passes(
        compressor, [_messages(tag="Unrelated calibration note")], lambda **_: ok
    )
    assert sends == [1]
    assert not compressor._last_compress_aborted


def test_content_filter_shape_is_latched_too():
    compressor = _compressor()
    refused = {
        "choices": [{"finish_reason": "content_filter", "message": {"content": ""}}],
        "provider_data": {"stop_details": {"category": "cyber"}},
    }
    sends, _ = _passes(compressor, [_messages()], lambda **_: refused)
    assert sends == [1]
    assert "category=cyber" in (compressor._last_summary_error or "")
    sends, _ = _passes(compressor, [_messages()] * 2, lambda **_: refused)
    assert sends == [0, 0]
    assert "already refused" in (compressor._last_summary_error or "")
    assert compressor._last_compress_aborted


def test_fallback_chain_route_tried_once_each_then_preserved():
    compressor = _compressor()
    chain = [{"provider": "openrouter", "model": "non-claude-summary"}]
    seen_models = []

    def call(**kwargs):
        seen_models.append(kwargs.get("model"))
        raise _SafeguardRefusal()

    with patch(
        "agent.context_compressor._compression_refusal_fallback_routes",
        return_value=chain,
    ):
        sends, _ = _passes(compressor, [_messages()] * 3, call)
    assert sends == [2, 0, 0]
    assert seen_models == ["configured-summary", "non-claude-summary"]


def test_fallback_chain_route_can_produce_the_summary():
    compressor = _compressor()
    chain = [{"provider": "openrouter", "model": "non-claude-summary"}]
    ok = {"choices": [{"finish_reason": "stop", "message": {"content": "Inventory reviewed; continue observations."}}]}

    def call(**kwargs):
        if kwargs.get("model") == "non-claude-summary":
            assert kwargs.get("provider") == "openrouter"
            return ok
        raise _SafeguardRefusal()

    with patch(
        "agent.context_compressor._compression_refusal_fallback_routes",
        return_value=chain,
    ):
        sends, results = _passes(compressor, [_messages()], call)
    assert sends == [2]
    assert not compressor._last_compress_aborted
    assert not compressor._last_summary_refusal_failure
    assert len(results[0]) < 12


# --- t_2ba784cc (Prism P1 on #1503): loop and send must agree on the key ---


def test_model_less_fallback_route_refusal_terminates():
    """A fallback_chain entry may omit ``model`` (only ``provider`` is
    required). ``_generate_summary`` then keeps ``summary_model`` in its
    call kwargs, so the loop in ``_handle_summary_refusal`` must derive the
    SAME latch key. When the keys diverged, a refusing model-less route was
    latched under one key and checked under another, and compression
    recursed until RecursionError."""
    compressor = _compressor()
    chain = [{"provider": "gemini-bridge"}]
    seen = []

    def call(**kwargs):
        seen.append((kwargs.get("provider"), kwargs.get("model")))
        raise _SafeguardRefusal()

    with patch(
        "agent.context_compressor._compression_refusal_fallback_routes",
        return_value=chain,
    ):
        sends, results = _passes(compressor, [_messages()] * 2, call)
    assert sends == [2, 0]  # primary + the fallback route, once each, then none
    assert seen[1][0] == "gemini-bridge"
    assert compressor._last_compress_aborted


def test_compressor_refusal_latch_expires(monkeypatch):
    """A refusal latch must not pin content for the life of the process:
    safeguard classifiers misfire and change."""
    import agent.context_compressor as cc

    clock = {"now": 1000.0}
    monkeypatch.setattr(cc.time, "monotonic", lambda: clock["now"])
    compressor = _compressor()
    sends, _ = _passes(compressor, [_messages()] * 2, _refuse)
    assert sends == [1, 0]
    clock["now"] += 30 * 24 * 3600
    sends, _ = _passes(compressor, [_messages()], _refuse)
    assert sends == [1]


@pytest.fixture(autouse=True)
def _clean_latch():
    _SUMMARY_REFUSALS.clear()
    yield
    _SUMMARY_REFUSALS.clear()
