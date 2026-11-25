"""Tests for per-channel model and system prompt overrides (Fixes #1955)."""

from unittest.mock import patch

import pytest

from gateway.config import (
    ChannelOverride,
    GatewayConfig,
    Platform,
    PlatformConfig,
)
from gateway.run import _get_channel_override, GatewayRunner
from gateway.session import SessionSource


class TestGetChannelOverride:


    def test_no_override_when_channel_not_in_overrides(self):
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "999": ChannelOverride(model="openrouter/healer-alpha"),
                    },
                ),
            },
        )
        assert _get_channel_override(config, Platform.DISCORD, "123") is None

    def test_returns_override_when_channel_matches(self):
        ov = ChannelOverride(
            model="openrouter/healer-alpha",
            provider="openrouter",
            system_prompt="You are a summarizer.",
        )
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={"1234567890": ov},
                ),
            },
        )
        result = _get_channel_override(config, Platform.DISCORD, "1234567890")
        assert result is not None
        assert result.model == "openrouter/healer-alpha"
        assert result.provider == "openrouter"
        assert result.system_prompt == "You are a summarizer."


    def test_thread_id_lookup_when_chat_id_misses(self):
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "thread_99": ChannelOverride(model="topic-model"),
                    },
                ),
            },
        )
        result = _get_channel_override(
            config, Platform.DISCORD, "parent_chan", thread_id="thread_99"
        )
        assert result is not None
        assert result.model == "topic-model"


class TestResolveModelForChannel:
    def test_uses_channel_override_when_present(self):
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "chan_1": ChannelOverride(model="anthropic/claude-opus-4.6"),
                    },
                ),
            },
        )
        runner = object.__new__(GatewayRunner)
        runner.config = config
        model = runner._resolve_model_for_channel(Platform.DISCORD, "chan_1")
        assert model == "anthropic/claude-opus-4.6"


class TestGetSystemPromptForChannel:
    def test_uses_channel_override_when_present(self):
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "chan_1": ChannelOverride(system_prompt="You are a coding assistant."),
                    },
                ),
            },
        )
        runner = object.__new__(GatewayRunner)
        runner.config = config
        runner._ephemeral_system_prompt = "Global prompt"
        prompt = runner._get_system_prompt_for_channel(Platform.DISCORD, "chan_1")
        assert prompt == "You are a coding assistant."


class TestResolveSessionAgentRuntimePriority:
    """Model/runtime priority: session /model → channel_overrides → global."""

    def test_channel_override_beats_global(self):
        runner = object.__new__(GatewayRunner)
        runner._session_model_overrides = {}
        runner.config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "chan_1": ChannelOverride(
                            model="channel/model",
                            provider="openrouter",
                        ),
                    },
                ),
            },
        )
        source = SessionSource(
            platform=Platform.DISCORD,
            chat_id="chan_1",
            user_id="u1",
        )
        with patch("gateway.run._resolve_gateway_model", return_value="global/model"), \
             patch("gateway.run._resolve_runtime_agent_kwargs", return_value={
                 "provider": "anthropic",
                 "api_key": "k",
                 "base_url": "https://api.anthropic.com",
                 "api_mode": "chat_completions",
             }), \
             patch(
                 "gateway.run._resolve_runtime_agent_kwargs_for_provider",
                 return_value={
                     "provider": "openrouter",
                     "api_key": "k2",
                     "base_url": "https://openrouter.ai/api/v1",
                     "api_mode": "chat_completions",
                 },
             ):
            model, runtime = runner._resolve_session_agent_runtime(
                source=source,
                user_config={"model": {"default": "global/model"}},
            )
        assert model == "channel/model"
        assert runtime["provider"] == "openrouter"




def _matrix_config(overrides):
    return GatewayConfig(platforms={
        Platform.MATRIX: PlatformConfig(enabled=True, channel_overrides=overrides),
        Platform.DISCORD: PlatformConfig(
            enabled=True, channel_overrides={"chan_1": ChannelOverride(model="discord/model")}),
    })


