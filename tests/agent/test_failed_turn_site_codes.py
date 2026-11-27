"""Deterministic loop outcomes (context overflow, empty reply, persistence failure) end as
failed turns whose chat copy names the slash command to run, and whose ``failure_reason``
lets the desktop card pick a code-specific action instead of a blind Retry.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.error_surface import build_error_surface_from_result
from agent.turn_explainers import EMPTY_RESPONSE_EXPLANATION, TurnExplainersMixin
from agent.turn_failure_copy import (
    SITE_FAILURE_CODES, exit_reason_failure, failure_cause_gloss,
)
from agent.turn_overflow import _Recovery


def _recovery():
    agent = SimpleNamespace(
        model="gpt-5", log_prefix="", _flush_status_buffer=lambda: None, _vprint=lambda *a, **k: None,
        _persist_session=lambda *a, **k: None,
    )
    return _Recovery(
        agent=agent, api_messages=[], system_message=None, effective_task_id="t", api_call_count=2,
        max_compression_attempts=3, messages=[], active_system_prompt=None, conversation_history=None,
        approx_tokens=200_000, compression_attempts=3,
    )


def test_overflow_exhaustion_is_non_retryable_context_overflow_with_slash_commands():
    verdict = _recovery().count_attempt()
    result = verdict.result
    assert result["failed"] is True and result["compression_exhausted"] is True
    assert result["failure_reason"] == "context_overflow" and result["failure_retryable"] is False
    text = result["final_response"]
    assert "/new" in text and "/compress" in text
    assert build_error_surface_from_result(result)["code"] == "context_overflow"


def _context_rejection(request_tokens: int, window: int = 65_536, error="HTTP 500: Context size has been exceeded.",
                       base_url: str = "http://127.0.0.1:1234/v1", provider: str = "lmstudio",
                       compression_attempts: int = 0):
    """Drive ``_recover_context_length`` with a provider "context exceeded" and a request the rough
    estimator prices at ``request_tokens`` against a ``window``-token model (no output cap)."""
    from unittest.mock import patch

    from agent.turn_overflow import _recover_context_length
    from agent.turn_retry_state import TurnRetryState

    st = _recovery()
    st.compression_attempts = compression_attempts
    st.agent.max_tokens = None
    st.agent.context_compressor = SimpleNamespace(context_length=window)
    st.agent.provider, st.agent.base_url, st.agent.tools = provider, base_url, None
    st.agent._buffer_vprint = st.agent._buffer_diagnostic_status = lambda *a, **k: None
    compressed = []
    st.agent._compress_context = lambda msgs, *a, **k: (compressed.append(1) or [{"role": "user", "content": "x"}], None)
    with patch("agent.model_metadata.estimate_request_tokens_rough", return_value=request_tokens), \
         patch("agent.model_metadata.estimate_messages_tokens_rough", side_effect=[request_tokens, 10]), \
         patch("agent.turn_overflow.time.sleep", lambda _: None):
        verdict = _recover_context_length(st, TurnRetryState(), error)
    return verdict, compressed


def test_context_rejection_far_below_the_window_is_not_blamed_on_the_conversation():
    """A single-slot local server rejecting a ~3k-token request (another thread held its
    context) must not read as "this conversation has grown too long": no compression, transient
    + retryable, and the gateway's overflow verdict (drop message / auto-reset) stays off."""
    from gateway.run_turn import is_context_overflow_failure_result

    verdict, compressed = _context_rejection(3_000)
    result = verdict.result
    assert verdict.action == "return" and not compressed
    assert result["failed"] is True and not result.get("compression_exhausted")
    assert result["failure_reason"] == "server_error" and result["failure_retryable"] is True
    text = result["final_response"]
    assert "grown too long" not in text and "/new" not in text
    assert "3,000" in text and "65,536" in text and "background" in text and "/retry" in text
    assert is_context_overflow_failure_result(result, history_len=2) is False


def test_context_rejection_near_the_window_still_compresses():
    """Control: a request that plausibly overflows keeps the compress-and-retry path, and so does
    a small local estimate when the SERVER quoted its own count — its measurement wins."""
    verdict, compressed = _context_rejection(60_000)
    assert compressed and verdict.action == "break"
    verdict, compressed = _context_rejection(3_000, error="prompt is too long: 70000 tokens > 65536 maximum")
    assert compressed and verdict.action == "break"


def test_hosted_context_rejection_far_below_the_known_window_compresses():
    """A hosted route has no shared slot to wait out: a small request rejected there means the
    route's real window is below the one Hermes assumes, so /retry would fail forever. Compress."""
    verdict, compressed = _context_rejection(
        47_000, window=1_000_000, base_url="https://api.anthropic.com",
        error="This model's maximum context length was exceeded. Please reduce the length of the messages.",
    )
    assert compressed and verdict.action == "break"


