"""Regression coverage for malformed HTTP-200 provider streams.

A provider can accept a request and return HTTP 200 while sending bytes that are
not valid JSON/SSE.  The stream parser then raises a plain ``ValueError``.  That
error originates on the response side and must use the retry/fallback transport
path; a request-serialization ``ValueError`` must still abort immediately.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pydantic_core import from_json

from agent.error_classifier import FailoverReason
from run_agent import AIAgent


_MALFORMED_SSE_BODY = b"\ndata: not-json"


class _Malformed200Stream:
    """Opened provider stream whose first non-empty SSE line is not JSON."""

    response = SimpleNamespace(
        status_code=200,
        headers={"content-type": "text/event-stream"},
    )
    body = _MALFORMED_SSE_BODY

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        # This is the live incident's parser family and exact exception shape:
        # ValueError("expected value at line 2 column 1").
        from_json(self.body)
        yield  # pragma: no cover - keeps this method an iterator

    def close(self):
        return None


def _response(content: str):
    message = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="fallback/model", usage=None)


def _make_agent(statuses: list[tuple[str, str]]) -> AIAgent:
    fallback = [
        {
            "provider": "openrouter",
            "model": "fallback/model",
            "base_url": "https://fallback.example/v1",
        }
    ]
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="primary-key",
            base_url="https://provider.example/anthropic",
            provider="claude-apr",
            model="claude-test",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback,
            status_callback=lambda kind, message: statuses.append((kind, message)),
        )

    agent.api_mode = "anthropic_messages"
    agent.client = None
    agent._api_max_retries = 3
    agent._anthropic_client = MagicMock()
    agent._anthropic_client.messages.stream.side_effect = (
        lambda **_kwargs: _Malformed200Stream()
    )
    agent._create_request_anthropic_client = (
        lambda *_args, **_kwargs: agent._anthropic_client
    )
    return agent


def _fallback_client() -> MagicMock:
    client = MagicMock()
    client.api_key = "fallback-key"
    client.base_url = "https://fallback.example/v1"
    client._custom_headers = None
    client.default_headers = None
    return client


def test_http_200_malformed_stream_retries_then_fails_over_with_reason(monkeypatch):
    """The real streaming + conversation paths retry once, then change route."""
    statuses: list[tuple[str, str]] = []
    agent = _make_agent(statuses)
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")

    fallback_response = _response("Recovered through fallback")
    with (
        patch.object(agent, "_interruptible_api_call", return_value=fallback_response),
        patch.object(agent, "_try_activate_fallback", wraps=agent._try_activate_fallback) as activate,
        patch.object(agent, "_try_recover_primary_transport", return_value=False),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_fallback_client(), "fallback/model"),
        ),
        patch(
            "hermes_cli.model_normalize.normalize_model_for_provider",
            side_effect=lambda model, _provider: model,
        ),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
        patch(
            "hermes_cli.config.read_raw_config",
            return_value={"model": {"announce_route_change": True}},
        ),
        patch("agent.conversation_loop.jittered_backoff", return_value=0.0),
    ):
        result = agent.run_conversation("hello")

    # One primary attempt plus one retry.  The next call is served by fallback.
    assert agent._anthropic_client.messages.stream.call_count == 2
    assert result["completed"] is True
    assert result["final_response"] == "Recovered through fallback"
    assert agent._fallback_activated is True

    activate.assert_called_once()
    assert activate.call_args.kwargs["reason"] is FailoverReason.stream_parse

    announcement = " || ".join(message for _kind, message in statuses)
    assert "🔄 Model fallback (malformed stream):" in announcement
    assert "expected value at line 2 column 1" not in announcement


def test_request_serialization_valueerror_remains_nonretryable(monkeypatch):
    """A local ValueError before any HTTP response still aborts on attempt one."""
    statuses: list[tuple[str, str]] = []
    agent = _make_agent(statuses)
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
    agent._anthropic_client.messages.stream.side_effect = ValueError(
        "request payload is not JSON serializable"
    )

    with (
        patch.object(agent, "_try_activate_fallback", wraps=agent._try_activate_fallback) as activate,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.conversation_loop.jittered_backoff", return_value=0.0),
    ):
        result = agent.run_conversation("hello")

    assert result["failed"] is True
    assert agent._anthropic_client.messages.stream.call_count == 1
    activate.assert_not_called()
    assert agent._fallback_activated is False


def test_parser_shaped_valueerror_without_http_response_is_not_stream_fault():
    """The parser message alone is insufficient without response provenance."""
    statuses: list[tuple[str, str]] = []
    agent = _make_agent(statuses)
    error = ValueError("expected value at line 2 column 1")

    assert agent._is_provider_stream_parse_error(error) is False
    assert agent._is_provider_stream_parse_error(error, http_status=200) is True
