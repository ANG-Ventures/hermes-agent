"""The fast-mode override injected into a turn must match the resolved route.

The seam under test is where `/fast` (a session preference) becomes
`request_overrides` on the outgoing request. These tests assert the wire
payload, not the command's reply text.
"""

import pytest

import gateway.run as gateway_run
from tests.gateway.test_fast_command import _make_runner  # noqa: F401


def _runtime(provider, api_mode, base_url="", **extra):
    rt = {
        "api_key": "***",
        "base_url": base_url,
        "provider": provider,
        "api_mode": api_mode,
        "command": None,
        "args": [],
        "credential_pool": None,
    }
    rt.update(extra)
    return rt


class TestGatewayTurnRouteInjection:
    def test_native_anthropic_route_injects_speed(self):
        runner = _make_runner()
        runner._service_tier = "priority"

        route = gateway_run.GatewayRunner._resolve_turn_agent_config(
            runner,
            "hi",
            "claude-opus-4-6",
            _runtime("anthropic", "anthropic_messages"),
        )

        assert route["request_overrides"] == {"speed": "fast"}

    def test_third_party_messages_route_injects_nothing(self):
        """`speed=fast` requires the fast-mode beta a proxy would reject.

        The model-only gate answers ``{"speed": "fast"}`` for this same model,
        so without the route check the proxy receives a parameter it cannot
        honour.
        """
        from hermes_cli.models import resolve_fast_mode_overrides

        assert resolve_fast_mode_overrides("claude-opus-4-6") == {"speed": "fast"}

        runner = _make_runner()
        runner._service_tier = "priority"

        route = gateway_run.GatewayRunner._resolve_turn_agent_config(
            runner,
            "hi",
            "claude-opus-4-6",
            _runtime(
                "openrouter",
                "anthropic_messages",
                base_url="https://openrouter.ai/api/v1",
            ),
        )

        assert route["request_overrides"] == {}
        assert "speed" not in route["request_overrides"]

    def test_codex_backend_injects_fast_tier_not_priority(self):
        runner = _make_runner()
        runner._service_tier = "priority"

        route = gateway_run.GatewayRunner._resolve_turn_agent_config(
            runner, "hi", "gpt-5.4", _runtime("openai-codex", "codex_responses")
        )

        assert route["request_overrides"] == {"service_tier": "fast"}

    def test_openai_api_route_injects_priority_tier(self):
        runner = _make_runner()
        runner._service_tier = "priority"

        route = gateway_run.GatewayRunner._resolve_turn_agent_config(
            runner, "hi", "gpt-5.4", _runtime("openai-api", "chat_completions")
        )

        assert route["request_overrides"] == {"service_tier": "priority"}

    def test_fast_off_injects_nothing_on_any_route(self):
        runner = _make_runner()
        runner._service_tier = None

        route = gateway_run.GatewayRunner._resolve_turn_agent_config(
            runner,
            "hi",
            "claude-opus-4-6",
            _runtime("anthropic", "anthropic_messages"),
        )

        assert route["request_overrides"] == {}

    def test_provider_extra_body_survives_alongside_fast(self):
        """A custom provider's own request_overrides must not be clobbered."""
        runner = _make_runner()
        runner._service_tier = "priority"

        route = gateway_run.GatewayRunner._resolve_turn_agent_config(
            runner,
            "hi",
            "gpt-5.4",
            _runtime(
                "openai-api",
                "chat_completions",
                request_overrides={"extra_body": {"chat_template_kwargs": {"a": 1}}},
            ),
        )

        assert route["request_overrides"]["service_tier"] == "priority"
        assert route["request_overrides"]["extra_body"] == {
            "chat_template_kwargs": {"a": 1}
        }

    def test_route_runtime_is_not_switched_by_fast(self):
        """Fast mode adds request params; it must never re-route the call."""
        runner = _make_runner()
        runner._service_tier = "priority"

        route = gateway_run.GatewayRunner._resolve_turn_agent_config(
            runner,
            "hi",
            "claude-opus-4-6",
            _runtime(
                "openrouter",
                "anthropic_messages",
                base_url="https://openrouter.ai/api/v1",
            ),
        )

        assert route["runtime"]["provider"] == "openrouter"
        assert route["runtime"]["api_mode"] == "anthropic_messages"
        assert route["runtime"]["base_url"] == "https://openrouter.ai/api/v1"
        assert route["model"] == "claude-opus-4-6"


