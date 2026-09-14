"""Run the real gateway resolution/capture/footer path with offline agents.

Only conversation generation and external dependencies are replaced; runtime
provider resolution, AIAgent construction, TurnRunner and the final footer
producer execute normally. Fallback tests additionally run the real fallback
activation (not a simulation of its reasoning assignment).
"""

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
import hermes_cli.config as cli_config
import hermes_cli.runtime_provider as runtime_provider
import run_agent
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource


@pytest.fixture
def footer_turn(monkeypatch, tmp_path):
    config = {
        "model": {"provider": "claude-bridge-f3", "default": "claude-opus-4-8"},
        "custom_providers": [{
            "name": "claude-bridge-f3",
            "model": "claude-opus-4-8",
            "api_key": "test-key",
            "base_url": "https://bridge.example.invalid/v1",
            "api_mode": "chat_completions",
        }],
        "display": {
            "runtime_footer": {"enabled": True, "fields": ["provider_model"]},
            "tool_progress": "off", "thinking_progress": False,
        },
        "agent": {"reasoning_effort": "low"},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
    }
    # Substitute the configuration source, not the provider/reasoning resolvers.
    monkeypatch.setattr(cli_config, "load_config", lambda *a, **kw: config)
    monkeypatch.setattr(runtime_provider, "load_config", lambda *a, **kw: config)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: config)
    monkeypatch.setattr(gateway_run, "_load_gateway_runtime_config", lambda: config)
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *a, **kw: None)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(run_agent, "OpenAI", MagicMock())
    monkeypatch.setattr(run_agent, "get_tool_definitions", lambda *a, **kw: [])
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda *a, **kw: {})
    monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *a, **kw: 100_000)
    monkeypatch.setattr("agent.context_compressor.get_model_context_length", lambda *a, **kw: 100_000)
    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda *a, **kw: set())
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    runner: Any = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {}
    runner._session_db = None
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_session_run_current = lambda _key, _gen: True
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *a, **kw: False
    runner._get_or_create_gateway_honcho = lambda _key: (None, None)
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.loaded_hooks = []
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="footer-test", chat_type="dm",
        user_id="footer-user",
    )
    session_key = runner._session_key_for_source(source)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key=session_key, session_id="footer-test-session",
        created_at=datetime.now(), updated_at=datetime.now(),
        platform=Platform.TELEGRAM, chat_type="dm",
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.get_model_override.return_value = None

    agents = []
    results = []
    before_response = []
    response_text = ["done"]

    def offline_conversation(agent, user_message, conversation_history=None, **kwargs):
        agents.append(agent)
        for callback in before_response:
            callback(agent)
        return {
            "final_response": response_text[0],
            "messages": [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": response_text[0]},
            ],
            "api_calls": 1, "failed": False,
        }

    monkeypatch.setattr(run_agent.AIAgent, "run_conversation", offline_conversation)
    real_run_sync = gateway_run.TurnRunner.run_sync

    def capture_result(turn_runner):
        result = real_run_sync(turn_runner)
        results.append(result)
        return result

    monkeypatch.setattr(gateway_run.TurnRunner, "run_sync", capture_result)

    async def run_turn():
        import yaml
        (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
        event = MessageEvent(text="hello", source=source, message_id="footer-message")
        return await runner._handle_message_with_agent(event, source, session_key, 1)

    yield config, runner, session_key, run_turn, agents, results, before_response, response_text
    for agent in agents:
        agent.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["claude-bridge-f3", "claude-bridge-f4"])
async def test_named_custom_provider_footer_uses_resolved_requested_name(footer_turn, name):
    config, runner, key, run_turn, agents, results, callbacks, response_text = footer_turn
    config["model"]["provider"] = name
    config["custom_providers"][0]["name"] = name
    response = await run_turn()
    assert len(agents) == len(results) == 1
    assert agents[0].provider == "custom"
    assert agents[0].requested_provider == name
    assert response == f"done\n\n{name}/claude-opus-4-8"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_reason", ["auth", "rate_limit"])
