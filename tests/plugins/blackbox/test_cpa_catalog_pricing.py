"""Every chat id the cpa proxy serves prices ``estimated``, never ``unknown`` (t_a9b4f1ad).

The fixture is the live CLIProxyAPI ``/v1/models`` catalog. A chat id in it with no
row in ``_OFFICIAL_DOCS_PRICING`` records an unpriced blackbox turn and trips the
first-unpriced-turn sentinel. Refresh the fixture when the proxy catalog grows;
list image/video/review endpoints under ``non_chat``.
"""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "cpa_models_2026-09-28.json").read_text()
)
_CHAT_IDS = sorted(set(_FIXTURE["ids"]) - set(_FIXTURE["non_chat"]))
_PROXIES = ("cpa", "kimi-code", "kimi-cpa", "cliproxyapi", "custom:cpa")
_USAGE = CanonicalUsage(input_tokens=1_000, output_tokens=100,
                        cache_read_tokens=500, cache_write_tokens=200)


def test_fixture_partition_is_consistent():
    assert set(_FIXTURE["non_chat"]) <= set(_FIXTURE["ids"])
    assert any(m.startswith("kimi-") for m in _CHAT_IDS)
    assert any(m.startswith("grok-") for m in _CHAT_IDS)


@pytest.mark.parametrize("provider", _PROXIES)
@pytest.mark.parametrize("model", _CHAT_IDS)
def test_every_cpa_chat_id_prices_estimated(provider, model):
    result = estimate_usage_cost(model, _USAGE, provider=provider)
    assert result.status == "estimated", (model, provider, result)
    assert result.amount_usd is not None and result.amount_usd > 0


def test_non_proxy_vendor_on_cpa_stays_unpriced():
    # #1411: a vendor with no proxy pricing lane is not priced via cpa.
    result = estimate_usage_cost("claude-opus-4-5", _USAGE, provider="cpa")
    assert result.status == "unknown", result


def test_grok_3_mini_fast_has_its_own_rate():
    # FleetReview 58d4eee8312e: the fast variant is priced above grok-3-mini.
    usage = CanonicalUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    fast = estimate_usage_cost("grok-3-mini-fast", usage, provider="cpa")
    base = estimate_usage_cost("grok-3-mini", usage, provider="cpa")
    assert fast.amount_usd == Decimal("4.60")
    assert fast.amount_usd > base.amount_usd


def test_k3_numbers_unchanged():
    usage = CanonicalUsage(input_tokens=1_000, output_tokens=100, cache_read_tokens=500)
    for model in ("kimi-k3", "kimi-k3-256k", "k3"):
        result = estimate_usage_cost(model, usage, provider="cpa")
        assert result.amount_usd == Decimal("0.00465")
        assert result.pricing_version == "openrouter-kimi-k3-2026-09-27"
