"""M1 vendor-by-model-name fallback (SPEC §5B). Prove a route whose (provider,model)
misses can still reach the correct vendor's official-docs snapshot when the model id
unambiguously names the vendor — with no infinite recursion and no fabricated price."""
import pytest

from agent.usage_pricing import (
    _infer_vendor_from_model,
    get_pricing_entry,
    _OFFICIAL_DOCS_PRICING,
)


@pytest.mark.parametrize("model,vendor", [
    ("claude-opus-4-8", "anthropic"),
    ("claude-sonnet-5", "anthropic"),
    ("gpt-5.5", "openai"),
    ("o3", "openai"),
    ("o1-mini", "openai"),
    ("gemini-2.5-pro", "google"),
    ("frobnicate-9", None),
    ("", None),
    ("mistral-large", None),
])
def test_infer_vendor_from_model(model, vendor):
    assert _infer_vendor_from_model(model) == vendor


def test_openai_provider_claude_model_reaches_anthropic_snapshot():
    # Class A: proxy provider 'openai' with an Anthropic model must resolve to the
    # anthropic snapshot price, not miss on ('openai','claude-opus-4-8').
    anth = get_pricing_entry("claude-opus-4-8", provider="anthropic")
    assert anth is not None and anth.input_cost_per_million is not None
    mismatched = get_pricing_entry("claude-opus-4-8", provider="openai")
    assert mismatched is not None
    assert mismatched.input_cost_per_million == anth.input_cost_per_million


def test_unknown_provider_claude_model_reaches_anthropic_snapshot():
    # A' path: provider unknown -> billing_mode 'unknown', but model names anthropic.
    anth = get_pricing_entry("claude-opus-4-8", provider="anthropic")
    got = get_pricing_entry("claude-opus-4-8", provider="copilot-acp")
    assert got is not None and got.input_cost_per_million == anth.input_cost_per_million


def test_vendor_fallback_negative_unknown_model_stays_none():
    assert get_pricing_entry("frobnicate-9", provider="openai") is None


def test_vendor_fallback_terminates_when_vendor_equals_provider():
    # provider already anthropic -> no infinite recursion, real match returned.
    assert get_pricing_entry("claude-opus-4-8", provider="anthropic") is not None


def test_vendor_fallback_never_fabricates_when_snapshot_missing():
    # If the inferred vendor has NO snapshot entry for the model, the fallback must
    # return exactly what the direct (vendor, model) lookup gives (None) — never a
    # fabricated price. Uses an inferable-but-absent id.
    direct = _OFFICIAL_DOCS_PRICING.get(("google", "gemini-2.5-pro"))
    got = get_pricing_entry("gemini-2.5-pro", provider="vertex")
    if direct is None:
        assert got is None
    else:
        assert got is not None and got.input_cost_per_million == direct.input_cost_per_million


def test_codex_notional_route_prefers_curated_snapshot_over_external_catalog(monkeypatch):
    """A day-one OpenAI model (gpt-6-astra, 2026-09-04) is priced in the curated
    snapshot before models.dev/OpenRouter list it. The openai-codex notional
    route must consult the snapshot FIRST, or every turn on the new flagship
    resolves to source="none"/$0 while a correct entry sits unused.
    """
    from agent import usage_pricing as up

    # Simulate the external catalogs not knowing the model yet.
    monkeypatch.setattr(up, "_external_pricing_entry", lambda route: None)
    usage = up.CanonicalUsage(input_tokens=1_000_000, output_tokens=0)
    result = up.estimate_usage_cost("gpt-6-astra", usage, provider="openai-codex")
    assert result.source == "official_docs_snapshot"
    assert result.amount_usd == up._OFFICIAL_DOCS_PRICING[("openai", "gpt-6-astra")].input_cost_per_million

    # Models absent from the snapshot still fall through to the external catalog.
    sentinel = up._OFFICIAL_DOCS_PRICING[("openai", "gpt-6-astra")]
    monkeypatch.setattr(up, "_external_pricing_entry", lambda route: sentinel)
    result = up.estimate_usage_cost("gpt-9-not-in-snapshot", usage, provider="openai-codex")
    assert result.source == "official_docs_snapshot"  # came via the external stub
