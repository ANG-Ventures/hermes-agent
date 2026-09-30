"""-z/oneshot must honour agent.reasoning_effort and provider-prefixed -m tiers (t_bffecae7)."""

import sys
import types

import pytest

import hermes_cli.oneshot as oneshot_mod
from hermes_cli.fast_mode_contracts import normalize_fast_model_id, ultrafast_contract_accepts
from hermes_cli.models import model_supports_ultrafast, service_tier_request_overrides


def _run_oneshot(monkeypatch, cfg, *, model, provider, api_mode="codex_responses"):
    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.suppress_status_output = False
            self.stream_delta_callback = object()
            self.tool_gen_callback = object()
            self._session_messages = []

        def run_conversation(self, _prompt):
            return {"final_response": "done"}

        def shutdown_memory_provider(self, messages=None):
            pass

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "run_agent", types.SimpleNamespace(AIAgent=FakeAgent))
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "api_key": "key",
            "base_url": "https://example.invalid",
            "provider": provider,
            "api_mode": api_mode,
            "credential_pool": None,
        },
    )
    monkeypatch.setattr(oneshot_mod, "_create_session_db_for_oneshot", lambda: None)
    oneshot_mod._run_agent("hello", model=model, provider=provider, use_config_toolsets=False)
    return captured


def test_oneshot_forwards_configured_reasoning_effort(monkeypatch):
    cfg = {
        "model": {"default": "gpt-6-astra", "provider": "openai-codex"},
        "agent": {"reasoning_effort": "low"},
    }
    kwargs = _run_oneshot(monkeypatch, cfg, model="gpt-6-astra", provider="openai-codex")

    from hermes_constants import resolve_reasoning_config

    assert kwargs["reasoning_config"] == resolve_reasoning_config(cfg, "gpt-6-astra")
    assert kwargs["reasoning_config"]["effort"] == "low"


def test_oneshot_reasoning_uses_per_model_override_for_effective_model(monkeypatch):
    cfg = {
        "model": {"default": "gpt-5.5", "provider": "openai-codex"},
        "agent": {
            "reasoning_effort": "low",
            "reasoning_overrides": {"gpt-6-astra": "high"},
        },
    }
    kwargs = _run_oneshot(monkeypatch, cfg, model="gpt-6-astra", provider="openai-codex")

    assert kwargs["reasoning_config"]["effort"] == "high"


def test_oneshot_sends_ultrafast_for_provider_prefixed_model(monkeypatch):
    cfg = {
        "model": {"default": "gpt-5.5", "provider": "openai-codex"},
        "agent": {"service_tier": "ultrafast"},
    }
    kwargs = _run_oneshot(
        monkeypatch, cfg, model="openai-codex/gpt-6-astra", provider="openai-codex"
    )

    assert kwargs["request_overrides"] == {"service_tier": "ultrafast"}


@pytest.mark.parametrize("prefix", ["openai-codex/", "openai-api/", "openai/", "OpenAI-Codex/"])
def test_provider_prefix_does_not_change_ultrafast_contract(prefix):
    assert normalize_fast_model_id(prefix + "gpt-6-astra") == "gpt-6-astra"
    assert ultrafast_contract_accepts(prefix + "gpt-6-astra") is ultrafast_contract_accepts("gpt-6-astra")
    assert model_supports_ultrafast(prefix + "gpt-6-astra") is model_supports_ultrafast("gpt-6-astra")


def test_prefixed_model_gets_same_tier_overrides_as_bare(caplog):
    bare = service_tier_request_overrides(
        model="gpt-6-astra", provider="openai-codex", api_mode="codex_responses", tier="ultrafast"
    )
    with caplog.at_level("WARNING"):
        prefixed = service_tier_request_overrides(
            model="openai-codex/gpt-6-astra",
            provider="openai-codex",
            api_mode="codex_responses",
            tier="ultrafast",
        )
    assert bare == {"service_tier": "ultrafast"}
    assert prefixed == bare
    assert "openai-codex/openai-codex/" not in caplog.text


def test_provider_prefix_does_not_unlock_non_native_route():
    # The prefix is only stripped for the model lookup; route gating still decides.
    assert service_tier_request_overrides(
        model="openai-codex/gpt-6-astra", provider="openrouter", api_mode="chat_completions", tier="ultrafast"
    ) == {}