@pytest.mark.parametrize("provider", ["claude-dtlr", "custom"])
def test_loopback_relay_context_rejection_compresses_once(provider, caplog):
    error = "context_length_exceeded: prompt is too long: the interactive session hit the model context limit; compact the history"
    verdict, compressed = _context_rejection(
        155_618, window=1_000_000, base_url="http://127.0.0.1:18816/v1/", provider=provider, error=error,
    )
    assert compressed and verdict.action == "break"
    assert verdict.provider_overflow_recovery_pending
    assert "155618" in caplog.text and "observed_rejected_request_tokens" in caplog.text
    verdict, compressed = _context_rejection(
        155_618, window=1_000_000, base_url="http://127.0.0.1:18816/v1/", provider=provider,
        error=error, compression_attempts=1,
    )
    assert not compressed and verdict.action == "return"
    assert verdict.result is not None
    assert verdict.result["failure_retryable"] is True and "compression_exhausted" not in verdict.result


_RELAY_REJECTION = (
    "context_length_exceeded: prompt is too long: the interactive session hit the model context limit; compact the history"
)


def _assert_session_kept(result):
    """``compression_exhausted`` makes ``gateway/run_turn.py::_hmwa_compression_exhaustion_reset``
    wipe the session; a rejection far below the window is not proof the transcript is too long."""
    from gateway.run_turn import is_context_overflow_failure_result

    assert result["failed"] is True
    assert "compression_exhausted" not in result
    assert result["failure_retryable"] is True and result["failure_reason"] != "context_overflow"
    text = result["final_response"]
    assert "/compress" in text and "/retry" in text and "/new" not in text
    assert is_context_overflow_failure_result(result, history_len=3_000) is False


def test_second_explicit_rejection_below_the_window_keeps_the_session():
    verdict, compressed = _context_rejection(
        155_618, window=1_000_000, base_url="http://127.0.0.1:18816/v1/", provider="claude-dtlr",
        error=_RELAY_REJECTION, compression_attempts=1,
    )
    assert not compressed and verdict.action == "return"
    _assert_session_kept(verdict.result)


def test_explicit_rejection_below_the_window_that_cannot_shrink_keeps_the_session():
    from unittest.mock import patch

    with patch("agent.turn_overflow._Recovery.compress_scored_by_tokens", return_value=(None, False, 155_618)):
        verdict, _ = _context_rejection(
            155_618, window=1_000_000, base_url="http://127.0.0.1:18816/v1/", provider="claude-dtlr",
            error=_RELAY_REJECTION,
        )
    assert verdict.action == "return"
    _assert_session_kept(verdict.result)


@pytest.mark.parametrize("error", [
    _RELAY_REJECTION + "; request_id=req_12345678",  # Prism be4e9c23bb2b: an id is not a token count
    _RELAY_REJECTION + " at 2026-11-09T18:46:03Z",
])
def test_unrelated_digits_in_the_rejection_still_keep_the_session(error):
    from unittest.mock import patch

    with patch("agent.turn_overflow._Recovery.compress_scored_by_tokens", return_value=(None, False, 155_618)):
        verdict, _ = _context_rejection(
            155_618, window=1_000_000, base_url="http://127.0.0.1:18816/v1/", provider="claude-dtlr", error=error,
        )
    assert verdict.action == "return"
    _assert_session_kept(verdict.result)


@pytest.mark.parametrize("request_tokens", [155_400, 400_123])
def test_kept_rejection_with_400_in_its_counts_is_not_read_as_overflow(request_tokens):
    """Prism 82b3fd6acb0e: the copy prints ~155,400; the gateway's bare-"400" heuristic for long
    sessions must not reclassify the session-keeping failure as a context overflow."""
    from gateway.run_turn import is_context_overflow_failure_result

    verdict, _ = _context_rejection(
        request_tokens, window=1_000_000, base_url="http://127.0.0.1:18816/v1/", provider="claude-dtlr",
        error=_RELAY_REJECTION, compression_attempts=1,
    )
    assert "400" in verdict.result["error"]
    assert is_context_overflow_failure_result(verdict.result, history_len=3_000) is False


def test_local_server_rejection_with_a_request_id_is_still_shared_slot_contention():
    """The local-endpoint branch uses the same measured-count check: a request id is not a count."""
    verdict, compressed = _context_rejection(3_000, error="HTTP 500: Context size has been exceeded. id=req_12345678")
    assert not compressed and verdict.action == "return"
    assert verdict.result["failure_retryable"] is True and "compression_exhausted" not in verdict.result


def test_local_server_rejection_with_400_in_its_counts_is_not_read_as_overflow():
    """Same class on main's sibling exit (local shared-slot rejection)."""
    from gateway.run_turn import is_context_overflow_failure_result

    verdict, compressed = _context_rejection(3_400)
    assert not compressed and "400" in verdict.result["error"]
    assert is_context_overflow_failure_result(verdict.result, history_len=3_000) is False


