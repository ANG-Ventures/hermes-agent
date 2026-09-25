from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent.error_classifier import FailoverReason
from run_agent import AIAgent


def _make_agent(fallbacks):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="http://127.0.0.1:18810/anthropic",
            provider="claude-apr",
            model="claude-fable-5",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallbacks,
        )
    agent.client = MagicMock()
    agent._retry_status_buffer = []
    return agent


def _client(base_url):
    client = MagicMock()
    client.base_url = base_url
    client.api_key = "test-key"
    return client


def _fallbacks():
    return [
        {"provider": "claude-apx-1", "model": "claude-opus-5"},
        {"provider": "claude-apx-2", "model": "claude-opus-5"},
        {"provider": "claude-apx-3", "model": "claude-opus-5"},
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},
    ]


def test_confirmed_tailscale_stop_skips_every_tailnet_rung_and_uses_cross_transport(
    monkeypatch,
):
    from agent import shared_transport_guard as guard

    agent = _make_agent(_fallbacks())
    clients = [
        _client("http://100.84.177.69:18801/anthropic"),
        _client("http://100.100.218.3:18801/anthropic"),
        _client("http://100.102.189.29:18801/anthropic"),
        _client("https://api.openai.com/v1"),
    ]
    monkeypatch.setattr(
        guard,
        "tailscale_status_down",
        lambda: (True, "backend_state=Stopped"),
    )

    with patch(
        "agent.auxiliary_client.resolve_provider_client",
        side_effect=[(client, entry["model"]) for client, entry in zip(clients, _fallbacks())],
    ):
        assert agent._try_activate_fallback(reason=FailoverReason.timeout) is True

    assert agent.provider == "openai-codex"
    assert agent.model == "gpt-5.6-sol"
    assert agent._fallback_index == 4
    assert agent._last_fallback_event["reason"] == FailoverReason.tailscale_down.value
    assert agent._last_fallback_event["reason_label"] == "Tailscale down"
    assert all(client.close.called for client in clients[:3])
    buffered = [message for _kind, message in agent._retry_status_buffer]
    assert sum("Tailscale down" in message for message in buffered) == 1
    assert any("4 tailnet routes" in message for message in buffered)
    assert not any("Primary model failed" in message for message in buffered)


def test_tailscale_healthy_preserves_single_provider_connection_drop_semantics(monkeypatch):
    from agent import shared_transport_guard as guard

    agent = _make_agent(_fallbacks()[:2])
    monkeypatch.setattr(
        guard,
        "tailscale_status_down",
        lambda: (False, "backend_state=Running"),
    )

    with patch(
        "agent.auxiliary_client.resolve_provider_client",
        return_value=(
            _client("http://100.84.177.69:18801/anthropic"),
            "claude-opus-5",
        ),
    ):
        assert agent._try_activate_fallback(reason=FailoverReason.timeout) is True

    assert agent.provider == "claude-apx-1"
    assert agent._fallback_index == 1
    event = agent._last_fallback_event
    assert event["reason"] == FailoverReason.timeout.value
    assert event["reason_label"] == "connection dropped"
    assert not any(
        "Tailscale" in message for _kind, message in agent._retry_status_buffer
    )


def test_tailscale_indeterminate_does_not_infer_outage_from_one_timeout(monkeypatch):
    from agent import shared_transport_guard as guard

    agent = _make_agent(_fallbacks()[:1])
    monkeypatch.setattr(
        guard,
        "tailscale_status_down",
        lambda: (None, "cli_unavailable"),
    )

    with patch(
        "agent.auxiliary_client.resolve_provider_client",
        return_value=(
            _client("http://100.84.177.69:18801/anthropic"),
            "claude-opus-5",
        ),
    ):
        assert agent._try_activate_fallback(reason=FailoverReason.timeout) is True

    assert agent.provider == "claude-apx-1"
    assert agent._last_fallback_event["reason"] == FailoverReason.timeout.value


def test_confirmed_stop_fails_before_any_model_request(monkeypatch):
    from agent import shared_transport_guard as guard

    agent = _make_agent(_fallbacks()[:1])
    agent.client.chat.completions.create.side_effect = AssertionError(
        "model request must not fire while Tailscale is explicitly stopped"
    )
    agent._anthropic_client = MagicMock()
    agent._anthropic_client.messages.stream.side_effect = AssertionError(
        "Anthropic request must not fire while Tailscale is explicitly stopped"
    )
    monkeypatch.setattr(
        guard,
        "tailscale_status_down",
        lambda: (True, "backend_state=Stopped"),
    )

    with patch(
        "agent.auxiliary_client.resolve_provider_client",
        return_value=(
            _client("http://100.84.177.69:18801/anthropic"),
            "claude-opus-5",
        ),
    ):
        result = agent.run_conversation("hello", system_message="test system")

    assert result["failed"] is True
    assert result["shared_transport_unavailable"] == "tailscale"
    agent.client.chat.completions.create.assert_not_called()
    agent._anthropic_client.messages.stream.assert_not_called()
    assert agent._retry_status_buffer == []
    assert result["final_response"] == (
        "Tailscale is down on the gateway host. Reconnect Tailscale or use a "
        "non-tailnet provider."
    )


def test_repeat_outage_reports_once_per_turn_with_fresh_route_count(monkeypatch):
    from agent import shared_transport_guard as guard

    agent = _make_agent(_fallbacks()[:1])
    monkeypatch.setattr(
        guard,
        "tailscale_status_down",
        lambda: (True, "backend_state=Stopped"),
    )

    with (
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(
                _client("http://100.84.177.69:18801/anthropic"),
                "claude-opus-5",
            ),
        ),
        patch.object(guard.logger, "warning") as warning,
    ):
        first = agent.run_conversation("first", system_message="test system")
        second = agent.run_conversation("second", system_message="test system")

    assert first["shared_transport_unavailable"] == "tailscale"
    assert second["shared_transport_unavailable"] == "tailscale"
    assert warning.call_count == 2
    assert [call.args[2] for call in warning.call_args_list] == [2, 1]
    assert agent._shared_transport_affected_routes == {
        "claude-apr/claude-fable-5",
    }


def test_tailscale_reason_rider_is_two_words():
    from agent.chat_completion_helpers import _fallback_reason_label

    assert _fallback_reason_label(FailoverReason.tailscale_down) == "Tailscale down"
