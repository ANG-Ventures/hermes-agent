"""GPT-6.2 Sol / Astra runtime tables (rollout gate G1, 2026-11-17, t_898dc305).

Measured facts the tables encode:
- codex catalog (client_version 0.169.0): gpt-6.2-sol context_window 500,000, gpt-6.2-astra 272,000,
  max_context_window 872,000 for both;
- codex responses endpoint, both ids: 918,028 input tokens accepted, 925,028 rejected, so the verified
  window is 900,000 (same ~922,000 ceiling as gpt-6.1-sol);
- models.dev: context 1,050,000; sol $2/$10, astra $8/$40 per 1M in/out below 272K;
- effort ladder low..ultra, no ``none``.
"""

import importlib
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from agent import model_metadata as mm
from agent.reasoning_effort import CODEX_ASTRA_EFFORTS, clamp_effort, codex_supported_efforts
from agent.usage_pricing import CanonicalUsage, _OFFICIAL_DOCS_PRICING, estimate_usage_cost, get_pricing_entry

pytestmark = pytest.mark.real_codex_context_policy

_config_mod = importlib.import_module("hermes_cli.config")
_IDS = ("gpt-6.2-sol", "gpt-6.2-astra")


def _use_policy(monkeypatch, tmp_path, policy):
    path = tmp_path / "config.yaml"
    path.write_text(f"model:\n  codex_context_policy: {policy}\n", encoding="utf-8")
    monkeypatch.setattr(_config_mod, "get_config_path", lambda: path)
    _config_mod._RAW_CONFIG_CACHE.clear()
    _config_mod._LOAD_CONFIG_CACHE.clear()


@pytest.fixture
def large(monkeypatch, tmp_path):
    _use_policy(monkeypatch, tmp_path, "large")


@pytest.fixture
def advertised(monkeypatch, tmp_path):
    _use_policy(monkeypatch, tmp_path, "advertised")


def _codex_ctx(model: str) -> int:
    """Context length on the Codex OAuth route with the live catalog unreachable (static tables)."""
    fake = MagicMock()
    fake.status_code = 401
    fake.json.return_value = {}
    mm._codex_oauth_context_cache = {}
    with patch("agent.model_metadata.model_metadata_http.get", return_value=fake), \
         patch("agent.model_metadata.get_cached_context_length", return_value=None), \
         patch("agent.model_metadata.save_context_length"):
        return mm.get_model_context_length(
            model=model,
            base_url="https://chatgpt.com/backend-api/codex",
            api_key="expired-token",
            provider="openai-codex",
        )


@pytest.mark.parametrize("model", _IDS)
def test_bare_slug_resolves_to_verified_window_under_large(large, model):
    assert mm.is_codex_900k_base(model) is True
    assert mm._resolve_codex_oauth_context_length(model) == 900_000
    assert _codex_ctx(model) == 900_000
    # The grant stays under the measured ~922,000 input ceiling.
    assert _codex_ctx(model) < 918_028


@pytest.mark.parametrize("model", _IDS)
def test_900k_alias_resolves_to_verified_window(advertised, model):
    assert mm.is_codex_context_variant(f"openai-codex/{model}-900k") is True
    assert mm.strip_codex_context_variant_suffix(f"{model}-900k") == model
    assert _codex_ctx(f"{model}-900k") == 900_000


@pytest.mark.parametrize("model, advertised_ctx", [("gpt-6.2-sol", 500_000), ("gpt-6.2-astra", 272_000)])
def test_bare_slug_keeps_catalog_window_under_advertised(advertised, model, advertised_ctx):
    assert _codex_ctx(model) == advertised_ctx


