"""Every Kimi id the cpa proxy serves prices ``estimated``, never ``unknown`` (t_a9b4f1ad).

The fixture is the live CLIProxyAPI ``/v1/models`` catalog. A Kimi id in it with no
row in ``_OFFICIAL_DOCS_PRICING`` records an unpriced blackbox turn and trips the
first-unpriced-turn sentinel. Refresh the fixture when the proxy catalog grows.
"""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

_FIXTURE = Path(__file__).parent / "fixtures" / "cpa_models_2026-09-28.json"
_KIMI_IDS = sorted(m for m in json.loads(_FIXTURE.read_text())["ids"] if m.startswith("kimi-"))
_PROXIES = ("cpa", "kimi-code", "kimi-cpa", "cliproxyapi", "custom:cpa")


def test_fixture_has_kimi_ids():
    assert len(_KIMI_IDS) >= 10


@pytest.mark.parametrize("provider", _PROXIES)
@pytest.mark.parametrize("model", _KIMI_IDS)
def test_every_cpa_kimi_id_prices_estimated(provider, model):
    usage = CanonicalUsage(input_tokens=1_000, output_tokens=100,
                           cache_read_tokens=500, cache_write_tokens=200)
    result = estimate_usage_cost(model, usage, provider=provider)
    assert result.status == "estimated", (model, provider, result)
    assert result.amount_usd is not None and result.amount_usd > 0


def test_k3_numbers_unchanged():
    usage = CanonicalUsage(input_tokens=1_000, output_tokens=100, cache_read_tokens=500)
    for model in ("kimi-k3", "kimi-k3-256k", "k3"):
        result = estimate_usage_cost(model, usage, provider="cpa")
        assert result.amount_usd == Decimal("0.00465")
        assert result.pricing_version == "openrouter-kimi-k3-2026-09-27"
