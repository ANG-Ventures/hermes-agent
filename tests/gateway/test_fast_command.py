"""Tests for gateway /fast support and Priority Processing routing."""

import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

import gateway.run as gateway_run
from gateway.config import ChannelOverride, GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


class _CapturingAgent:
    last_init = None
    last_run = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.tools = []

    def run_conversation(
        self,
        user_message,
        conversation_history=None,
        task_id=None,
        persist_user_message=None,
        persist_user_timestamp=None,
    ):
        type(self).last_run = {
            "user_message": user_message,
            "conversation_history": conversation_history,
            "task_id": task_id,
            "persist_user_message": persist_user_message,
            "persist_user_timestamp": persist_user_timestamp,
        }
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
            "completed": True,
        }


def _install_fake_agent(monkeypatch):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._service_tier = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._session_model_overrides = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(streaming=None)
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-1"),
        load_transcript=lambda session_id: [],
    )
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner._enrich_message_with_vision = AsyncMock(return_value="ENRICHED")
    return runner


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="user-1",
    )


def _make_discord_auto_thread_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="999",
        chat_type="thread",
        user_id="user-1",
        thread_id="999",
        parent_chat_id="100",
        auto_thread_created=True,
        auto_thread_initial_name="raw user prompt",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def test_turn_route_injects_priority_processing_without_changing_runtime():
    runner = _make_runner()
    runner._service_tier = "priority"
    runtime_kwargs = {
        "api_key": "***",
        "base_url": "https://api.openai.com/v1",
        "provider": "openai-api",
        "api_mode": "codex_responses",
        "command": None,
        "args": [],
        "credential_pool": None,
    }

    route = gateway_run.GatewayRunner._resolve_turn_agent_config(runner, "hi", "gpt-5.4", runtime_kwargs)

    assert route["runtime"]["provider"] == "openai-api"
    assert route["runtime"]["api_mode"] == "codex_responses"
    assert route["request_overrides"] == {"service_tier": "priority"}


def test_turn_route_fails_closed_for_proxy_gpt_model():
    runner = _make_runner()
    runner._service_tier = "priority"
    runtime_kwargs = {
        "api_key": "***",
        "base_url": "https://openrouter.ai/api/v1",
        "provider": "openrouter",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
    }

    route = gateway_run.GatewayRunner._resolve_turn_agent_config(
        runner, "hi", "gpt-5.6-sol", runtime_kwargs
    )

    assert route["request_overrides"] == {}


@pytest.mark.parametrize(
    ("provider", "api_mode", "model", "expected"),
    [
        (
            "openai-codex",
            "codex_responses",
            "gpt-5.5",
            {"service_tier": "fast"},
        ),
        (
            "anthropic",
            "anthropic_messages",
            "claude-opus-4-6",
            {"speed": "fast"},
        ),
    ],
)
def test_turn_route_serializes_provider_specific_fast_override(
    provider, api_mode, model, expected
):
    runner = _make_runner()
    runner._service_tier = "priority"
    runtime_kwargs = {
        "api_key": "runtime-secret",
        "base_url": "https://example.invalid",
        "provider": provider,
        "api_mode": api_mode,
        "command": None,
        "args": [],
        "credential_pool": None,
    }

    route = gateway_run.GatewayRunner._resolve_turn_agent_config(
        runner, "hi", model, runtime_kwargs
    )

    assert route["request_overrides"] == expected


def test_turn_route_skips_priority_processing_for_unsupported_models():
    runner = _make_runner()
    runner._service_tier = "priority"
    runtime_kwargs = {
        "api_key": "***",
        "base_url": "https://openrouter.ai/api/v1",
        "provider": "openrouter",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
    }

    route = gateway_run.GatewayRunner._resolve_turn_agent_config(runner, "hi", "gpt-5.3-codex", runtime_kwargs)

    assert route["request_overrides"] == {}


@pytest.mark.asyncio
async def test_handle_fast_command_persists_config(monkeypatch, tmp_path):
    runner = _make_runner()

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    runner._resolve_effective_session_route_identity = MagicMock(
        return_value=("gpt-5.4", "openai-api", "codex_responses")
    )

    response = await runner._handle_fast_command(_make_event("/fast fast"))

    assert "FAST" in response
    assert runner._service_tier == "priority"

    saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert saved["agent"]["service_tier"] == "fast"


@pytest.mark.asyncio
async def test_fast_status_uses_persisted_session_route_without_credentials(
    monkeypatch, tmp_path
):
    runner = _make_runner()
    source = _make_source()
    session_key = runner._session_key_for_source(source)
    persisted = {
        "model": "gpt-5.6-sol",
        "provider": "openai-codex",
        "api_mode": "codex_responses",
    }
    runner.session_store = SimpleNamespace(
        entry_for=lambda key: SimpleNamespace(model_override_identity=persisted)
        if key == session_key
        else None,
    )
    runner._session_model_overrides[session_key] = {
        **persisted,
        "api_key": "must-not-be-read",
    }

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "model": {
                "default": "claude-opus-4-8",
                "provider": "anthropic",
                "api_mode": "anthropic_messages",
            }
        },
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        MagicMock(side_effect=AssertionError("credential checkout during /fast status")),
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs_for_provider",
        MagicMock(side_effect=AssertionError("provider checkout during /fast status")),
    )

    response = await runner._handle_fast_command(_make_event("/fast status"))

    assert "openai-codex/gpt-5.6-sol" in response
    assert "Codex Fast" in response
    assert "GPT-5.5" in response
    assert "claude" not in response.lower()


