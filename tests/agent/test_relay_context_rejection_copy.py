"""An explicit context_length_exceeded far below the believed window never renders the shared-slot copy
(card t_c2a562c9).

2026-11-09 18:46:03 (gateway.error.log): claude-dtlr / claude-fable-5-2 through the loopback relay
http://127.0.0.1:18816/v1/ returned 400 context_length_exceeded at ~155,618 of 1,000,000 tokens. The turn
died with "The model server rejected this request as too large ... Another request on the same server ...
was probably holding its capacity ... send /retry". The rejection was the CLI's own 200k local ceiling
(claude-bpx #572/#574), not a busy server; /retry could never pass and no compression was tried.
#193 (efa168c9991) routes an explicit rejection to compress-once-then-keep-the-session; the tests in
test_failed_turn_site_codes.py pin the verdicts but none asserts the misleading copy is gone, on the
chat text OR the buffered notices, for the exact error string the gateway logged.

Red on efa168c9991^ (the 18:46 copy is rendered), green on fork/main.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.turn_overflow import _Recovery, _recover_context_length
from agent.turn_retry_state import TurnRetryState

# Verbatim from gateway.error.log 2026-11-09 18:46:03,340 (agent.chat_completion_helpers).
LOGGED_ERROR = ("Error code: 400 - {'error': {'type': 'invalid_request_error', 'code': 'context_length_exceeded', "
                "'message': 'prompt is too long: the interactive session hit the model context limit; compact the history'}}")
RELAY = "http://127.0.0.1:18816/v1/"
SHARED_SLOT_PHRASES = ("holding its capacity", "holding its context", "another request on the same server",
                       "may have been holding", "wait a moment")


def _drive(compression_attempts, *, shrinks=True, provider="claude-dtlr", error=LOGGED_ERROR):
    printed = []
    agent = SimpleNamespace(
        model="claude-fable-5-2", log_prefix="", provider=provider, base_url=RELAY, tools=None, max_tokens=None,
        context_compressor=SimpleNamespace(context_length=1_000_000),
        _flush_status_buffer=lambda: None, _persist_session=lambda *a, **k: None,
        _vprint=lambda msg, *a, **k: printed.append(str(msg)),
        _buffer_vprint=lambda msg, *a, **k: printed.append(str(msg)),
        _buffer_diagnostic_status=lambda msg, *a, **k: printed.append(str(msg)),
    )
    st = _Recovery(
        agent=agent, api_messages=[], system_message=None, effective_task_id="t", api_call_count=2,
        max_compression_attempts=3, messages=[], active_system_prompt=None, conversation_history=None,
        approx_tokens=155_618, compression_attempts=compression_attempts,
    )
    agent._compress_context = lambda msgs, *a, **k: ([{"role": "user", "content": "x"}], None)
    after = 10 if shrinks else 155_618
    with patch("agent.model_metadata.estimate_request_tokens_rough", return_value=155_618), \
         patch("agent.model_metadata.estimate_messages_tokens_rough", side_effect=[155_618, after, after, after]), \
         patch("agent.turn_overflow.time.sleep", lambda _: None):
        verdict = _recover_context_length(st, TurnRetryState(), error)
    return verdict, printed


def _rendered(verdict, printed):
    text = " ".join(printed)
    if verdict.result is not None:
        text += " " + str(verdict.result.get("final_response") or "") + " " + str(verdict.result.get("error") or "")
    return text.lower()


@pytest.mark.parametrize("provider", ["claude-dtlr", "custom"])
@pytest.mark.parametrize("attempts,shrinks", [(0, True), (0, False), (1, True), (2, True)])
def test_explicit_relay_rejection_below_the_window_never_blames_a_busy_server(provider, attempts, shrinks):
    verdict, printed = _drive(attempts, shrinks=shrinks, provider=provider)
    text = _rendered(verdict, printed)
    for phrase in SHARED_SLOT_PHRASES:
        assert phrase not in text, f"shared-slot copy {phrase!r} rendered for an explicit rejection: {text[:600]}"
    if verdict.result is not None:
        # when the turn does end here, the user is told to shrink (compress), not just to wait and retry
        assert "/compress" in verdict.result["final_response"], verdict.result["final_response"]
        assert verdict.result.get("context_overflow") is False or verdict.result.get("compression_exhausted") is True


def test_first_explicit_relay_rejection_compresses_instead_of_ending_the_turn():
    """The 18:46 turn ended on the FIRST rejection with zero compression attempts."""
    verdict, _ = _drive(0)
    assert verdict.action == "break" and verdict.result is None
