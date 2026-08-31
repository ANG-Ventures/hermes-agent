"""Route-aware fast-mode capability resolution.

These tests pin the property the model-only gate cannot express: fast mode is
carried by a *wire-specific* request parameter. ``service_tier`` is an
OpenAI-style field that endpoints ignore when they do not sell the tier, so
breadth there is free. ``speed`` is an Anthropic Messages parameter — on any
other wire it is an unknown argument, and even on Anthropic it is a hard 400
outside the dated contract — so it must be gated on the resolved route rather
than on the model id alone.
"""

import pytest

from hermes_cli.fast_mode_contracts import (
    FAST_MODE_CAPABILITY_CATALOG,
    anthropic_fast_contract_accepts,
    codex_fast_contract_accepts,
    normalize_fast_model_id,
)
from hermes_cli.models import (
    resolve_fast_mode_capability,
    resolve_fast_mode_capability_for_configured_route,
    resolve_fast_mode_overrides,
)


class TestAnthropicSpeedIsRouteGated:
    """`speed=fast` may only be emitted on the native Anthropic Messages wire."""

    def test_native_anthropic_route_emits_speed(self):
        cap = resolve_fast_mode_capability(
            model="claude-opus-4-6",
            provider="anthropic",
            api_mode="anthropic_messages",
        )
        assert cap.supported is True
        assert cap.family == "anthropic_fast"
        assert cap.request_overrides == {"speed": "fast"}

    def test_third_party_anthropic_messages_route_emits_nothing(self):
        """A proxy speaking Messages would reject the fast-mode beta header.

        The model-only gate answers ``{"speed": "fast"}`` here, which is what
        the adapter then has to defensively strip.
        """
        assert resolve_fast_mode_overrides("claude-opus-4-6") == {"speed": "fast"}

        cap = resolve_fast_mode_capability(
            model="claude-opus-4-6",
            provider="openrouter",
            api_mode="anthropic_messages",
        )
        assert cap.supported is False
        assert cap.request_overrides == {}
        assert "native" in (cap.reason or "")

    def test_anthropic_model_on_openai_style_wire_never_emits_speed(self):
        """`speed` is not a field on an OpenAI-compatible wire at all."""
        cap = resolve_fast_mode_capability(
            model="claude-opus-4-6",
            provider="openrouter",
            api_mode="chat_completions",
        )
        assert "speed" not in cap.request_overrides

    def test_off_contract_claude_emits_nothing_on_native_route(self):
        """Opus 4.7 rejects `speed=fast` with a 400; it must not be sent."""
        cap = resolve_fast_mode_capability(
            model="claude-opus-4-7",
            provider="anthropic",
            api_mode="anthropic_messages",
        )
        assert cap.supported is False
        assert cap.request_overrides == {}


class TestCodexFastIsADistinctTierValue:
    """The Codex backend sells `fast`, not `priority` — a different value."""

    def test_codex_backend_emits_service_tier_fast(self):
        cap = resolve_fast_mode_capability(
            model="gpt-5.4", provider="openai-codex", api_mode="codex_responses"
        )
        assert cap.supported is True
        assert cap.family == "codex_fast"
        assert cap.request_overrides == {"service_tier": "fast"}

    def test_same_model_on_openai_api_emits_priority_instead(self):
        """Identical model id, different backend, different tier value."""
        codex = resolve_fast_mode_capability(
            model="gpt-5.4", provider="openai-codex", api_mode="codex_responses"
        )
        openai_api = resolve_fast_mode_capability(
            model="gpt-5.4", provider="openai-api", api_mode="chat_completions"
        )
        assert codex.supported and openai_api.supported
        assert codex.request_overrides == {"service_tier": "fast"}
        assert openai_api.request_overrides == {"service_tier": "priority"}
        assert codex.request_overrides != openai_api.request_overrides

    def test_codex_series_model_off_contract_emits_nothing(self):
        cap = resolve_fast_mode_capability(
            model="gpt-5-codex",
            provider="openai-codex",
            api_mode="codex_responses",
        )
        assert cap.supported is False
        assert cap.request_overrides == {}


class TestPriorityProcessingStaysPermissive:
    """`service_tier` is ignored, not rejected — breadth here is deliberate."""

    def test_openai_flagship_through_a_proxy_still_supported(self):
        cap = resolve_fast_mode_capability(
            model="gpt-5.4", provider="openrouter", api_mode="chat_completions"
        )
        assert cap.supported is True
        assert cap.request_overrides == {"service_tier": "priority"}

    def test_grok_46_supported_on_openai_style_wire(self):
        cap = resolve_fast_mode_capability(
            model="grok-4.6", provider="xai", api_mode="chat_completions"
        )
        assert cap.supported is True
        assert cap.request_overrides == {"service_tier": "priority"}

    def test_non_flagship_model_emits_nothing(self):
        cap = resolve_fast_mode_capability(
            model="some-random-model",
            provider="openrouter",
            api_mode="chat_completions",
        )
        assert cap.supported is False
        assert cap.request_overrides == {}