@pytest.mark.asyncio
async def test_fast_status_opus_48_proxy_points_to_separate_fast_model(
    monkeypatch, tmp_path
):
    runner = _make_runner()
    runner._resolve_effective_session_route_identity = MagicMock(
        return_value=(
            "claude-opus-4-8",
            "claude-apr",
            "anthropic_messages",
        )
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})

    response = await runner._handle_fast_command(_make_event("/fast status"))

    assert "claude-apr/claude-opus-4-8" in response
    assert "speed=fast" in response
    assert "claude-opus-4-8-fast" in response


def test_pure_route_identity_matches_runtime_precedence(monkeypatch):
    source = _make_source()
    session_key = "agent:main:telegram:dm:12345"
    config = {
        "model": {
            "default": "gpt-5.4",
            "provider": "openai-api",
            "api_mode": "codex_responses",
        }
    }

    runner = _make_runner()
    runner.config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                channel_overrides={
                    "12345": ChannelOverride(
                        model="gpt-5.5",
                        provider="openai-codex",
                    )
                }
            )
        }
    )
    runner.session_store = SimpleNamespace(entry_for=lambda _key: None)

    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openai-api",
            "api_mode": "codex_responses",
            "api_key": "global-key",
        },
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs_for_provider",
        lambda provider: {
            "provider": provider,
            "api_mode": "codex_responses",
            "api_key": "channel-key",
        },
    )

    pure = runner._resolve_effective_session_route_identity(
        source=source,
        session_key=session_key,
        user_config=config,
    )
    model, runtime = runner._resolve_session_agent_runtime(
        source=source,
        session_key=session_key,
        user_config=config,
    )

    assert pure == (model, runtime["provider"], runtime["api_mode"])
    assert pure == ("gpt-5.5", "openai-codex", "codex_responses")


def test_pure_route_identity_matches_runtime_for_session_override(monkeypatch):
    source = _make_source()
    runner = _make_runner()
    session_key = runner._session_key_for_source(source)
    identity = {
        "model": "gpt-5.6-sol",
        "provider": "openai-codex",
        "api_mode": "codex_responses",
    }
    runner.session_store = SimpleNamespace(
        entry_for=lambda _key: SimpleNamespace(model_override_identity=identity)
    )
    runner._session_model_overrides[session_key] = {
        **identity,
        "api_key": "session-secret",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "credential_pool": object(),
    }
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        MagicMock(side_effect=AssertionError("session override must win")),
    )
    config = {
        "model": {
            "default": "claude-opus-4-8",
            "provider": "anthropic",
            "api_mode": "anthropic_messages",
        }
    }

    pure = runner._resolve_effective_session_route_identity(
        source=source, session_key=session_key, user_config=config
    )
    model, runtime = runner._resolve_session_agent_runtime(
        source=source, session_key=session_key, user_config=config
    )

    assert pure == (model, runtime["provider"], runtime["api_mode"])
    assert pure == ("gpt-5.6-sol", "openai-codex", "codex_responses")


def test_pure_route_identity_matches_runtime_for_channel_and_session_override(
    monkeypatch,
):
    source = _make_source()
    runner = _make_runner()
    session_key = runner._session_key_for_source(source)
    identity = {
        "model": "claude-opus-4-6",
        "provider": "anthropic",
        "api_mode": "anthropic_messages",
    }
    runner.config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                channel_overrides={
                    "12345": ChannelOverride(
                        model="gpt-5.5",
                        provider="openai-codex",
                    )
                }
            )
        }
    )
    runner.session_store = SimpleNamespace(
        entry_for=lambda _key: SimpleNamespace(model_override_identity=identity)
    )
    runner._session_model_overrides[session_key] = {
        **identity,
        "api_key": "session-secret",
    }
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        MagicMock(side_effect=AssertionError("session override must win")),
    )
    config = {
        "model": {
            "default": "gpt-5.4",
            "provider": "openai-api",
            "api_mode": "codex_responses",
        }
    }

    pure = runner._resolve_effective_session_route_identity(
        source=source, session_key=session_key, user_config=config
    )
    model, runtime = runner._resolve_session_agent_runtime(
        source=source, session_key=session_key, user_config=config
    )

    assert pure == (model, runtime["provider"], runtime["api_mode"])
    assert pure == ("claude-opus-4-6", "anthropic", "anthropic_messages")