@pytest.mark.parametrize("final_response", ["done", ""])
async def test_fallback_footer_uses_actual_agent_reasoning(
    footer_turn, monkeypatch, failure_reason, final_response,
):
    from agent.error_classifier import FailoverReason

    config, runner, key, run_turn, agents, results, callbacks, response_text = footer_turn
    config["display"]["runtime_footer"]["fields"] = ["reasoning"]
    config["fallback_providers"] = [{
        "provider": "openrouter", "model": "openai/gpt-5.4",
        "base_url": "https://openrouter.ai/api/v1", "api_key": "test-key",
        "api_mode": "chat_completions",
    }]
    runner._set_session_reasoning_override(key, {"enabled": True, "effort": "high"})
    response_text[0] = final_response
    fallback_client = MagicMock()
    fallback_client.api_key = "test-key"
    fallback_client.base_url = "https://openrouter.ai/api/v1"
    fallback_client._custom_headers = {}
    fallback_client.default_headers = {}
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *args, **kwargs: (fallback_client, "openai/gpt-5.4"),
    )

    def activate_fallback(agent):
        # The real gateway applies the session override before running the agent.
        assert agent.reasoning_config == {"enabled": True, "effort": "high"}
        assert agent._try_activate_fallback(FailoverReason(failure_reason)) is True
        assert agent.reasoning_config == {"enabled": True, "effort": "low"}
        # Confirm the request builder uses this same effective value.
        kwargs = agent._build_api_kwargs([{"role": "user", "content": "hello"}])
        assert kwargs["extra_body"]["reasoning"]["effort"] == "low"

    callbacks.append(activate_fallback)
    response = await run_turn()
    assert len(agents) == len(results) == 1
    assert agents[0].model == "openai/gpt-5.4"
    assert runner._resolve_session_reasoning_config(session_key=key) == {
        "enabled": True, "effort": "high",
    }
    assert response.endswith("\n\nr:low")
    assert results[0]["reasoning_config"] == agents[0].reasoning_config
    assert results[0]["reasoning_config"] is not agents[0].reasoning_config


@pytest.mark.asyncio
@pytest.mark.parametrize("effective,expected", [(None, "done"), ({}, "done"), ({"enabled": False}, "done\n\nr:none")])
async def test_footer_does_not_guess_reasoning_when_agent_has_no_effort(
    footer_turn, effective, expected,
):
    config, runner, key, run_turn, agents, results, callbacks, response_text = footer_turn
    config["display"]["runtime_footer"]["fields"] = ["reasoning"]
    runner._set_session_reasoning_override(key, {"enabled": True, "effort": "high"})
    callbacks.append(lambda agent: setattr(agent, "reasoning_config", effective))
    response = await run_turn()
    assert len(agents) == len(results) == 1
    assert response == expected


@pytest.mark.asyncio
async def test_default_gateway_footer_ignores_new_runtime_metadata(footer_turn):
    import os

    config, runner, key, run_turn, agents, results, callbacks, response_text = footer_turn
    config["display"]["runtime_footer"].pop("fields")
    runner._set_session_reasoning_override(key, {"enabled": True, "effort": "high"})
    response = await run_turn()
    assert len(agents) == len(results) == 1
    assert response.encode() == (
        "done\n\nclaude-opus-4-8 · 0% · " + os.environ["TERMINAL_CWD"]
    ).encode()


@pytest.mark.asyncio
async def test_stable_session_reasoning_footer_matches_agent(footer_turn):
    config, runner, key, run_turn, agents, results, callbacks, response_text = footer_turn
    config["display"]["runtime_footer"]["fields"] = ["reasoning"]
    runner._set_session_reasoning_override(key, {"enabled": True, "effort": "high"})
    response = await run_turn()
    assert agents[0].reasoning_config == {"enabled": True, "effort": "high"}
    assert response == "done\n\nr:high"