class TestWildcardChannelOverride:
    """``channel_overrides['*']`` is the platform's default (t_3639712d): Matrix -> Opus."""

    def test_wildcard_applies_when_no_key_matches(self):
        config = _matrix_config({"*": ChannelOverride(model="opus", provider="claude-alr")})
        ov = _get_channel_override(config, Platform.MATRIX, "!room:example.org")
        assert ov is not None and ov.model == "opus" and ov.provider == "claude-alr"
        runner = object.__new__(GatewayRunner)
        runner.config = config
        assert runner._resolve_model_for_channel(Platform.MATRIX, "!other:example.org") == "opus"

    def test_exact_thread_and_parent_ids_beat_wildcard(self):
        config = _matrix_config({
            "*": ChannelOverride(model="opus"),
            "!exact:x": ChannelOverride(model="exact-model"),
            "thread_1": ChannelOverride(model="thread-model"),
            "!parent:x": ChannelOverride(model="parent-model"),
        })
        assert _get_channel_override(config, Platform.MATRIX, "!exact:x").model == "exact-model"
        assert _get_channel_override(
            config, Platform.MATRIX, "!nomatch:x", thread_id="thread_1").model == "thread-model"
        assert _get_channel_override(
            config, Platform.MATRIX, "!nomatch:x", parent_id="!parent:x").model == "parent-model"
        assert _get_channel_override(config, Platform.MATRIX, "!nomatch:x").model == "opus"

    def test_platform_without_wildcard_unchanged(self):
        config = _matrix_config({"*": ChannelOverride(model="opus")})
        # Discord has no '*' row: a non-matching chat still gets no override, and Matrix's '*'
        # never leaks to another platform.
        assert _get_channel_override(config, Platform.DISCORD, "other") is None
        assert _get_channel_override(config, Platform.DISCORD, "chan_1").model == "discord/model"
        assert _get_channel_override(config, Platform.TELEGRAM, "123") is None

    def test_wildcard_parsed_from_yaml_shaped_config(self):
        pc = PlatformConfig.from_dict({
            "enabled": True,
            "channel_overrides": {"*": {"model": "opus", "provider": "claude-alr"}},
        })
        config = GatewayConfig(platforms={Platform.MATRIX: pc})
        assert _get_channel_override(config, Platform.MATRIX, "!any:x").model == "opus"

    def test_wildcard_drives_session_runtime(self):
        runner = object.__new__(GatewayRunner)
        runner._session_model_overrides = {}
        runner.config = _matrix_config({"*": ChannelOverride(model="opus", provider="claude-alr")})
        source = SessionSource(platform=Platform.MATRIX, chat_id="!room:x", user_id="u1")
        with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={
                "provider": "anthropic", "api_key": "k", "base_url": "https://api.anthropic.com",
                "api_mode": "chat_completions"}), \
             patch("gateway.run._resolve_runtime_agent_kwargs_for_provider", return_value={
                "provider": "claude-alr", "api_key": "k2", "base_url": "http://127.0.0.1:1/v1",
                "api_mode": "anthropic_messages"}):
            model, runtime = runner._resolve_session_agent_runtime(
                source=source, user_config={"model": {"default": "fable"}})
        assert model == "opus"
        assert runtime["provider"] == "claude-alr"


def test_chat_pin_beats_wildcard(tmp_path, monkeypatch):
    """A persisted chat pin (``!model``) outranks the platform's '*' channel override."""
    from gateway.session import SessionStore

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = _matrix_config({"*": ChannelOverride(model="opus", provider="claude-alr")})
    runner.session_store = SessionStore(config=GatewayConfig(), sessions_dir=tmp_path)
    runner.session_store._db = None
    source = SessionSource(platform=Platform.MATRIX, chat_id="!room:x", chat_type="group", user_id="alice")
    entry = runner.session_store.get_or_create_session(source)
    runner.session_store.set_model_override(
        entry.session_key, {"model": "pinned-model", "provider": "openrouter"})
    monkeypatch.setattr(runner, "_reresolve_model_override_credentials", lambda identity: {
        **identity, "api_key": "resolved", "base_url": "https://example.test/v1"})
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs",
                        lambda: {"provider": "openrouter", "api_key": "global"})
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider",
                        lambda provider, target_model=None: {"provider": provider, "api_key": "p"})
    model, runtime = runner._resolve_session_agent_runtime(
        user_config={"model": {"default": "fable", "provider": "openrouter"}},
        source=source, session_key=entry.session_key)
    assert model == "pinned-model"
    assert runtime["provider"] == "openrouter"
    # Clearing the pin falls back to the '*' channel default, not config.yaml.
    runner.session_store.clear_model_route_override(entry.session_key)
    model, runtime = runner._resolve_session_agent_runtime(
        user_config={"model": {"default": "fable", "provider": "openrouter"}},
        source=source, session_key=entry.session_key)
    assert model == "opus"
    assert runtime["provider"] == "claude-alr"


def test_multiplex_wildcard_stays_in_runtime_profile():
    """Under multiplex the routed profile's own ``channel_overrides`` apply: the launch profile's '*'
    never reaches a secondary profile's rooms, and the secondary's own '*' is honoured (Prism ba9e433fba77)."""
    runner = object.__new__(GatewayRunner)
    runner.config = _matrix_config({"*": ChannelOverride(model="opus", provider="claude-alr")})
    runner.config.multiplex_profiles = True
    runner._primary_profile_name = "default"
    runner._profile_configs = {
        "ops": GatewayConfig(platforms={Platform.MATRIX: PlatformConfig(enabled=True)}),
        "lab": _matrix_config({"*": ChannelOverride(model="lab-model")}),
    }
    ops = SessionSource(platform=Platform.MATRIX, chat_id="!room:x", user_id="u1", profile="ops")
    lab = SessionSource(platform=Platform.MATRIX, chat_id="!room:x", user_id="u1", profile="lab")
    dflt = SessionSource(platform=Platform.MATRIX, chat_id="!room:x", user_id="u1", profile="default")
    assert runner._channel_override_for(ops) is None
    assert runner._channel_override_for(lab).model == "lab-model"
    assert runner._channel_override_for(dflt).model == "opus"
    # Unloaded secondary config: no override rather than the launch profile's.
    ghost = SessionSource(platform=Platform.MATRIX, chat_id="!room:x", user_id="u1", profile="ghost")
    assert runner._channel_override_for(ghost) is None
    # Sourceless lookups (system prompt / model loaders) follow the bound runtime scope.
    with patch.object(GatewayRunner, "_active_profile_name", return_value="ops"):
        assert runner._resolve_model_for_channel(
            Platform.MATRIX, "!room:x", user_config={"model": {"default": "fable"}}) == "fable"
    with patch.object(GatewayRunner, "_active_profile_name", return_value="default"):
        assert runner._resolve_model_for_channel(
            Platform.MATRIX, "!room:x", user_config={"model": {"default": "fable"}}) == "opus"
