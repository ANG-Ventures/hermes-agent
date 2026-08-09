"""Regression tests for deterministic response-decoding failures.

A corrupt compressed response is deterministic for the bytes returned by a
route. Retrying the same route only decodes the same bytes again; the turn must
fall through to the next configured route without spending every route's full
retry budget.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from requests.exceptions import ContentDecodingError

from agent.error_classifier import (
    FailoverReason,
    classify_api_error,
    deterministic_error_signature,
)
from run_agent import AIAgent


_DECODE_MESSAGE = "Error -3 while decompressing data: incorrect header check"


def _make_agent_with_fallback(fallback_chain):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="primary-key-abcdef12",
            base_url="https://primary.example/v1",
            provider="zai",
            model="glm-primary",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_chain,
        )
        agent.client = MagicMock()
        return agent


def _mock_response(content: str):
    message = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="fallback/model", usage=None)


def _fallback_client():
    client = MagicMock()
    client.api_key = "fallback-key-abcdef12"
    client.base_url = "https://fallback.example/v1"
    client._custom_headers = None
    client.default_headers = None
    return client


def _run_decode_chain(classifier=classify_api_error):
    fallback_chain = [
        {
            "provider": "zai",
            "model": "glm-fallback-1",
            "base_url": "https://fallback.example/v1",
        },
        {
            "provider": "zai",
            "model": "glm-fallback-2",
            "base_url": "https://fallback.example/v1",
        },
    ]
    agent = _make_agent_with_fallback(fallback_chain)
    agent._api_max_retries = 3
    calls = []

    def fake_api_call(_api_kwargs):
        calls.append((agent.provider, agent.model))
        if agent.model in {"glm-primary", "glm-fallback-1"}:
            raise httpx.DecodingError(_DECODE_MESSAGE)
        return _mock_response("Recovered after corrupt routes")

    fallback_client = _fallback_client()

    def resolve_provider_client(_provider, *, model, **_kwargs):
        return fallback_client, model

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.conversation_loop.classify_api_error", side_effect=classifier),
        patch("agent.conversation_loop.time.sleep"),
        patch("run_agent.OpenAI", return_value=MagicMock()),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            side_effect=resolve_provider_client,
        ),
        patch(
            "hermes_cli.model_normalize.normalize_model_for_provider",
            side_effect=lambda model, _provider: model,
        ),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
    ):
        result = agent.run_conversation("hello")

    return result, calls


def test_httpx_decoding_error_is_distinct_and_non_retryable_on_same_rung():
    result = classify_api_error(httpx.DecodingError(_DECODE_MESSAGE))

    assert result.reason == FailoverReason.decode_error
    assert result.retryable is False
    assert result.should_fallback is True
    assert result.should_compress is False
    assert result.should_rotate_credential is False


def test_requests_content_decoding_error_maps_to_the_same_reason():
    result = classify_api_error(ContentDecodingError(_DECODE_MESSAGE))

    assert result.reason == FailoverReason.decode_error
    assert result.retryable is False
    assert result.should_fallback is True


def test_wrapped_decoding_error_is_classified_from_its_cause():
    cause = httpx.DecodingError(_DECODE_MESSAGE)
    wrapper = RuntimeError("provider request failed")
    wrapper.__cause__ = cause

    result = classify_api_error(wrapper)

    assert result.reason == FailoverReason.decode_error


def test_deterministic_signature_is_exact_not_fuzzy():
    first = httpx.DecodingError(_DECODE_MESSAGE)
    same = httpx.DecodingError(_DECODE_MESSAGE)
    different = httpx.DecodingError("brotli: decoder process cannot accept more data")

    first_sig = deterministic_error_signature(first, classify_api_error(first))
    same_sig = deterministic_error_signature(same, classify_api_error(same))
    different_sig = deterministic_error_signature(
        different, classify_api_error(different)
    )

    assert first_sig == same_sig
    assert first_sig != different_sig


def test_identical_decode_failure_does_not_burn_each_fallback_retry_budget():
    result, calls = _run_decode_chain()

    assert result["completed"] is True
    assert result["final_response"] == "Recovered after corrupt routes"
    assert calls == [
        ("zai", "glm-primary"),
        ("zai", "glm-fallback-1"),
        ("zai", "glm-fallback-2"),
    ]


def test_repeated_signature_breaks_route_budget_if_retryability_regresses():
    """Cross-route state is a real breaker, not just classifier metadata."""

    def accidentally_retryable(error, **kwargs):
        result = classify_api_error(error, **kwargs)
        if result.reason == FailoverReason.decode_error:
            result.retryable = True
            result.should_fallback = False
        return result

    result, calls = _run_decode_chain(accidentally_retryable)

    assert result["completed"] is True
    assert calls == [
        # The first route gets one normal retry before repetition proves the
        # signature deterministic. Every later route gets one attempt only.
        ("zai", "glm-primary"),
        ("zai", "glm-primary"),
        ("zai", "glm-fallback-1"),
        ("zai", "glm-fallback-2"),
    ]
