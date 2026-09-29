"""GPT-6.1 Sol pre-stage (2026-09-29): the runtime knows ``gpt-6.1-sol``
before the Codex lane serves it.

Behaviour contracts, not snapshots:
- context resolves to the documented 1.05M window on the direct API and to
  the advertised 272K on the Codex OAuth offline fallback;
- the ``-900k`` opt-in is NOT granted (no measured max_context_window yet);
- pricing resolves to 6.1's own rates (cache read $0.10) while gpt-6-sol keeps
  its own ($0.20), on both the ``openai`` and ``openai-codex`` routes;
- ``openai-codex/gpt-6.1-sol`` normalizes to the bare slug.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

from agent.model_metadata import get_model_context_length, is_codex_900k_base
from agent.usage_pricing import CanonicalUsage, estimate_usage_cost, get_pricing_entry
from hermes_cli.model_normalize import normalize_model_for_provider


def test_gpt61_sol_context_resolves_to_1m_class_window():
    with patch("agent.model_metadata.get_cached_context_length", return_value=None), \
         patch("agent.model_metadata.fetch_model_metadata", return_value={}), \
         patch("agent.model_metadata.fetch_endpoint_model_metadata", return_value={}), \
         patch("agent.models_dev.lookup_models_dev_context", return_value=None):
        ctx = get_model_context_length("gpt-6.1-sol")
        # Same window as its sibling; must not fall through to the 256K default.
        assert ctx == get_model_context_length("gpt-6-sol")
    assert ctx >= 1_000_000


def test_gpt61_sol_codex_offline_fallback_is_advertised_272k():
    fake_response = MagicMock()
    fake_response.status_code = 401
    fake_response.json.return_value = {}
    import agent.model_metadata as mm
    mm._codex_oauth_context_cache = {}
    with patch("agent.model_metadata.requests.get", return_value=fake_response), \
         patch("agent.model_metadata.get_cached_context_length", return_value=None), \
         patch("agent.model_metadata.save_context_length"):
        ctx = get_model_context_length(
            model="gpt-6.1-sol",
            base_url="https://chatgpt.com/backend-api/codex",
            api_key="expired-token",
            provider="openai-codex",
        )
    assert ctx == 272_000


def test_gpt61_sol_not_900k_eligible_until_measured():
    assert is_codex_900k_base("gpt-6-sol") is True
    assert is_codex_900k_base("gpt-6.1-sol") is False


def test_gpt61_sol_prices_distinct_from_gpt6_sol():
    new = get_pricing_entry("gpt-6.1-sol", provider="openai")
    old = get_pricing_entry("gpt-6-sol", provider="openai")
    assert new is not None and old is not None
    assert new.source == "official_docs_snapshot"
    assert (new.input_cost_per_million, new.output_cost_per_million) == (
        Decimal("2.00"),
        Decimal("10.00"),
    )
    assert new.cache_read_cost_per_million == Decimal("0.10")
    assert new.cache_write_cost_per_million == Decimal("2.50")
    assert new.tier_threshold_tokens == 272_000
    assert new.cache_read_cost_per_million_above == Decimal("0.20")
    # ADD-KEEP: the sibling keeps its own cache-read rate.
    assert old.cache_read_cost_per_million == Decimal("0.20")
    assert old.pricing_version != new.pricing_version


def test_gpt61_sol_costs_nonzero_on_codex_lane():
    below = estimate_usage_cost(
        "gpt-6.1-sol",
        CanonicalUsage(input_tokens=100_000, output_tokens=10_000, cache_read_tokens=100_000),
        provider="openai-codex",
    )
    # 100k * $2/M + 10k * $10/M + 100k * $0.10/M
    assert below.amount_usd == Decimal("0.31")
    above = estimate_usage_cost(
        "gpt-6.1-sol",
        CanonicalUsage(input_tokens=300_000, output_tokens=10_000),
        provider="openai-codex",
    )
    # whole-request tier: 300k * $4/M + 10k * $15/M
    assert above.amount_usd == Decimal("1.35")


def test_openai_codex_prefix_normalizes_to_bare_slug():
    assert normalize_model_for_provider("openai-codex/gpt-6.1-sol", "openai-codex") == "gpt-6.1-sol"
