"""Codex-subscription proxy lanes resolve the Codex window, not the API one (t_c1403b02).

The ``cpa`` provider plugin fronts CLIProxyAPI, which serves Codex-family
slugs from the SAME ChatGPT/Codex subscription tokens as ``openai-codex``. The
backend enforces the Codex ceiling (gpt-6.1-sol: 922,000 input, measured by
bisection 2026-09-30), not the API platform's 1,050,000. A provider profile
that sets ``codex_subscription_backend=True`` routes Codex-family slugs through
the openai-codex resolver (policy knob, verified tables, ``-900k`` alias);
non-Codex ids on the same lane (kimi-*, grok-*) are unaffected.
"""

from __future__ import annotations

import importlib
from decimal import Decimal
from unittest.mock import patch

import pytest

import providers
from agent import model_metadata as mm
from agent.usage_pricing import CanonicalUsage, estimate_usage_cost
from providers.base import ProviderProfile

pytestmark = pytest.mark.real_codex_context_policy

_config_mod = importlib.import_module("hermes_cli.config")
_CPA_URL = "http://127.0.0.1:18812/v1"

# Mirrors the cpa plugin (~/.hermes/plugins/model-providers/cpa): multi-vendor
# CLIProxyAPI lane that declares the Codex-subscription backend capability.
_CPA = ProviderProfile(
    name="cpa",
    aliases=("kimi-code", "kimi-cpa", "cliproxyapi"),
    base_url=_CPA_URL,
    codex_subscription_backend=True,
)


def _use_policy(monkeypatch, tmp_path, policy):
    path = tmp_path / "config.yaml"
    path.write_text(f"model:\n  codex_context_policy: {policy}\n", encoding="utf-8")
    monkeypatch.setattr(_config_mod, "get_config_path", lambda: path)
    _config_mod._RAW_CONFIG_CACHE.clear()


@pytest.fixture
def cpa_registered(monkeypatch):
    real = providers.get_provider_profile

    def _lookup(name):
        if name in ("cpa",) + _CPA.aliases:
            return _CPA
        return real(name)

    monkeypatch.setattr(providers, "get_provider_profile", _lookup)


@pytest.fixture
def large(monkeypatch, tmp_path, cpa_registered):
    _use_policy(monkeypatch, tmp_path, "large")


@pytest.fixture
def advertised(monkeypatch, tmp_path, cpa_registered):
    _use_policy(monkeypatch, tmp_path, "advertised")


def _ctx(model, provider="cpa", cached=None):
    """Resolve with network probes dead and the persistent cache controlled."""
    with patch("agent.model_metadata.model_metadata_http.get", side_effect=AssertionError("no network")), \
         patch("agent.model_metadata.get_cached_context_length", return_value=cached), \
         patch("agent.model_metadata.save_context_length"), \
         patch("agent.model_metadata._query_local_context_length", return_value=None), \
         patch("agent.model_metadata._query_ollama_api_show", return_value=None), \
         patch("agent.model_metadata._resolve_endpoint_context_length", return_value=None):
        return mm.get_model_context_length(
            model=model, base_url=_CPA_URL, api_key="cpa-proxy-key", provider=provider,
        )


@pytest.mark.parametrize("model, expected", [
    ("gpt-6.1-sol", 900_000),
    ("cpa/gpt-6.1-sol", 900_000),
    ("gpt-6-sol", 872_000),
    ("gpt-6.1-sol-900k", 900_000),   # legacy alias -> same window
])
def test_cpa_codex_slug_uses_verified_window_under_large(large, model, expected):
    assert _ctx(model) == expected


@pytest.mark.parametrize("provider", ["cpa", "kimi-cpa", "cliproxyapi"])
def test_cpa_aliases_resolve_the_same(large, provider):
    assert _ctx("gpt-6.1-sol", provider=provider) == 900_000


def test_cpa_codex_slug_uses_advertised_window_under_advertised(advertised):
    assert _ctx("gpt-6.1-sol") == 272_000
    assert _ctx("gpt-6.1-sol-900k") == 900_000


def test_stale_persisted_api_window_cannot_win(large):
    # The Studio already persisted 1,050,000 for cpa/gpt-6.1-sol; it must not
    # short-circuit the Codex resolution at step 1.
    assert _ctx("gpt-6.1-sol", cached=1_050_000) == 900_000


def test_cpa_matches_openai_codex(large):
    with patch("agent.model_metadata.model_metadata_http.get", side_effect=AssertionError("no network")):
        codex = mm._resolve_codex_oauth_context_length("gpt-6.1-sol")
    assert _ctx("gpt-6.1-sol") == codex == 900_000


def test_cpa_key_is_never_sent_to_the_codex_catalog(large):
    # The proxy bearer is not a ChatGPT OAuth token; resolution must use the
    # static Codex tables, never probe chatgpt.com with it.
    with patch.object(mm, "_fetch_codex_oauth_context_lengths_with_source") as probe:
        _ctx("gpt-6.1-sol")
    probe.assert_not_called()


@pytest.mark.parametrize("model", ["kimi-k3", "grok-4.20"])
def test_non_codex_ids_on_cpa_unaffected(large, model, monkeypatch):
    with_flag = _ctx(model)
    monkeypatch.setattr(mm, "provider_serves_codex_subscription", lambda p: False)
    assert _ctx(model) == with_flag
    assert with_flag != 900_000


def test_flag_off_profile_keeps_api_resolution(large, monkeypatch):
    plain = ProviderProfile(name="plainproxy", base_url=_CPA_URL)
    real = providers.get_provider_profile
    monkeypatch.setattr(
        providers, "get_provider_profile",
        lambda n: plain if n == "plainproxy" else real(n),
    )
    assert not mm.provider_serves_codex_subscription("plainproxy")
    assert mm.provider_serves_codex_subscription("cpa")
    assert mm.provider_serves_codex_subscription("openai-codex")
    assert not mm.provider_serves_codex_subscription("")


def test_pricing_parity_cpa_vs_openai_codex_above_272k():
    usage = CanonicalUsage(input_tokens=300_000, output_tokens=1_000)
    cpa = estimate_usage_cost("gpt-6.1-sol", usage, provider="cpa", base_url=_CPA_URL)
    codex = estimate_usage_cost(
        "gpt-6.1-sol", usage, provider="openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
    )
    assert codex.amount_usd is not None
    assert cpa.amount_usd == codex.amount_usd
    # Above-272K request tier: $4/M input, $15/M output (vs $2/$10 base).
    assert codex.amount_usd == Decimal("300000") * Decimal("4") / Decimal("1000000") \
        + Decimal("1000") * Decimal("15") / Decimal("1000000")
