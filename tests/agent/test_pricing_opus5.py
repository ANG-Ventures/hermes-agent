"""claude-opus-5 rate entry — regression lock (card t_2e382a4b).

The opus-5 launch (2026-07-24) is the incident that motivated the pricing
sentinel: the model went live and every turn recorded unpriced until the rate
landed (PR #419). These tests lock that entry so a future refactor of
``_OFFICIAL_DOCS_PRICING`` can't silently drop it back into the unpriced state,
and prove it prices a synthetic turn correctly across ALL FOUR token classes
(fresh input, output, cache read, cache write) — not just in/out.

Rates (source: https://platform.claude.com/docs/en/about-claude/pricing,
fetched 2026-07-24): $5 input / $25 output per MTok, $0.50 cache read,
$6.25 cache write. claude-opus-5-fast is the 2x tier, mirroring opus-4-8-fast.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agent.usage_pricing import (
    CanonicalUsage,
    estimate_usage_cost,
    get_pricing_entry,
    resolve_billing_route,
)


OPUS5_RATES = {
    "input_cost_per_million": Decimal("5.00"),
    "output_cost_per_million": Decimal("25.00"),
    "cache_read_cost_per_million": Decimal("0.50"),
    "cache_write_cost_per_million": Decimal("6.25"),
}

OPUS5_FAST_RATES = {
    "input_cost_per_million": Decimal("10.00"),
    "output_cost_per_million": Decimal("50.00"),
    "cache_read_cost_per_million": Decimal("1.00"),
    "cache_write_cost_per_million": Decimal("12.50"),
}


@pytest.mark.parametrize(
    "model,rates",
    [("claude-opus-5", OPUS5_RATES), ("claude-opus-5-fast", OPUS5_FAST_RATES)],
)
def test_opus5_entry_carries_all_four_rate_classes(model, rates):
    entry = get_pricing_entry(model, provider="anthropic")
    assert entry is not None, f"{model} is UNPRICED — the sentinel would fire"
    for field, expected in rates.items():
        assert getattr(entry, field) == expected, f"{model}.{field}"
    assert entry.source == "official_docs_snapshot"
    assert entry.source_url == "https://platform.claude.com/docs/en/about-claude/pricing"


def test_opus5_matches_the_opus_4_8_entry_shape():
    """The brief pins opus-5 to the neighbouring 4.8 entry's shape; if a new
    cache class is ever added to PricingEntry, both must gain it together."""
    five = get_pricing_entry("claude-opus-5", provider="anthropic")
    four_eight = get_pricing_entry("claude-opus-4-8", provider="anthropic")
    assert five is not None and four_eight is not None
    # Same base tier ($5/$25) — the launch announcement says so explicitly.
    assert five.input_cost_per_million == four_eight.input_cost_per_million
    assert five.output_cost_per_million == four_eight.output_cost_per_million
    assert five.cache_read_cost_per_million == four_eight.cache_read_cost_per_million
    assert five.cache_write_cost_per_million == four_eight.cache_write_cost_per_million
    # And no rate field is populated on one but not the other.
    for f in five.__dataclass_fields__:
        if f.endswith("_per_million"):
            assert (getattr(five, f) is None) == (getattr(four_eight, f) is None), f


def test_opus5_prices_a_synthetic_turn_including_cache_classes():
    """A realistic cache-heavy turn must price exactly, all four classes."""
    usage = CanonicalUsage(
        input_tokens=500_000,
        output_tokens=559,
        cache_read_tokens=499_000,
        cache_write_tokens=1_000,
    )
    # 500000*5/1e6 + 559*25/1e6 + 499000*0.5/1e6 + 1000*6.25/1e6
    expected = 2.5 + 0.013975 + 0.2495 + 0.00625  # == 2.769725
    result = estimate_usage_cost("claude-opus-5", usage, provider="anthropic")

    assert result.amount_usd is not None, "opus-5 turn priced None"
    assert result.status in {"estimated", "actual"}
    assert float(result.amount_usd) == round(expected, 6)

    # Per-class split must decompose the same total (SPEC-C).
    assert float(result.cost_input_usd) == 2.5
    assert float(result.cost_output_usd) == round(0.013975, 6)
    assert float(result.cost_cache_read_usd) == 0.2495
    assert float(result.cost_cache_write_usd) == 0.00625
    parts = (
        result.cost_input_usd + result.cost_output_usd
        + result.cost_cache_read_usd + result.cost_cache_write_usd
    )
    assert float(parts) == float(result.amount_usd)


def test_opus5_fast_prices_at_double():
    usage = CanonicalUsage(
        input_tokens=100_000, output_tokens=1_000,
        cache_read_tokens=10_000, cache_write_tokens=1_000,
    )
    base = estimate_usage_cost("claude-opus-5", usage, provider="anthropic")
    fast = estimate_usage_cost("claude-opus-5-fast", usage, provider="anthropic")
    assert base.amount_usd is not None and fast.amount_usd is not None
    assert float(fast.amount_usd) == float(base.amount_usd) * 2


@pytest.mark.parametrize("provider", ["claude-apr", "claude-bpr", "claude-apx-3", "yunwu"])
def test_opus5_prices_through_the_notional_relays(provider):
    """The fleet runs opus-5 behind subscription relays; those must price it
    at the same notional rate rather than recording it unknown."""
    route = resolve_billing_route("claude-opus-5", provider=provider)
    assert route.provider == "anthropic"
    assert route.billing_mode == "official_docs_snapshot"

    usage = CanonicalUsage(input_tokens=1_000_000, output_tokens=0)
    result = estimate_usage_cost("claude-opus-5", usage, provider=provider)
    assert result.status == "estimated"
    assert float(result.amount_usd) == 5.0


@pytest.mark.parametrize("alias", ["claude-opus-5", "anthropic/claude-opus-5"])
def test_opus5_aliases_all_resolve_to_the_entry(alias):
    """The vendor-prefixed form must reach the same rate."""
    entry = get_pricing_entry(alias, provider="anthropic")
    assert entry is not None, alias
    assert entry.input_cost_per_million == Decimal("5.00"), alias


def test_single_digit_dated_id_is_a_known_gap_the_sentinel_catches():
    """PRE-EXISTING gap, documented not fixed (card t_2e382a4b).

    ``_ANTHROPIC_DATED_SUFFIX_RE`` requires a TWO-segment version tail
    (``-4-8-20260115``), so a single-segment id like ``claude-opus-5-20260724``
    is not date-stripped and misses the snapshot. Loosening the regex would
    newly strip five existing dated entries (claude-opus-4-20250514,
    claude-sonnet-4-20250514, claude-opus-4-6/4-7-*, claude-sonnet-4-6-*), so
    it is deliberately out of scope for this branch.

    This test pins the CURRENT behaviour and, more usefully, proves the sentinel
    treats such an id as an unpriced model — i.e. the gap now announces itself
    instead of sitting silent, which is the whole point of the sentinel.
    """
    from agent.usage_pricing import _strip_anthropic_release_date, is_known_model

    assert _strip_anthropic_release_date("claude-opus-5-20260724") is None
    assert get_pricing_entry("claude-opus-5-20260724", provider="anthropic") is None
    assert is_known_model("claude-opus-5-20260724", "anthropic") is False


def test_opus5_is_known_to_the_sentinel_probe():
    """The closing of the loop: with the rate landed, the sentinel must NOT
    consider opus-5 a new unpriced model any more."""
    from agent.usage_pricing import is_known_model

    assert is_known_model("claude-opus-5", "anthropic") is True
    assert is_known_model("claude-opus-5-fast", "anthropic") is True
    assert is_known_model("claude-opus-5", "claude-apr") is True
