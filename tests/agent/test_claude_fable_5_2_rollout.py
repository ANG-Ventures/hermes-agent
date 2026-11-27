"""claude-fable-5-2 rollout (2026-11-09, t_c2a562c9): context window, output cap,
pricing row and catalog membership. ADD-KEEP — fable-5-1 stays resolvable.

Values come from models.dev ``anthropic.models.claude-fable-5-2``:
limit.context=1_000_000, limit.output=128_000, cost 8/40/0.20/10 per MTok.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest

from agent.usage_pricing import (
    CanonicalUsage,
    estimate_usage_cost,
    get_pricing_entry,
    has_known_pricing,
)

FABLE_5_2 = "claude-fable-5-2"


def test_context_length_is_1m_from_the_explicit_row():
    from agent.model_metadata import ANTHROPIC_CONTEXT_FALLBACK, get_model_context_length

    assert (FABLE_5_2, 1_000_000) in ANTHROPIC_CONTEXT_FALLBACK
    with patch("agent.model_metadata.get_cached_context_length", return_value=None), \
         patch("agent.model_metadata.fetch_model_metadata", return_value={}), \
         patch("agent.model_metadata.fetch_endpoint_model_metadata", return_value={}), \
         patch("agent.models_dev.lookup_models_dev_context", return_value=None):
        assert get_model_context_length(FABLE_5_2) == 1_000_000
        assert get_model_context_length("claude-fable-5-1") == 1_000_000  # ADD-KEEP


def test_output_cap_is_128k():
    from agent.anthropic_adapter import _get_anthropic_max_output

    assert _get_anthropic_max_output(FABLE_5_2) == 128_000
    assert _get_anthropic_max_output("claude-fable-5.2") == 128_000


def test_pricing_row_matches_models_dev():
    entry = get_pricing_entry(FABLE_5_2, provider="anthropic")
    assert entry is not None
    assert entry.input_cost_per_million == Decimal("8.00")
    assert entry.output_cost_per_million == Decimal("40.00")
    assert entry.cache_read_cost_per_million == Decimal("0.20")
    assert entry.cache_write_cost_per_million == Decimal("10.00")
    assert entry.tier_threshold_tokens is None  # models.dev lists no tier for 5-2
    # ADD-KEEP: the 5-1 row is untouched.
    old = get_pricing_entry("claude-fable-5-1", provider="anthropic")
    assert old is not None and old.input_cost_per_million == Decimal("10.00")


@pytest.mark.parametrize("provider", ["anthropic", "claude-apr", "claude-alr", "claude-bpx-3", "", None])
def test_fable_5_2_turn_prices(provider):
    usage = CanonicalUsage(
        input_tokens=100_000, output_tokens=10_000,
        cache_read_tokens=200_000, cache_write_tokens=4_000,
    )
    # 100000*8/1e6 + 10000*40/1e6 + 200000*0.20/1e6 + 4000*10/1e6 = 1.28
    result = estimate_usage_cost(FABLE_5_2, usage, provider=provider)
    assert result.status != "unknown", provider
    assert result.amount_usd is not None, provider
    assert float(result.amount_usd) == pytest.approx(1.28), (provider, result.amount_usd)
    assert has_known_pricing(FABLE_5_2, provider=provider), provider


def test_model_lists_carry_fable_5_2():
    from hermes_cli.models import OPENROUTER_MODELS, _PROVIDER_MODELS

    assert "claude-fable-5.2" in _PROVIDER_MODELS["anthropic"]
    assert "claude-fable-5.1" in _PROVIDER_MODELS["anthropic"]  # ADD-KEEP
    or_ids = [mid for mid, _ in OPENROUTER_MODELS]
    assert "anthropic/claude-fable-5.2" in or_ids
    assert "anthropic/claude-fable-5.1" in or_ids  # ADD-KEEP


def test_fable_5_2_takes_the_5_5_generation_request_contract():
    from agent.anthropic_adapter import (
        _MANDATORY_THINKING_CLAUDE_SUBSTRINGS,
        _NO_FORCED_TOOL_CHOICE_CLAUDE_SUBSTRINGS,
        _model_matches,
    )

    for mid in (FABLE_5_2, "claude-fable-5.2", "anthropic/claude-fable-5.2"):
        assert _model_matches(mid, _NO_FORCED_TOOL_CHOICE_CLAUDE_SUBSTRINGS), mid
        assert _model_matches(mid, _MANDATORY_THINKING_CLAUDE_SUBSTRINGS), mid