@pytest.mark.parametrize("request_tokens,error", [
    (990_000, _RELAY_REJECTION),  # at the window: a genuine overflow
    (155_618, "context_length_exceeded: prompt is too long: 1200000 tokens > 1000000 maximum"),  # provider's own count
    (155_618, "context_length_exceeded: prompt is too long: 233153 tokens > 200000"),  # comparison form
])
def test_genuine_overflow_that_cannot_shrink_still_exhausts(request_tokens, error):
    from unittest.mock import patch

    with patch("agent.turn_overflow._Recovery.compress_scored_by_tokens", return_value=(None, False, request_tokens)):
        verdict, _ = _context_rejection(
            request_tokens, window=1_000_000, base_url="http://127.0.0.1:18816/v1/", provider="claude-dtlr",
            error=error,
        )
    assert verdict.action == "return"
    assert verdict.result["compression_exhausted"] is True
    assert verdict.result["failure_reason"] == "context_overflow" and verdict.result["failure_retryable"] is False


def test_context_ceiling_observation_never_logs_url_credentials(caplog):
    """Prism d4b1105d40df: the observation line carries scheme/host/port only, never userinfo,
    path or query (a relay base_url may embed a password or an api_key)."""
    error = "context_length_exceeded: prompt is too long"
    _context_rejection(
        155_618, window=1_000_000, provider="custom", error=error,
        base_url="https://alice:s3cretpw@relay.example:8443/v1/tenant-path?api_key=sk-leak123",
    )
    records = [r.getMessage() for r in caplog.records if "observed_rejected_request_tokens" in r.getMessage()]
    assert records, "observation line not emitted"
    for text in records:
        assert "endpoint=https://relay.example:8443 " in text
        for secret in ("alice", "s3cretpw", "sk-leak123", "api_key", "tenant-path"):
            assert secret not in text


def test_empty_response_exhaustion_has_one_text_everywhere():
    """One constant feeds the CLI explainer and the gateway '(empty)' rewrite; no surface
    asserts 'after processing tool results' or 'inspect the tool output above'."""
    verdict = exit_reason_failure("empty_response_exhausted")
    assert (verdict.reason, verdict.retryable) == ("empty_response", True)
    text = TurnExplainersMixin._format_turn_completion_explanation("empty_response_exhausted", model="llama3")
    assert EMPTY_RESPONSE_EXPLANATION.format(model="llama3") in text


def test_persistence_failure_default_copy_is_actionable_and_profile_aware(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/srv/hermes-profile")
    text = TurnExplainersMixin._format_turn_completion_explanation("session_persistence_failed", "replaced")
    assert "hermes gateway stop" in text and "hermes doctor" in text
    assert "~/.hermes" not in text and "/srv/hermes-profile" in text
    assert "manifest" not in text  # the runbook stays in logger.error at hermes_state




@pytest.mark.parametrize("exit_reason", ["empty_response_exhausted", "local_processing_error(TypeError)"])
def test_advisory_exit_reasons_stamp_a_code_but_never_flip_failed(exit_reason):
    """Cron silence, the kanban dispatcher breaker and gateway transcript persistence all key
    on ``failed``; these two exits must keep failed=False and only gain the descriptor code."""
    verdict = exit_reason_failure(exit_reason)
    assert verdict is not None and verdict.fails_turn is False
    assert verdict.reason in SITE_FAILURE_CODES


@pytest.mark.parametrize("exit_reason, code", [
    ("context_compression_timeout", "context_overflow"),
    ("ollama_runtime_context_too_small", "context_overflow"),
    ("redirect_restart_limit_exceeded", "loop_error"),
    ("rebuilt_restart_limit_exceeded", "loop_error"),
])
def test_previously_bare_exit_reasons_now_carry_a_code(exit_reason, code):
    verdict = exit_reason_failure(exit_reason)
    assert verdict is not None and verdict.reason == code
    assert build_error_surface_from_result(
        {"failed": True, "error": "x", "failure_reason": verdict.reason, "failure_retryable": verdict.retryable}
    )["code"] == code


def test_every_failure_code_copy_key_is_a_failure_code():
    """The copy table split: keys of the failure-code table are exactly codes the descriptor
    contract lists (one-off strings live in their own table)."""
    from agent.turn_failure_copy import _FAILURE_CODE_COPY, _ONE_OFF_COPY

    assert set(_FAILURE_CODE_COPY) <= SITE_FAILURE_CODES
    assert not (set(_ONE_OFF_COPY) & SITE_FAILURE_CODES)


def test_cause_gloss_substitutes_the_subject_and_skips_unknown_reasons():
    assert "the job's" in failure_cause_gloss("context_overflow", subject="this job", possessive="the job's")
    assert failure_cause_gloss("model_not_found")
    assert failure_cause_gloss("unknown") is None and failure_cause_gloss(None) is None