def test_missing_persisted_api_mode_does_not_rehydrate_matching_cached_route(
    monkeypatch,
):
    source = _make_source()
    runner = _make_runner()
    session_key = runner._session_key_for_source(source)
    persisted = {"model": "gpt-5.5", "provider": "openai-codex"}
    cached = {
        **persisted,
        "api_mode": "codex_responses",
        "api_key": "cached-secret",
    }
    runner.session_store = SimpleNamespace(
        entry_for=lambda _key: SimpleNamespace(model_override_identity=persisted)
    )
    runner._session_model_overrides[session_key] = cached
    reresolve = MagicMock(
        side_effect=AssertionError("matching route must not rehydrate credentials")
    )
    monkeypatch.setattr(runner, "_reresolve_model_override_credentials", reresolve)

    model, runtime = runner._resolve_session_agent_runtime(
        source=source,
        session_key=session_key,
        user_config={"model": {"default": "global-model"}},
    )

    assert model == "gpt-5.5"
    assert runtime["api_mode"] == "codex_responses"
    assert runner._session_model_overrides[session_key] is cached
    reresolve.assert_not_called()


def test_pure_route_identity_matches_runtime_for_global_route(monkeypatch):
    runner = _make_runner()
    runner.config = GatewayConfig()
    runner.session_store = SimpleNamespace(entry_for=lambda _key: None)
    config = {
        "model": {
            "default": "claude-opus-4-6",
            "provider": "anthropic",
            "api_mode": "anthropic_messages",
        }
    }
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "anthropic",
            "api_mode": "anthropic_messages",
            "api_key": "global-secret",
        },
    )

    pure = runner._resolve_effective_session_route_identity(
        source=_make_source(), session_key="global-key", user_config=config
    )
    model, runtime = runner._resolve_session_agent_runtime(
        source=_make_source(), session_key="global-key", user_config=config
    )

    assert pure == (model, runtime["provider"], runtime["api_mode"])


def test_model_switch_note_reports_enabled_fast_becoming_unavailable():
    runner = _make_runner()
    runner._service_tier = "priority"
    result = SimpleNamespace(
        new_model="gpt-5.6-sol",
        target_provider="openai-codex",
        api_mode="codex_responses",
    )

    note = runner._fast_unavailable_model_switch_row(result)

    assert "Fast: unavailable" in note
    assert "openai-codex/gpt-5.6-sol" in note
    assert "normal speed" in note


def test_model_switch_note_derives_api_mode_when_result_omits_it():
    runner = _make_runner()
    runner._service_tier = "priority"
    result = SimpleNamespace(
        new_model="gpt-5.5",
        target_provider="openai-codex",
    )

    assert runner._fast_unavailable_model_switch_row(result) is None


@pytest.mark.asyncio
async def test_run_agent_passes_priority_processing_to_gateway_agent(monkeypatch, tmp_path):
    _install_fake_agent(monkeypatch)
    runner = _make_runner()

    (tmp_path / "config.yaml").write_text("agent:\n  service_tier: fast\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    # ``_load_service_tier`` was refactored to call ``_load_gateway_runtime_config``
    # (which wraps ``_load_gateway_config`` plus env-expansion).  Since the test
    # stubs ``_load_gateway_config`` to ``{}``, also stub the runtime wrapper
    # directly so the priority routing assertions still exercise the live tier.
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_runtime_config",
        lambda: {"agent": {"service_tier": "fast"}},
    )
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openai-api",
            "api_mode": "codex_responses",
            "base_url": "https://api.openai.com/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})

    _CapturingAgent.last_init = None
    result = await runner._run_agent(
        message="hi",
        context_prompt="",
        history=[],
        source=_make_source(),
        session_id="session-1",
        session_key="agent:main:telegram:dm:12345",
    )

    assert result["final_response"] == "ok"
    assert _CapturingAgent.last_init["service_tier"] == "priority"
    assert _CapturingAgent.last_init["request_overrides"] == {"service_tier": "priority"}


@pytest.mark.asyncio
async def test_run_agent_passes_discord_auto_thread_title_callback(monkeypatch, tmp_path):
    _install_fake_agent(monkeypatch)
    runner = _make_runner()
    runner._session_db = SimpleNamespace(_db=MagicMock())  # type: ignore[assignment]

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_load_gateway_runtime_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})

    with patch("agent.title_generator.maybe_auto_title") as mock_title:
        await runner._run_agent(
            message="raw user prompt",
            context_prompt="",
            history=[],
            source=_make_discord_auto_thread_source(),
            session_id="session-1",
            session_key="agent:main:discord:thread:999",
        )

    mock_title.assert_called_once()
    callback = mock_title.call_args.kwargs["title_callback"]
    with patch.object(runner, "_schedule_discord_semantic_thread_rename") as mock_schedule:
        callback("Semantic Session Title")
    mock_schedule.assert_called_once()
    assert mock_schedule.call_args.args[1] == "session-1"
    assert mock_schedule.call_args.args[2] == "Semantic Session Title"


def test_session_source_preserves_discord_auto_thread_metadata():
    source = _make_discord_auto_thread_source()

    restored = SessionSource.from_dict(source.to_dict())

    assert restored.auto_thread_created is True
    assert restored.auto_thread_initial_name == "raw user prompt"
