"""End-to-end replay of the 2026-08-17 multimodal 413 incident chain.

This module intentionally composes the real agent request builder, byte-budget
preflight, fallback rebuild, route-change producer, and gateway status delivery
seam. Only the two network edges are fakes: an Anthropic-shaped provider and a
platform adapter.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.request_body_budget import serialized_request_body_size
from gateway.config import Platform
from gateway.run import TurnRunner
from gateway.turn_context import TurnContext
from run_agent import AIAgent

_HARD_PROVIDER_CAP = 10 * 1024 * 1024
_PRIMARY_PROVIDER = "incident-primary"
_FALLBACK_PROVIDER = "incident-fallback"
_MODEL = "claude-opus-4-6"


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))


class _ProviderHTTPError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.response = SimpleNamespace(
            status_code=status_code,
            headers={},
            text=message,
        )


class _FakeStream:
    def __init__(self, *, response=None, error: BaseException | None = None):
        self.response = SimpleNamespace(status_code=200, headers={})
        self._response = response
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        return iter(())

    def get_final_message(self):
        if self._error is not None:
            raise self._error
        return self._response


class _FakeMessages:
    def __init__(self, provider: "_CappedAnthropicProvider"):
        self._provider = provider

    def create(self, **kwargs):
        return self._provider.create(kwargs)

    def stream(self, **kwargs):
        return self._provider.stream(kwargs)


class _CappedAnthropicProvider:
    """Anthropic-shaped provider that hard-rejects bodies above 10 MiB."""

    def __init__(self, route: str, *, behavior: str):
        self.route = route
        self.behavior = behavior
        self.base_url = f"https://{route}.invalid/anthropic"
        self.api_key = "test-key"  # gitleaks:allow
        self.messages = _FakeMessages(self)
        self.requests: list[dict] = []
        self.body_sizes: list[int] = []
        self.status_413_count = 0
        self._stream_calls = 0

    def _record(self, kwargs: dict) -> None:
        self.requests.append(kwargs)
        size = serialized_request_body_size(kwargs)
        self.body_sizes.append(size)
        if size > _HARD_PROVIDER_CAP:
            self.status_413_count += 1
            raise _ProviderHTTPError(413, "request body exceeds 10 MiB")

    @staticmethod
    def _success_response(text: str):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason="end_turn",
            stop_sequence=None,
            model=_MODEL,
            usage=None,
        )

    def create(self, kwargs: dict):
        self._record(kwargs)
        if self.behavior == "always_503":
            raise _ProviderHTTPError(503, "primary unavailable")
        return self._success_response("Recovered through the fallback route")

    def stream(self, kwargs: dict):
        self._record(kwargs)
        self._stream_calls += 1
        if self.behavior == "malformed_then_503":
            if self._stream_calls == 1:
                return _FakeStream(
                    error=ValueError("expected value at line 2 column 1")
                )
            raise _ProviderHTTPError(503, "primary unavailable after malformed stream")
        if self.behavior == "always_503":
            raise _ProviderHTTPError(503, "primary unavailable")
        return _FakeStream(
            response=self._success_response("Recovered through the fallback route")
        )

    def close(self):
        return None


class _RecordingAdapter:
    def __init__(self):
        self.send_calls: list[tuple[str, str, dict | None]] = []
        self.update_calls: list[tuple[str, str, str, dict | None]] = []

    async def send(self, chat_id, content, metadata=None):
        self.send_calls.append((chat_id, content, metadata))
        return SimpleNamespace(success=True, message_id="route-1", error=None)

    async def send_or_update_status(
        self, chat_id, status_key, content, *, metadata=None
    ):
        self.update_calls.append((chat_id, status_key, content, metadata))
        return SimpleNamespace(success=True, message_id="status-1", error=None)


class _GatewayOwner:
    def __init__(self, adapter):
        self.adapter = adapter

    def _adapter_for_source(self, _source):
        return self.adapter


def _gateway_turn_runner(adapter, loop) -> TurnRunner:
    source = SimpleNamespace(platform=Platform.TELEGRAM, chat_id="incident-chat")
    owner = _GatewayOwner(adapter)
    context = TurnContext(
        source=source,
        _run_still_current=lambda: True,
        _loop_for_step=loop,
        _status_adapter=adapter,
        _current_status_adapter=lambda: owner._adapter_for_source(source),
        _status_chat_id=source.chat_id,
        _status_thread_metadata={"thread_id": "incident-thread"},
    )
    return TurnRunner(owner, context)  # type: ignore[arg-type]


def _multimodal_history() -> tuple[list[dict], list[str]]:
    image_urls = ["data:image/png;base64," + (marker * 3_500_000) for marker in "abcd"]
    history: list[dict] = []
    for index, image_url in enumerate(image_urls):
        call_id = f"call_vision_{index}"
        history.extend([
            {"role": "user", "content": f"Inspect screenshot {index}."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "vision_analyze",
                            "arguments": "{}",
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": "vision_analyze",
                "content": [
                    {
                        "type": "text",
                        "text": f"Screenshot {index} text summary",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url},
                    },
                ],
            },
            {
                "role": "assistant",
                "content": f"Screenshot {index} inspected.",
            },
        ])
    return history, image_urls


def _make_agent(*, stream: bool) -> AIAgent:
    with (
        patch("agent.model_metadata.get_model_context_length", return_value=256_000),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",  # gitleaks:allow
            base_url="https://bootstrap.invalid/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    agent.provider = _PRIMARY_PROVIDER
    agent.requested_provider = _PRIMARY_PROVIDER
    agent.model = _MODEL
    agent.api_mode = "anthropic_messages"
    agent.base_url = f"https://{_PRIMARY_PROVIDER}.invalid/anthropic"
    agent._anthropic_base_url = agent.base_url
    agent._anthropic_api_key = "test-key"  # gitleaks:allow
    agent._is_anthropic_oauth = False
    agent._disable_streaming = not stream
    agent._cached_system_prompt = "You are the incident-chain test agent."
    agent._use_prompt_caching = False
    agent.compression_enabled = True
    agent.save_trajectories = False
    agent._fallback_activated = False
    agent._fallback_index = 0
    agent._fallback_chain = [
        {
            "provider": _FALLBACK_PROVIDER,
            "model": _MODEL,
            "base_url": f"https://{_FALLBACK_PROVIDER}.invalid/anthropic",
            "api_key": "test-key",  # gitleaks:allow
        }
    ]
    agent._fallback_model = agent._fallback_chain[0]
    agent._primary_runtime = {
        "provider": _PRIMARY_PROVIDER,
        "model": _MODEL,
        "base_url": agent.base_url,
        "api_mode": "anthropic_messages",
    }
    return agent


def _install_provider_seams(
    monkeypatch,
    agent: AIAgent,
    primary: _CappedAnthropicProvider,
    fallback: _CappedAnthropicProvider,
) -> None:
    def _active_request_client(**_kwargs):
        return fallback if agent.provider == _FALLBACK_PROVIDER else primary

    monkeypatch.setattr(
        agent, "_create_request_anthropic_client", _active_request_client
    )
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *_args, **_kwargs: (fallback, _MODEL),
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter.build_anthropic_client",
        lambda *_args, **_kwargs: fallback,
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 256_000,
    )
    monkeypatch.setattr(
        "agent.auxiliary_client.set_runtime_main", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr("run_agent.jittered_backoff", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)


async def _run_with_gateway_status(
    agent: AIAgent,
    adapter: _RecordingAdapter,
    history: list[dict],
) -> dict:
    runner = _gateway_turn_runner(adapter, asyncio.get_running_loop())
    agent.status_callback = runner._status_callback_sync
    result = await asyncio.to_thread(
        agent.run_conversation,
        "Continue after the provider interruption.",
        conversation_history=history,
    )
    for _ in range(100):
        if any("Model fallback" in content for _, content, _ in adapter.send_calls):
            break
        await asyncio.sleep(0.01)
    return result


def _route_change_messages(adapter: _RecordingAdapter) -> list[str]:
    return [
        content for _, content, _ in adapter.send_calls if "Model fallback" in content
    ]


def test_413_incident_chain_recovers_once_without_413_or_compression_stall(
    monkeypatch, caplog
):
    history, original_image_urls = _multimodal_history()
    assert serialized_request_body_size({"messages": history}) > _HARD_PROVIDER_CAP

    primary = _CappedAnthropicProvider(_PRIMARY_PROVIDER, behavior="always_503")
    fallback = _CappedAnthropicProvider(_FALLBACK_PROVIDER, behavior="success")
    agent = _make_agent(stream=False)
    _install_provider_seams(monkeypatch, agent, primary, fallback)
    adapter = _RecordingAdapter()

    with (
        patch.object(agent, "_model_supports_vision", return_value=True),
        patch.object(
            agent,
            "_compress_context",
            side_effect=AssertionError("legacy 413 compression path must not run"),
        ) as compress,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        caplog.at_level("INFO", logger="agent.conversation_compression"),
    ):
        result = asyncio.run(_run_with_gateway_status(agent, adapter, history))

    assert result["completed"] is True
    assert result["final_response"] == "Recovered through the fallback route"
    assert primary.body_sizes
    assert primary.body_sizes[0] <= _HARD_PROVIDER_CAP
    assert fallback.body_sizes and fallback.body_sizes[0] <= _HARD_PROVIDER_CAP
    assert primary.status_413_count == 0
    assert fallback.status_413_count == 0
    assert agent._last_fallback_event["old_provider"] == _PRIMARY_PROVIDER
    assert agent._last_fallback_event["new_provider"] == _FALLBACK_PROVIDER

    route_messages = _route_change_messages(adapter)
    assert len(route_messages) == 1
    assert f"{_PRIMARY_PROVIDER}/{_MODEL}" in route_messages[0]
    assert f"{_FALLBACK_PROVIDER}/{_MODEL}" in route_messages[0]

    for index in range(4):
        assert f"Screenshot {index} text summary" in str(
            fallback.requests[0]["messages"]
        )
    assert all(image_url in str(history) for image_url in original_image_urls)

    compress.assert_not_called()
    no_progress_attempts = [
        record.getMessage()
        for record in caplog.records
        if "compression_attempt" in record.getMessage()
        and '"failure_class":"no_progress"' in record.getMessage()
    ]
    assert no_progress_attempts == []


def test_malformed_200_stream_retries_then_fails_over(monkeypatch):
    primary = _CappedAnthropicProvider(_PRIMARY_PROVIDER, behavior="malformed_then_503")
    fallback = _CappedAnthropicProvider(_FALLBACK_PROVIDER, behavior="success")
    agent = _make_agent(stream=True)
    _install_provider_seams(monkeypatch, agent, primary, fallback)
    adapter = _RecordingAdapter()

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = asyncio.run(_run_with_gateway_status(agent, adapter, []))

    assert primary._stream_calls >= 2
    assert result["completed"] is True
    assert result["final_response"] == "Recovered through the fallback route"
    assert len(_route_change_messages(adapter)) == 1
    assert primary.status_413_count == 0
    assert fallback.status_413_count == 0
