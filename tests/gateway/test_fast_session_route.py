"""`/fast` must answer for the route the session's NEXT turn will take.

A session-scoped `/model` override changes that route, so a fast toggle
resolved against the *configured* route would report and gate on a model the
session is no longer using.
"""

import pytest

import gateway.run as gateway_run
from tests.gateway.test_fast_command import _make_runner, _make_event


CONFIG = {
    "model": {
        "default": "gpt-5.4",
        "provider": "openai-api",
        "api_mode": "chat_completions",
    }
}


class TestSessionRouteResolution:
    def test_no_override_uses_the_configured_route(self):
        runner = _make_runner()

        model, provider, api_mode = runner._resolve_session_fast_route(
            session_key="s1", user_config=CONFIG
        )

        assert model == "gpt-5.4"
        assert provider == "openai-api"
        assert api_mode == "chat_completions"

    def test_session_model_override_steers_the_route(self):
        runner = _make_runner()
        runner._session_model_overrides["s1"] = {
            "model": "claude-opus-4-6",
            "provider": "anthropic",
            "api_mode": "anthropic_messages",
        }

        model, provider, api_mode = runner._resolve_session_fast_route(
            session_key="s1", user_config=CONFIG
        )

        assert model == "claude-opus-4-6"
        assert provider == "anthropic"
        assert api_mode == "anthropic_messages"

    def test_override_without_api_mode_is_rederived_not_inherited(self):
        """Persisted overrides drop api_mode; it must not leak from config.

        Inheriting the configured `chat_completions` here would resolve an
        Anthropic model onto an OpenAI-style wire.
        """
        runner = _make_runner()
        runner._session_model_overrides["s1"] = {
            "model": "claude-opus-4-6",
            "provider": "anthropic",
        }

        _, _, api_mode = runner._resolve_session_fast_route(
            session_key="s1", user_config=CONFIG
        )

        assert api_mode == "anthropic_messages"
        assert api_mode != CONFIG["model"]["api_mode"]

    def test_override_is_scoped_to_its_own_session(self):
        runner = _make_runner()
        runner._session_model_overrides["s1"] = {
            "model": "claude-opus-4-6",
            "provider": "anthropic",
            "api_mode": "anthropic_messages",
        }

        other = runner._resolve_session_fast_route(
            session_key="s2", user_config=CONFIG
        )

        assert other[0] == "gpt-5.4"
        assert other[1] == "openai-api"


class TestOverrideChangesTheInjectedOverrides:
    """End-to-end: the override must change what reaches the wire."""

    @staticmethod
    def _overrides_for(runner, session_key):
        from hermes_cli.models import resolve_fast_mode_capability

        model, provider, api_mode = runner._resolve_session_fast_route(
            session_key=session_key, user_config=CONFIG
        )
        return resolve_fast_mode_capability(
            model=model, provider=provider, api_mode=api_mode
        ).request_overrides

    def test_switching_the_session_model_switches_the_fast_key(self):
        runner = _make_runner()

        # Configured route -> OpenAI-style tier.
        default = self._overrides_for(runner, "s1")
        assert default == {"service_tier": "priority"}

        # After /model to a native Anthropic route -> the Anthropic key.
        runner._session_model_overrides["s1"] = {
            "model": "claude-opus-4-6",
            "provider": "anthropic",
            "api_mode": "anthropic_messages",
        }
        overridden = self._overrides_for(runner, "s1")
        assert overridden == {"speed": "fast"}
        assert overridden != default


class TestServiceTierPreferencePersistence:
    """The session preference itself must survive and stay session-scoped."""

    def test_session_override_is_readable_back(self):
        runner = _make_runner()
        runner._set_session_service_tier_override("s1", "priority")

        assert runner._resolve_session_service_tier(session_key="s1") == "priority"

    def test_explicit_normal_is_distinct_from_unset(self):
        """`/fast normal` stores None and must WIN over a config default.

        Presence, not truthiness, decides — an explicit normal is an override.
        """
        runner = _make_runner()
        runner._set_session_service_tier_override("s1", None)

        assert runner._resolve_session_service_tier(session_key="s1") is None

    def test_clear_falls_back_to_the_config_default(self, monkeypatch):
        runner = _make_runner()
        monkeypatch.setattr(
            gateway_run,
            "_load_gateway_runtime_config",
            lambda: {"agent": {"service_tier": "fast"}},
        )

        runner._set_session_service_tier_override("s1", None)
        assert runner._resolve_session_service_tier(session_key="s1") is None

        runner._set_session_service_tier_override("s1", None, clear=True)
        assert runner._resolve_session_service_tier(session_key="s1") == "priority"

    def test_override_does_not_leak_to_other_sessions(self, monkeypatch):
        runner = _make_runner()
        monkeypatch.setattr(
            gateway_run,
            "_load_gateway_runtime_config",
            lambda: {"agent": {"service_tier": "fast"}},
        )

        runner._set_session_service_tier_override("s1", None)

        assert runner._resolve_session_service_tier(session_key="s1") is None
        assert runner._resolve_session_service_tier(session_key="s2") == "priority"
