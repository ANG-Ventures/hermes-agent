"""LCM's Codex OAuth cap must not clamp explicit ``-900k`` context variants.

Regression for the "372k" incident: ``gpt-5.6-sol-900k`` resolves to 900,000
on the host (live-verified table in ``agent.model_metadata``), but LCM's
private fallback table substring-matched ``gpt-5.6`` and clamped the window to
a stale transient value, so compaction fired at ~372k on every sol/terra/luna
cron that had opted into the large window.
"""

from __future__ import annotations

import sys
import types

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


def test_shadow_table_matches_host_for_every_row() -> None:
    """LCM's cap table is a shadow of the host fallback; any drift fails here.

    This is the ratchet for the 372k incident: the shadow had silently diverged
    on ONE row for six weeks. Every row must agree with what the host resolves
    for the same bare slug on the Codex OAuth route.
    """
    drift = {}
    for slug, cap in codex_routing._CODEX_OAUTH_CONTEXT_CAPS.items():
        host = model_metadata.get_model_context_length(slug, provider="openai-codex")
        if host != cap:
            drift[slug] = (cap, host)
    assert not drift, f"LCM shadow table drifted from host (lcm, host): {drift}"


def test_every_host_context_variant_family_is_uncapped() -> None:
    """Any base slug the shadow table knows must be uncapped in its variant form."""
    suffix = model_metadata.CODEX_CONTEXT_VARIANT_SUFFIX
    capped = {}
    for slug in codex_routing._CODEX_OAUTH_CONTEXT_CAPS:
        variant = f"{slug}{suffix}"
        if not model_metadata.is_codex_context_variant(variant):
            continue
        cap = codex_routing._codex_oauth_context_cap(variant, "openai-codex")
        if cap is not None:
            capped[variant] = cap
    assert not capped, f"variants clamped by LCM: {capped}"


def test_non_codex_route_unaffected() -> None:
    assert codex_routing._codex_oauth_context_cap("gpt-5.6-sol-900k", "openrouter") is None
    assert codex_routing._codex_oauth_context_cap("gpt-5.6-sol", "openai-api") is None


def _install_host_helper(monkeypatch, predicate):
    mod = types.ModuleType("agent.model_metadata")
    mod.is_codex_context_variant = predicate
    monkeypatch.setitem(sys.modules, "agent.model_metadata", mod)


def test_helper_missing_falls_back_to_table(monkeypatch):
    """Upstream CI stub / older host: no helper -> conservative table applies."""
    monkeypatch.setitem(sys.modules, "agent.model_metadata", None)  # import raises
    assert codex_routing._codex_oauth_context_cap("gpt-5.6-sol-900k", "openai-codex") == 272_000


def test_helper_raising_is_fail_open_to_table(monkeypatch):
    def _boom(_m):
        raise RuntimeError("host helper exploded")

    _install_host_helper(monkeypatch, _boom)
    assert codex_routing._codex_oauth_context_cap("gpt-5.6-sol-900k", "openai-codex") == 272_000


def test_helper_rejecting_slug_keeps_table(monkeypatch):
    _install_host_helper(monkeypatch, lambda m: False)
    assert codex_routing._codex_oauth_context_cap("gpt-5.6-sol-900k", "openai-codex") == 272_000