def test_live_catalog_500k_advertisement_is_bumped_for_sol(large, monkeypatch):
    """gpt-6.2-sol advertises 500K, not the stale 272K; without its STALE_ADVERTISED row the bare
    slug stays at 500K under ``large`` (the hermes-agent#79 miss for 6.1). The catalog's own
    max_context_window still caps the bump (#105443)."""
    monkeypatch.setattr(mm, "_fetch_codex_oauth_context_lengths_with_source",
                        lambda token, base_url="": ({"gpt-6.2-sol": 500_000}, True))
    fp = mm._codex_oauth_token_fingerprint("tok", "")
    monkeypatch.setitem(mm._codex_oauth_max_context_cache, fp, {})
    assert mm._resolve_codex_oauth_context_length("gpt-6.2-sol", access_token="tok") == 900_000
    monkeypatch.setitem(mm._codex_oauth_max_context_cache, fp, {"gpt-6.2-sol": 872_000})
    assert mm._resolve_codex_oauth_context_length("gpt-6.2-sol", access_token="tok") == 872_000


@pytest.mark.parametrize("slug", ["gpt-6.2-sol-pro", "gpt-6.2-astra-fast", "gpt-6.2", "gpt-6.2-luna"])
def test_no_prefix_leak(slug):
    assert mm.is_codex_900k_base(slug) is False


@pytest.mark.parametrize("model", _IDS)
def test_api_context_length_row(model):
    assert mm.DEFAULT_CONTEXT_LENGTHS[model] == 1_050_000


@pytest.mark.parametrize("model, below_usd, above_usd", [
    ("gpt-6.2-sol", Decimal("12"), Decimal("19")),
    ("gpt-6.2-astra", Decimal("48"), Decimal("76")),
])
def test_pricing_rows(model, below_usd, above_usd):
    entry = get_pricing_entry(model, provider="openai")
    assert entry is not None and entry.tier_threshold_tokens == 272_000
    # 1M input + 1M output at the below-272K rates.
    assert entry.input_cost_per_million + entry.output_cost_per_million == below_usd
    assert entry.input_cost_per_million_above + entry.output_cost_per_million_above == above_usd
    # A real estimate below the tier: 100K in + 100K out is a tenth of the 1M/1M figure.
    cost = estimate_usage_cost(model, CanonicalUsage(input_tokens=100_000, output_tokens=100_000), provider="openai")
    assert cost.amount_usd == below_usd / 10


def test_astra_62_is_not_the_gpt6_astra_row():
    assert _OFFICIAL_DOCS_PRICING[("openai", "gpt-6.2-astra")] != _OFFICIAL_DOCS_PRICING[("openai", "gpt-6-astra")]
    entry = _OFFICIAL_DOCS_PRICING[("openai", "gpt-6.2-astra")]
    assert (entry.cache_read_cost_per_million, entry.cache_write_cost_per_million) == (Decimal("0.40"), Decimal("10.00"))
    assert (entry.cache_read_cost_per_million_above, entry.cache_write_cost_per_million_above) == (
        Decimal("0.80"), Decimal("20.00"))


@pytest.mark.parametrize("model", _IDS)
def test_900k_alias_prices_like_base(model):
    assert get_pricing_entry(f"{model}-900k", provider="openai") == get_pricing_entry(model, provider="openai")


@pytest.mark.parametrize("model", _IDS)
def test_effort_ladder_has_max_and_no_none(model):
    assert codex_supported_efforts(model) == CODEX_ASTRA_EFFORTS
    assert codex_supported_efforts(f"openai-codex/{model}") == CODEX_ASTRA_EFFORTS
    assert clamp_effort("max", codex_supported_efforts(model)) == "max"
    assert clamp_effort("ultra", codex_supported_efforts(model)) == "max"
    assert clamp_effort("none", codex_supported_efforts(model)) == "low"


def test_flagship_gate_covers_gpt62_astra():
    from hermes_cli.model_policy import flagship_model_match

    assert flagship_model_match("gpt-6.2-astra", {}) == "astra"
    assert flagship_model_match("openai-codex/gpt-6.2-astra", {}) == "astra"
    assert flagship_model_match("cpa/gpt-6.2-astra-900k", {}) == "astra"
    assert flagship_model_match("gpt-6.2-sol", {}) is None
