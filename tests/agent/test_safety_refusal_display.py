"""Apollo's status-less Codex safety block must not look like a connection issue.

This is display-only: adding the phrase to the existing content-policy matcher
would change retries, fallback eligibility, credential handling and cooldowns.
"""
import ast
from pathlib import Path
from unittest.mock import patch

import httpx
import openai
import pytest

from agent.error_classifier import FailoverReason, classify_api_error
from tests.agent.test_fallback_reason_threading import TestOverloadedFailoverAnnouncesReasonE2E as _LiveAnnounce


LIVE_MESSAGE = (
    "This request was blocked by our safety systems. "
    "Reason: Potentially unintended activity."
)


def _error(message=LIVE_MESSAGE, status=None):
    request = httpx.Request("POST", "https://example.test/responses")
    if status is None:
        return openai.APIError(message, request=request, body=None)
    return openai.APIStatusError(
        message, response=httpx.Response(status, request=request), body=None,
    )


@pytest.mark.parametrize("status", [None, 400, 403, 500])
def test_safety_display_preserves_existing_routing(status):
    classified = classify_api_error(_error(status=status), provider="openai-codex")
    baseline = classify_api_error(_error("unrecognized error", status), provider="openai-codex")
    for field in ("reason", "retryable", "should_fallback", "should_rotate_credential", "should_compress"):
        assert getattr(classified, field) == getattr(baseline, field)
    if status is None:
        assert classified.reason is FailoverReason.unknown
        assert classified.retryable and not classified.should_fallback
    assert classified.display_reason is FailoverReason.content_policy_blocked


@pytest.mark.parametrize("error", [
    httpx.ConnectError("connection refused"),
    httpx.ReadTimeout("timed out"),
    httpx.RemoteProtocolError("peer closed connection without sending complete message body"),
    _error("A safety systems connection timed out"),
    _error("Potentially unintended activity"),
])
def test_negative_controls_do_not_display_safety_refusal(error):
    classified = classify_api_error(error, provider="openai-codex")
    assert classified.display_reason is classified.reason
    assert classified.display_reason is not FailoverReason.content_policy_blocked


@pytest.mark.parametrize("status", [None, 400, 403, 500])
def test_real_fallback_notification_names_safety_without_new_cooldown(status):
    helper = _LiveAnnounce()
    agent, captured = helper._make_agent_with_sink(
        [{"provider": "claude-apx-1", "model": "claude-opus-4-8"}]
    )
    classified = classify_api_error(_error(status=status), provider="openai-codex")
    cooldown = getattr(agent, "_rate_limited_until", None)
    with (
        patch("agent.auxiliary_client.resolve_provider_client", return_value=(helper._mock_client(), "resolved")),
        patch("hermes_cli.config.read_raw_config", return_value={"model": {"announce_route_change": True}}),
    ):
        assert agent._try_activate_fallback(
            reason=classified.reason, display_reason=classified.display_reason,
        )
    messages = [text for _, text in captured if "Model fallback" in text]
    assert len(messages) == 1
    assert "Model fallback (safety refusal):" in messages[0]
    assert "connection issue" not in messages[0]
    assert getattr(agent, "_rate_limited_until", None) == cooldown
    print(messages[0])


@pytest.mark.parametrize("message, label, primary_attempts", [
    (LIVE_MESSAGE, "safety refusal", 3),
    ("unrecognized error", "connection issue", 3),
    # Existing transport policy tries fallback after the second failure.
    ("connection reset by peer", "connection dropped", 2),
])
def test_retry_exhaustion_runs_real_loop_and_renders_honest_notice(message, label, primary_attempts):
    from tests.run_agent.test_malformed_provider_stream_failover import (
        _make_agent, _fallback_client, _response,
    )

    captured = []
    agent = _make_agent(captured)
    agent.provider = "openai-codex"
    agent.api_mode = "chat_completions"
    agent.client = _fallback_client()
    attempts = []

    def respond(*args, **kwargs):
        attempts.append(agent.provider)
        if agent.provider == "openai-codex":
            raise _error(message)
        return _response("Recovered through fallback")

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=respond),
        patch.object(agent, "_try_activate_fallback", wraps=agent._try_activate_fallback) as activate,
        patch.object(agent, "_try_recover_primary_transport", return_value=False),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.auxiliary_client.resolve_provider_client", return_value=(_fallback_client(), "fallback/model")),
        patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda model, provider: model),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
        patch("hermes_cli.config.read_raw_config", return_value={"model": {"announce_route_change": True}}),
        patch("agent.conversation_loop.jittered_backoff", return_value=0.0),
    ):
        result = agent.run_conversation("hello")

    assert attempts == ["openai-codex"] * primary_attempts + ["openrouter"]
    assert result["completed"] is True
    activate.assert_called_once()
    notices = [text for _, text in captured if "Model fallback" in text]
    assert len(notices) == 1
    assert f"Model fallback ({label}):" in notices[0]
    print(notices[0])


def test_loop_threads_display_separately_from_routing():
    """The production caller must supply the label, not just the unit test."""
    root = Path(__file__).resolve().parents[2]
    tree = ast.parse((root / "agent/conversation_loop.py").read_text())
    matched = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "_try_activate_fallback":
            continue
        keywords = {kw.arg: ast.unparse(kw.value) for kw in node.keywords}
        reason = keywords.get("reason", "")
        if "classified" not in reason:
            continue
        matched += 1
        assert "display_reason" in keywords
        assert keywords["display_reason"] == "classified.display_reason"
        assert "display_reason" not in reason
    assert matched > 0