class TestUnsupportedNeverCarriesOverrides:
    @pytest.mark.parametrize(
        "model,provider,api_mode",
        [
            ("claude-opus-4-6", "openrouter", "anthropic_messages"),
            ("claude-opus-4-7", "anthropic", "anthropic_messages"),
            ("gpt-5-codex", "openai-codex", "codex_responses"),
            ("some-random-model", "openrouter", "chat_completions"),
            ("gpt-5.4", "bedrock", "converse"),
            ("", "", ""),
        ],
    )
    def test_structural_invariant(self, model, provider, api_mode):
        """not supported => nothing may reach the wire, for every route."""
        cap = resolve_fast_mode_capability(
            model=model, provider=provider, api_mode=api_mode
        )
        assert cap.supported is False
        assert cap.request_overrides == {}

    def test_refusals_name_the_route(self):
        cap = resolve_fast_mode_capability(
            model="claude-opus-4-7",
            provider="anthropic",
            api_mode="anthropic_messages",
        )
        assert cap.reason
        assert "claude-opus-4-7" in cap.reason


class TestUnpinnedProviderDoesNotHideFast:
    """`auto`/absent provider must not be treated as a WRONG provider."""

    @pytest.mark.parametrize(
        "model,expected_family,expected_overrides",
        [
            ("claude-opus-4-6", "anthropic_fast", {"speed": "fast"}),
            ("gpt-5.4", "openai_priority", {"service_tier": "priority"}),
            ("grok-4.6", "openai_priority", {"service_tier": "priority"}),
        ],
    )
    def test_auto_provider_resolves_native_route(
        self, model, expected_family, expected_overrides
    ):
        cap = resolve_fast_mode_capability_for_configured_route(
            model=model, provider="auto", api_mode=""
        )
        assert cap.supported is True
        assert cap.family == expected_family
        assert cap.request_overrides == expected_overrides

    def test_empty_provider_resolves_native_route(self):
        cap = resolve_fast_mode_capability_for_configured_route(
            model="claude-opus-4-6", provider=None, api_mode=None
        )
        assert cap.supported is True
        assert cap.request_overrides == {"speed": "fast"}

    def test_auto_provider_still_says_no_for_uncovered_model(self):
        cap = resolve_fast_mode_capability_for_configured_route(
            model="claude-opus-4-7", provider="auto", api_mode=""
        )
        assert cap.supported is False
        assert cap.request_overrides == {}

    def test_concrete_provider_is_not_rerouted(self):
        """A named third-party provider still fails closed, unlike `auto`."""
        cap = resolve_fast_mode_capability_for_configured_route(
            model="claude-opus-4-6",
            provider="openrouter",
            api_mode="anthropic_messages",
        )
        assert cap.supported is False


class TestDatedContracts:
    def test_anthropic_contract_is_exact_not_prefix(self):
        assert anthropic_fast_contract_accepts("claude-opus-4-6") is True
        # Neighbouring ids in the same family reject the parameter.
        assert anthropic_fast_contract_accepts("claude-opus-4-7") is False
        assert anthropic_fast_contract_accepts("claude-sonnet-4-6") is False

    def test_documented_spelling_aliases_fold(self):
        assert normalize_fast_model_id("claude-opus-4.6") == "claude-opus-4-6"
        assert normalize_fast_model_id("anthropic/claude-opus-4-6") == (
            "claude-opus-4-6"
        )
        assert anthropic_fast_contract_accepts("anthropic/claude-opus-4.6") is True

    def test_undocumented_suffixes_do_not_fold(self):
        """A suffix we don't recognise must fail the gate, not be coerced."""
        assert anthropic_fast_contract_accepts("claude-opus-4-6:free") is False
        assert anthropic_fast_contract_accepts("claude-opus-4-6-20260101") is False

    def test_codex_contract_is_exact(self):
        assert codex_fast_contract_accepts("gpt-5.4") is True
        assert codex_fast_contract_accepts("gpt-5-codex") is False

    def test_every_contract_carries_provenance(self):
        for family, contract in FAST_MODE_CAPABILITY_CATALOG.items():
            assert contract["source_url"].startswith("https://"), family
            assert contract["checked_date"], family
            assert contract["models"], family

    def test_anthropic_capability_agrees_with_adapter_gate(self):
        """The command gate and the adapter's own gate must not drift.

        ``agent.anthropic_adapter._supports_fast_mode`` decides whether the
        request actually carries ``speed``; if it disagrees with what /fast
        advertises, the UI shows a toggle the runtime silently drops.
        """
        from agent.anthropic_adapter import _supports_fast_mode

        for model in ("claude-opus-4-6", "claude-opus-4-7", "claude-sonnet-4-6"):
            cap = resolve_fast_mode_capability(
                model=model, provider="anthropic", api_mode="anthropic_messages"
            )
            assert cap.supported is _supports_fast_mode(model), model
