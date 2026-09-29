"""Footer provenance must survive the runtime's provider/model resolution."""

from typing import Any

import pytest

from gateway.runtime_footer import build_footer_line
from hermes_cli.model_normalize import normalize_model_for_provider


@pytest.mark.parametrize("provider", ["openrouter", "nous"])
@pytest.mark.parametrize("model", ["claude-sonnet-4.6", "anthropic/claude-sonnet-4.6"])
def test_aggregator_footer_preserves_serving_provider(provider, model):
    # Exercise the normalizer used at agent init, not an ideal bare footer ID.
    resolved_model = normalize_model_for_provider(model, provider)
    assert resolved_model == "anthropic/claude-sonnet-4.6"
    footer = build_footer_line(
        user_config={"display": {"runtime_footer": {
            "enabled": True, "fields": ["provider_model"],
        }}},
        platform_key="discord",
        model=resolved_model,
        provider=provider,
        context_tokens=0,
        context_length=None,
    )
    assert footer == f"{provider}/{resolved_model}"


@pytest.mark.parametrize("provider", ["openrouter", "nous", "claude-bridge-f3"])
def test_default_footer_bytes_unchanged_with_resolved_metadata(monkeypatch, provider):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    common: dict[str, Any] = dict(
        user_config={"display": {"runtime_footer": {"enabled": True}}},
        platform_key="discord",
        model="anthropic/claude-sonnet-4.6",
        context_tokens=50_247,
        context_length=1_000_000,
        cwd="/var/data",
    )
    baseline = build_footer_line(**common).encode("utf-8")
    enriched = build_footer_line(
        **common,
        provider=provider,
        reasoning_config={"enabled": True, "effort": "low"},
        turn_seconds=22,
    ).encode("utf-8")
    assert baseline == enriched == "claude-sonnet-4.6 · 5% · /var/data".encode("utf-8")
