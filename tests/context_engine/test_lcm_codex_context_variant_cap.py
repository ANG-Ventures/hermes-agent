"""LCM's Codex OAuth cap must not clamp explicit ``-900k`` context variants.

Regression for the "372k" incident: ``gpt-5.6-sol-900k`` resolves to 900,000
on the host (live-verified table in ``agent.model_metadata``), but LCM's
private fallback table substring-matched ``gpt-5.6`` and clamped the window to
a stale transient value, so compaction fired at ~372k on every sol/terra/luna
cron that had opted into the large window.
"""

from __future__ import annotations

import pytest

from agent import model_metadata
from plugins.context_engine.lcm import codex_routing


@pytest.mark.parametrize(
    "model",
    ["gpt-5.6-sol-900k", "gpt-5.6-terra-900k", "gpt-5.6-luna-900k", "gpt-6-astra-900k"],
)
def test_explicit_context_variant_is_not_capped(model: str) -> None:
    assert model_metadata.is_codex_context_variant(model), "premise: host marks it a variant"
    assert codex_routing._codex_oauth_context_cap(model, "openai-codex") is None


@pytest.mark.parametrize(
    "model, expected",
    [
        ("gpt-5.6-sol", 272_000),
        ("gpt-5.6", 272_000),
        ("gpt-5.4-mini", 272_000),
        ("gpt-5.3-codex-spark", 128_000),
    ],
)
def test_base_slugs_keep_conservative_cap(model: str, expected: int) -> None:
    assert codex_routing._codex_oauth_context_cap(model, "openai-codex") == expected


def test_gpt56_table_matches_host_fallback_not_july_transient() -> None:
    # The host resolver is the source of truth for the advertised base window.
    host = model_metadata.get_model_context_length("gpt-5.6-sol", provider="openai-codex")
    assert host == 272_000
    assert codex_routing._CODEX_OAUTH_CONTEXT_CAPS["gpt-5.6"] == host


def test_cap_never_exceeds_host_for_variant_end_to_end() -> None:
    """The effective window LCM budgets against equals what the host resolved."""
    model = "gpt-5.6-sol-900k"
    host = model_metadata.get_model_context_length(model, provider="openai-codex")
    assert host == 900_000
    cap = codex_routing._codex_oauth_context_cap(model, "openai-codex")
    effective = host if cap is None else min(host, cap)
    assert effective == 900_000


def test_non_codex_route_unaffected() -> None:
    assert codex_routing._codex_oauth_context_cap("gpt-5.6-sol-900k", "openrouter") is None
    assert codex_routing._codex_oauth_context_cap("gpt-5.6-sol", "openai-api") is None