class TestCliTurnRouteInjection:
    def _stub(self, *, model, provider, api_mode, service_tier="priority"):
        from types import SimpleNamespace

        return SimpleNamespace(
            model=model,
            api_key="***",
            base_url="",
            provider=provider,
            requested_provider=provider,
            api_mode=api_mode,
            acp_command=None,
            acp_args=[],
            _credential_pool=None,
            service_tier=service_tier,
        )

    def test_native_anthropic_route_injects_speed(self):
        import cli as cli_mod

        route = cli_mod.HermesCLI._resolve_turn_agent_config(
            self._stub(
                model="claude-opus-4-6",
                provider="anthropic",
                api_mode="anthropic_messages",
            ),
            "hi",
        )
        assert route["request_overrides"] == {"speed": "fast"}

    def test_third_party_messages_route_injects_nothing(self):
        import cli as cli_mod

        route = cli_mod.HermesCLI._resolve_turn_agent_config(
            self._stub(
                model="claude-opus-4-6",
                provider="openrouter",
                api_mode="anthropic_messages",
            ),
            "hi",
        )
        assert not route["request_overrides"]

    def test_codex_backend_injects_fast_tier(self):
        import cli as cli_mod

        route = cli_mod.HermesCLI._resolve_turn_agent_config(
            self._stub(
                model="gpt-5.4",
                provider="openai-codex",
                api_mode="codex_responses",
            ),
            "hi",
        )
        assert route["request_overrides"] == {"service_tier": "fast"}


class TestPromptCacheSafety:
    """Fast mode must ride the REQUEST, never the cached prefix.

    A per-conversation cached prefix is reused every turn; anything that
    mutates the system prompt or past context on a toggle would invalidate it
    and multiply the user's cost. The seam is therefore asserted structurally:
    the resolved route may differ ONLY in ``request_overrides``.
    """

    def test_toggling_fast_changes_only_request_overrides(self):
        runner_off = _make_runner()
        runner_off._service_tier = None
        runner_on = _make_runner()
        runner_on._service_tier = "priority"

        rt = _runtime("anthropic", "anthropic_messages")
        off = gateway_run.GatewayRunner._resolve_turn_agent_config(
            runner_off, "hi", "claude-opus-4-6", dict(rt)
        )
        on = gateway_run.GatewayRunner._resolve_turn_agent_config(
            runner_on, "hi", "claude-opus-4-6", dict(rt)
        )

        differing = {k for k in set(off) | set(on) if off.get(k) != on.get(k)}
        assert differing == {"request_overrides"}

    def test_cache_identity_inputs_are_unchanged_by_fast(self):
        """model / runtime / signature are cache-identity; they must not move."""
        runner_off = _make_runner()
        runner_off._service_tier = None
        runner_on = _make_runner()
        runner_on._service_tier = "priority"

        rt = _runtime("openai-api", "chat_completions")
        off = gateway_run.GatewayRunner._resolve_turn_agent_config(
            runner_off, "hi", "gpt-5.4", dict(rt)
        )
        on = gateway_run.GatewayRunner._resolve_turn_agent_config(
            runner_on, "hi", "gpt-5.4", dict(rt)
        )

        assert off["model"] == on["model"]
        assert off["runtime"] == on["runtime"]
        assert off["signature"] == on["signature"]
        # ...and the toggle DID take effect, so this is not a vacuous pass.
        assert on["request_overrides"] == {"service_tier": "priority"}
        assert off["request_overrides"] == {}

    def test_fast_overrides_carry_no_prompt_or_context_keys(self):
        """Nothing the toggle emits may touch prompt/message/tool payloads."""
        from hermes_cli.models import resolve_fast_mode_capability

        forbidden = {
            "system",
            "messages",
            "input",
            "instructions",
            "tools",
            "prompt",
            "prompt_cache_key",
        }
        for model, provider, api_mode in [
            ("claude-opus-4-6", "anthropic", "anthropic_messages"),
            ("gpt-5.4", "openai-api", "chat_completions"),
            ("gpt-5.4", "openai-codex", "codex_responses"),
            ("grok-4.6", "xai", "chat_completions"),
        ]:
            cap = resolve_fast_mode_capability(
                model=model, provider=provider, api_mode=api_mode
            )
            assert cap.supported, (model, provider)
            assert not (set(cap.request_overrides) & forbidden), cap.request_overrides
