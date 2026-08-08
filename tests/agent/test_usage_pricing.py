from types import SimpleNamespace

from agent.usage_pricing import (
    CanonicalUsage,
    NOTIONAL_ANTHROPIC_PROVIDERS,
    estimate_usage_cost,
    get_pricing_entry,
    has_known_pricing,
    is_notional_anthropic_provider,
    is_notional_subscription_bridge,
    is_notional_xai_provider,
    normalize_usage,
    resolve_billing_route,
)
from agent.usage_pricing import _infer_vendor_from_model


# Representative notional-Anthropic provider keys: the exact base members PLUS a
# spread of the failover family (matched by pattern, not membership). Tests
# that assert "every notional provider prices correctly" iterate THIS, not the
# frozenset — because the frozenset deliberately holds only the base names now.
# 2026-07-08 rename: pools claude-app/claude-pool → claude-apr, claude-bpp →
# claude-bpr; failover lanes claude-api-proxy-fN → claude-apx-N, claude-bridge-fN
# → claude-bpx-N. The old names are retired.
_REPRESENTATIVE_NOTIONAL = [
    "claude-api-proxy",
    "claude-bridge",
    "claude-apr",
    "claude-bpr",
    "yunwu",
    "claude-apx-1",
    "claude-apx-2",
    "claude-apx-5",
    "claude-bpx-1",
    "claude-bpx-3",
    "claude-bpx-5",
]


def test_normalize_usage_reads_deepseek_native_cache_hit_tokens():
    """DeepSeek's native API (api.deepseek.com) reports context-cache hits as
    top-level prompt_cache_hit_tokens / prompt_cache_miss_tokens (with
    prompt_tokens = hit + miss), not OpenAI's nested
    prompt_tokens_details.cached_tokens. Before this fix, direct DeepSeek
    sessions always normalized to cache_read_tokens=0 — cache hits were
    invisible in accounting and billed at the full input rate (#61871)."""
    usage = SimpleNamespace(
        prompt_tokens=2000,
        completion_tokens=400,
        prompt_cache_hit_tokens=1500,
        prompt_cache_miss_tokens=500,
    )

    normalized = normalize_usage(usage, provider="deepseek", api_mode="chat_completions")

    assert normalized.cache_read_tokens == 1500
    # prompt_tokens includes cached; input = 2000 - 1500 = the miss bucket
    assert normalized.input_tokens == 500
    assert normalized.output_tokens == 400


def test_normalize_usage_openai_reads_top_level_anthropic_cache_fields():
    """Some OpenAI-compatible proxies (OpenRouter, Vercel AI Gateway, Cline) expose
    Anthropic-style cache token counts at the top level of the usage object when
    routing Claude models, instead of nesting them in prompt_tokens_details.

    Regression guard for the bug fixed in cline/cline#10266 — before this fix,
    the chat-completions branch of normalize_usage() only read
    prompt_tokens_details.cache_write_tokens and completely missed the
    cache_creation_input_tokens case, so cache writes showed as 0 and reflected
    inputTokens were overstated by the cache-write amount.
    """
    usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=200,
        prompt_tokens_details=SimpleNamespace(cached_tokens=500),
        cache_creation_input_tokens=300,
    )

    normalized = normalize_usage(usage, provider="openrouter", api_mode="chat_completions")

    # Expected: cache read from prompt_tokens_details.cached_tokens (preferred),
    # cache write from top-level cache_creation_input_tokens (fallback).
    assert normalized.cache_read_tokens == 500
    assert normalized.cache_write_tokens == 300
    # input_tokens = prompt_total - cache_read - cache_write = 1000 - 500 - 300 = 200
    assert normalized.input_tokens == 200
    assert normalized.output_tokens == 200


def test_estimate_usage_cost_marks_true_subscription_routes_included(monkeypatch):
    """The included short-circuit must still fire for any route whose
    billing_mode is 'subscription_included'. (Codex is no longer such a route —
    it's now notional-OpenRouter — so we force the mode via the resolver to keep
    covering the included code path.)"""
    from agent import usage_pricing as up

    monkeypatch.setattr(
        "agent.usage_pricing.resolve_billing_route",
        lambda *a, **k: up.BillingRoute(
            provider="flat-sub", model="x", billing_mode="subscription_included"
        ),
    )
    result = estimate_usage_cost(
        "x", CanonicalUsage(input_tokens=1000, output_tokens=500), provider="flat-sub"
    )
    assert result.status == "included"
    assert result.amount_usd is not None and float(result.amount_usd) == 0.0  # type: ignore[arg-type]


def test_notional_anthropic_includes_failover_family_by_pattern():
    """Regression (the recurring lane alias gap): every failover lane —
    claude-apx-N / claude-bpx-N for ANY integer N — must price as
    notional-Anthropic, matched by PATTERN (not frozenset membership) so a new
    lane never needs a code edit. Missing aliases caused -f2 (audit 2026-06-13,
    ~25% of Opus spend) and then claude-pool/-f3/-f4/-f5 (audit 2026-06-17,
    ~$872 over 2 days) to price 'unknown'/$0, blinding /cost. The 2026-07-08
    rename (claude-api-proxy-fN → claude-apx-N, claude-bridge-fN → claude-bpx-N,
    pools → claude-apr/claude-bpr) reintroduced the SAME blind spot until the
    pattern + base set were updated to the new names."""
    for provider in (
        "claude-apx-0", "claude-apx-1", "claude-apx-2",
        "claude-apx-3", "claude-apx-5", "claude-apx-10",
        "claude-bpx-0", "claude-bpx-1", "claude-bpx-2",
        "claude-bpx-3", "claude-bpx-5", "claude-apr", "claude-bpr",
    ):
        assert is_notional_anthropic_provider(provider), provider
        result = estimate_usage_cost(
            "claude-opus-4-8",
            CanonicalUsage(input_tokens=1000, output_tokens=100,
                           cache_read_tokens=5000, cache_write_tokens=200),
            provider=provider,
        )
        assert result.status == "estimated", f"{provider}: {result.status}"
        assert result.amount_usd is not None and float(result.amount_usd) > 0


def test_notional_anthropic_pattern_is_open_ended_on_n():
    """The -N pattern must accept ANY integer N (today -0..-10, tomorrow -11+),
    proving a future failover lane is covered with zero code change."""
    for provider in (
        "claude-bpx-6", "claude-bpx-9", "claude-bpx-12",
        "claude-apx-7", "claude-apx-11", "claude-apx-99",
    ):
        assert is_notional_anthropic_provider(provider), provider
        route = resolve_billing_route("claude-opus-4-8", provider=provider)
        assert route.billing_mode == "official_docs_snapshot", provider


def test_notional_anthropic_pattern_rejects_non_failover():
    """The pattern must be ANCHORED + integer-only — it must NOT match a lookalike
    that isn't a real failover lane (else we'd misprice an unrelated provider as
    Anthropic). Guards against a greedy/unanchored regex. Includes the RETIRED
    old-scheme names (claude-app, claude-pool, claude-api-proxy-fN,
    claude-bridge-fN) which must now be REJECTED — they no longer exist."""
    for provider in (
        "claude-apx-frobnicate", "claude-bpx-foo", "claude-apx-",
        "claude-aprx3", "xclaude-apx-3", "claude-apx-3x",
        "claude-apx-3-extra", "claude-apr-3", "claude-aprx",
        # retired old-scheme names — must NOT match anymore:
        "claude-app", "claude-pool", "claude-bpp",
        "claude-api-proxy-f1", "claude-bridge-f3",
        "openai-codex", "anthropic", "openrouter", "", "   ",
    ):
        assert not is_notional_anthropic_provider(provider), repr(provider)
    # None must be safe too (Optional param)
    assert not is_notional_anthropic_provider(None)


def test_notional_anthropic_providers_price_at_official_rates():
    """The notional Claude subscription proxies/bridges/pool must price
    claude-opus-4-8 at official Anthropic rates ($5/$25 per M, $0.50 cache-read,
    $6.25 cache-write) and label the result 'estimated' — NOT 'unknown' (which
    suppresses /cost cards) and NOT 'included'/$0 (which hides spend in
    rollups). Iterates the representative set incl. -fN pattern members."""
    usage = CanonicalUsage(
        input_tokens=500_000, output_tokens=559,
        cache_read_tokens=499_000, cache_write_tokens=1000,
    )
    # 500000*5/1e6 + 559*25/1e6 + 499000*0.5/1e6 + 1000*6.25/1e6 = 2.769725
    expected = 2.769725
    for provider in _REPRESENTATIVE_NOTIONAL:
        result = estimate_usage_cost("claude-opus-4-8", usage, provider=provider)
        assert result.status == "estimated", f"{provider}: {result.status}"
        assert result.amount_usd is not None, f"{provider} priced None"
        assert float(result.amount_usd) == round(expected, 6), (
            f"{provider}: {result.amount_usd}"
        )


def test_notional_anthropic_route_resolves_to_anthropic_billing():
    """resolve_billing_route must rewrite the proxy provider to 'anthropic' with
    the docs-snapshot billing mode so all downstream pricing lookups work."""
    for provider in _REPRESENTATIVE_NOTIONAL:
        route = resolve_billing_route("claude-opus-4-8", provider=provider)
        assert route.provider == "anthropic", provider
        assert route.billing_mode == "official_docs_snapshot", provider
        assert route.model == "claude-opus-4-8", provider


def test_yunwu_prices_claude_models_at_anthropic_snapshot():
    """Yunwu (云雾) is a real-cash Anthropic-compatible reseller, but by request
    its claude-* turns price at the SAME official Anthropic snapshot the rest of
    the fleet's Claude rows use — no bespoke Yunwu rate table. Every model in
    Ace's starting set must (a) route to provider='anthropic' and (b) yield the
    identical dollar amount a direct 'anthropic' route would, labelled
    'estimated'. Covers both the bare 'claude-haiku-4-5' shown in the picker and
    the dated 'claude-haiku-4-5-20251001' Yunwu actually bills on the wire."""
    usage = CanonicalUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    for model in (
        "claude-opus-4-8",
        "claude-sonnet-5",
        "claude-fable-5",
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
    ):
        route = resolve_billing_route(model, provider="yunwu")
        assert route.provider == "anthropic", model
        assert route.billing_mode == "official_docs_snapshot", model

        yunwu_cost = estimate_usage_cost(model, usage, provider="yunwu")
        anthropic_cost = estimate_usage_cost(model, usage, provider="anthropic")
        assert yunwu_cost.status == "estimated", model
        assert yunwu_cost.amount_usd is not None, model
        assert yunwu_cost.amount_usd == anthropic_cost.amount_usd, (
            f"{model}: yunwu={yunwu_cost.amount_usd} anthropic={anthropic_cost.amount_usd}"
        )


def test_notional_anthropic_accepts_dot_notation_and_prefixed_model():
    """Dot-notation (claude-opus-4.8) and anthropic/ prefix must still resolve
    through the normal Anthropic normalization once the provider is remapped."""
    usage = CanonicalUsage(input_tokens=1000, output_tokens=1000)
    for model in ("claude-opus-4.8", "anthropic/claude-opus-4-8", "claude-opus-4-7"):
        result = estimate_usage_cost(model, usage, provider="claude-bridge")
        assert result.status == "estimated", f"{model}: {result.status}"
        assert result.amount_usd is not None and float(result.amount_usd) > 0  # type: ignore[arg-type]


def test_notional_anthropic_has_known_pricing():
    """has_known_pricing must return True for the notional providers (used by
    callers to decide whether to attempt cost display) — incl. -fN pattern
    members and claude-pool, since it routes through resolve_billing_route."""
    for provider in _REPRESENTATIVE_NOTIONAL:
        assert has_known_pricing("claude-opus-4-8", provider=provider), provider


# --- xAI Grok (SuperGrok/Premium+ OAuth notional + metered api.x.ai) ------------
# xai-oauth fronts Grok on a flat subscription (marginal cash $0) → priced at
# xAI official rates, status "estimated". The metered direct-API provider ("xai")
# prices from the SAME snapshot but bills real dollars. Single-vendor: everything
# routes to the fixed "xai" vendor (contrast the poly-vendor gemini-bridge).
_XAI_GROK_CASES = [
    # (model, in$/Mtok, out$/Mtok)  — 1M in + 1M out = in+out
    ("grok-build-0.1", 1.00, 2.00),
    ("grok-4.5", 2.00, 6.00),
    ("grok-4.3", 1.25, 2.50),
    ("grok-4.20", 1.25, 2.50),
    ("grok-4.20-multi-agent", 1.25, 2.50),
]


def test_notional_xai_predicate_matches_only_oauth():
    assert is_notional_xai_provider("xai-oauth")
    assert is_notional_xai_provider("XAI-OAUTH")  # normalized
    # the metered direct-API key and unrelated providers are NOT notional
    assert not is_notional_xai_provider("xai")
    assert not is_notional_xai_provider("openai")
    assert not is_notional_xai_provider("")
    assert not is_notional_xai_provider(None)


def test_xai_oauth_route_resolves_to_xai_billing():
    """resolve_billing_route rewrites xai-oauth AND the metered xai/x-ai/xai-api
    provider keys to provider 'xai' with the docs-snapshot billing mode."""
    for provider in ("xai-oauth", "xai", "x-ai", "xai-api"):
        route = resolve_billing_route("grok-build-0.1", provider=provider)
        assert route.provider == "xai", provider
        assert route.billing_mode == "official_docs_snapshot", provider
        assert route.model == "grok-build-0.1", provider


def test_xai_metered_route_resolves_via_base_url():
    """The metered direct API is also reachable by base_url host match
    (api.x.ai) with no explicit provider — it must still route to 'xai'."""
    route = resolve_billing_route(
        "grok-build-0.1", provider="", base_url="https://api.x.ai/v1"
    )
    assert route.provider == "xai"
    assert route.billing_mode == "official_docs_snapshot"
    assert route.model == "grok-build-0.1"


def test_xai_grok_prices_at_official_rates():
    """Every Grok model in the catalog must price at its exact xAI official rate
    (status 'estimated', never 'unknown'/$0) through BOTH the notional OAuth relay
    and the metered direct-API provider."""
    usage = CanonicalUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    for model, in_rate, out_rate in _XAI_GROK_CASES:
        expected = round(in_rate + out_rate, 6)
        for provider in ("xai-oauth", "xai"):
            result = estimate_usage_cost(model, usage, provider=provider)
            assert result.status == "estimated", f"{provider}/{model}: {result.status}"
            assert result.amount_usd is not None, f"{provider}/{model} priced None"
            assert float(result.amount_usd) == expected, (
                f"{provider}/{model}: {result.amount_usd} != {expected}"
            )


def test_grok_vendor_inferred_from_model_id():
    """A bare grok-* id with no/unknown provider must still price via the M1
    vendor-inference fallback (grok- → xai) — mirrors claude-*/gpt-*/gemini-*."""
    assert _infer_vendor_from_model("grok-build-0.1") == "xai"
    usage = CanonicalUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    for provider in ("", None):
        result = estimate_usage_cost("grok-build-0.1", usage, provider=provider)
        assert result.status == "estimated", f"provider={provider!r}: {result.status}"
        assert float(result.amount_usd) == 3.00, f"provider={provider!r}: {result.amount_usd}"


def test_unsupported_grok_model_stays_unpriced():
    """A grok-* id with no pricing entry must NOT masquerade as priced — it stays
    unknown/None rather than inventing a rate."""
    usage = CanonicalUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    result = estimate_usage_cost("grok-nonexistent-99", usage, provider="xai-oauth")
    assert result.amount_usd is None
    assert result.status == "unknown"


def test_xai_has_known_pricing():
    for provider in ("xai-oauth", "xai"):
        assert has_known_pricing("grok-build-0.1", provider=provider), provider


# --- gemini-bridge (Google AI Ultra sub via agy) notional pricing ---------------
# The bridge is Gemini-ONLY: it fronts the Gemini surface of the Ultra sub. (agy
# also advertises Claude/GPT-OSS tiers, but the bridge deliberately excludes them —
# the claude-*/gpt-oss aliases were removed 2026-07-12; zero non-Gemini spend.)
# Each alias must normalize to a canonical priced Gemini model and route to google
# at official-docs rates (status "estimated", never "unknown"/$0).
_GEMINI_BRIDGE_CASES = [
    # (model_arg, expected_vendor, expected_canonical, in$/Mtok, out$/Mtok)
    ("gemini-flash", "google", "gemini-3.5-flash", 1.50, 9.00),
    ("gemini-pro", "google", "gemini-3.1-pro", 2.00, 12.00),
    # the store records the provider-prefixed form too
    ("gemini-bridge/gemini-flash", "google", "gemini-3.5-flash", 1.50, 9.00),
]


def test_gemini_bridge_is_notional_subscription_bridge():
    assert is_notional_subscription_bridge("gemini-bridge")
    assert is_notional_subscription_bridge("GEMINI-BRIDGE")
    assert not is_notional_subscription_bridge("gemini")
    assert not is_notional_subscription_bridge("claude-bridge")
    assert not is_notional_subscription_bridge(None)
    assert not is_notional_subscription_bridge("")


def test_gemini_bridge_routes_alias_to_vendor():
    """resolve_billing_route must normalize each bridge alias to its canonical
    priced model and route to the inferred vendor with docs-snapshot billing."""
    for model, vendor, canonical, _in, _out in _GEMINI_BRIDGE_CASES:
        route = resolve_billing_route(model, provider="gemini-bridge")
        assert route.provider == vendor, f"{model}: {route.provider}"
        assert route.model == canonical, f"{model}: {route.model}"
        assert route.billing_mode == "official_docs_snapshot", model


def test_gemini_bridge_models_price_at_official_rates():
    """Every gemini-bridge model must price 'estimated' (not 'unknown'/$0). For the
    ones with explicit expected rates, assert the exact per-Mtok dollar figure."""
    for model, _vendor, _canonical, in_rate, out_rate in _GEMINI_BRIDGE_CASES:
        usage = CanonicalUsage(input_tokens=1_000_000, output_tokens=1_000_000)
        result = estimate_usage_cost(model, usage, provider="gemini-bridge")
        assert result.status == "estimated", f"{model}: {result.status}"
        assert result.amount_usd is not None, f"{model} priced None"
        assert float(result.amount_usd) > 0, model
        if in_rate is not None and out_rate is not None:
            assert float(result.amount_usd) == round(in_rate + out_rate, 6), (
                f"{model}: {result.amount_usd} != {in_rate + out_rate}"
            )


def test_gemini_bridge_has_known_pricing():
    for model, *_ in _GEMINI_BRIDGE_CASES:
        assert has_known_pricing(model, provider="gemini-bridge"), model


def test_gemini_bridge_unknown_alias_routes_unknown_not_google():
    """An unrecognized bridge model must route as 'unknown', NOT masquerade as a
    priced route — covers BOTH (a) no known vendor prefix, and (b) a prefix-VALID
    but unsupported id the bridge doesn't actually front (e.g. gemini-2.0-flash,
    a real Google model but NOT one the Ultra bridge serves). Either way, pricing
    it would misclassify a typo/misconfig as a valid route in diagnostics."""
    bogus = (
        "mistral-large", "totally-made-up", "flash", "opus",  # no vendor prefix
        "gemini-2.0-flash", "gemini-2.5-flash", "claude-opus-4-8", "gpt-5.5",  # prefix-valid, not fronted
        # Gemini-only bridge: the former claude-*/gpt-oss short aliases are no
        # longer fronted and must now route unknown (removed 2026-07-12).
        "claude-opus", "claude-sonnet", "gpt-oss",
    )
    for model in bogus:
        route = resolve_billing_route(model, provider="gemini-bridge")
        assert route.provider == "unknown", f"{model}: {route.provider}"
        assert route.billing_mode == "unsupported_notional", (
            f"{model}: {route.billing_mode}"
        )


def test_gemini_bridge_unsupported_model_prices_none_not_google_rates():
    """MONEY-PATH regression: an unsupported-but-prefix-valid bridge model
    (gemini-2.0-flash — a real Google model the Ultra bridge does NOT front) must
    price as unknown/$None through the ACTUAL cost entrypoint, NOT get resurrected
    at Google rates by the M1 vendor fallback in _lookup_official_docs_pricing.

    This is the bug the "unsupported_notional" sentinel exists to close:
    resolve_billing_route returns provider="unknown", but M1 independently
    re-infers the vendor from the model id (gemini-2.0-flash → google) and would
    price it ~$0.50/Mtok unless the sentinel suppresses that one fallback.
    """
    usage = CanonicalUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    for model in ("gemini-2.0-flash", "gemini-2.5-flash", "claude-opus-4-8", "gpt-5.5"):
        result = estimate_usage_cost(model, usage, provider="gemini-bridge")
        assert result.status == "unknown", f"{model}: {result.status}"
        assert result.amount_usd is None, f"{model}: priced {result.amount_usd}, expected None"
        assert not has_known_pricing(model, provider="gemini-bridge"), model


def test_m1_vendor_fallback_still_prices_vendor_named_model_on_mismatched_provider():
    """GUARD the sentinel didn't break M1's real job: a vendor-named model on a
    MISMATCHED/unknown provider (not a notional bridge) must STILL price via the
    M1 vendor fallback. The sentinel suppression must be scoped to
    'unsupported_notional', never blanket-applied to every 'unknown' route — a
    custom/localhost endpoint serving 'claude-opus-4-6' relies on M1 to price it.
    """
    usage = CanonicalUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    # A real Anthropic model id arriving on a non-anthropic/unknown provider: M1
    # infers vendor=anthropic and prices it. This is the exact case M1 was built for.
    result = estimate_usage_cost("claude-opus-4-6", usage, provider="some-custom-proxy")
    assert result.status == "estimated", f"status={result.status}"
    assert result.amount_usd is not None and float(result.amount_usd) > 0, (
        f"M1 fallback regressed: claude-opus-4-6 on a mismatched provider priced "
        f"{result.amount_usd} (expected a positive amount via vendor inference)"
    )


_CODEX_STUB_METADATA = {
    "gpt-5.5": {"pricing": {"prompt": "0.000005", "completion": "0.00003"}},
    "gpt-5.4": {"pricing": {"prompt": "0.0000025", "completion": "0.000015"}},
    "gpt-5.3-codex": {"pricing": {"prompt": "0.00000175", "completion": "0.000014"}},
}


def test_anthropic_dated_release_suffix_falls_back_to_base_snapshot():
    """A new-scheme Anthropic id with a trailing -YYYYMMDD release date that has
    no explicit snapshot entry must fall back to its base model's pricing —
    NOT price 'unknown'. Fixes the dated-Haiku gap (claude-haiku-4-5-20251001
    priced $0 while bare claude-haiku-4-5 priced fine; audit 2026-06-17)."""
    usage = CanonicalUsage(input_tokens=1000, output_tokens=100,
                           cache_read_tokens=5000, cache_write_tokens=200)
    base = estimate_usage_cost("claude-haiku-4-5", usage, provider="claude-pool")
    dated = estimate_usage_cost("claude-haiku-4-5-20251001", usage, provider="claude-pool")
    assert base.status == "estimated"
    assert dated.status == "estimated", f"dated priced {dated.status}"
    assert dated.amount_usd is not None and dated.amount_usd == base.amount_usd, (
        f"dated {dated.amount_usd} != base {base.amount_usd}"
    )
    # Also covers a hypothetical future dated Opus id.
    opus = estimate_usage_cost("claude-opus-4-8-20260115", usage, provider="claude-bridge")
    assert opus.status == "estimated" and opus.amount_usd is not None


def test_anthropic_old_scheme_dated_model_is_not_date_stripped():
    """No-regression guard: an OLD-scheme id whose date is PART of the canonical
    name (claude-3-5-haiku-20241022) must hit its OWN snapshot entry directly,
    never get date-stripped to a non-existent base (claude-3-5-haiku). It has
    its own distinct rate, so stripping would mis-price it."""
    from agent.usage_pricing import _strip_anthropic_release_date
    # The strip helper must DECLINE the old-scheme name (no -N-N version tail).
    assert _strip_anthropic_release_date("claude-3-5-haiku-20241022") is None
    assert _strip_anthropic_release_date("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    assert _strip_anthropic_release_date("claude-opus-4-8-20260115") == "claude-opus-4-8"
    # Non-dated / partial-date names are left alone.
    for n in ("claude-haiku-4-5", "claude-opus-4-8", "claude-haiku-4-5-2025", "gpt-5.5"):
        assert _strip_anthropic_release_date(n) is None, n
    # End-to-end: the old-scheme dated Haiku still prices via its own entry,
    # and its rate is DISTINCT from the new 4-5 Haiku (proves no cross-contamination).
    usage = CanonicalUsage(input_tokens=1_000_000, output_tokens=0)
    old = estimate_usage_cost("claude-3-5-haiku-20241022", usage, provider="anthropic")
    new = estimate_usage_cost("claude-haiku-4-5", usage, provider="anthropic")
    assert old.status == "estimated" and new.status == "estimated"
    assert old.amount_usd != new.amount_usd, "old-scheme must keep its own rate"


def test_openai_codex_route_resolves_to_notional_openrouter():
    """openai-codex must resolve to an OpenRouter-priced route (notional cost
    visibility), NOT subscription_included/$0 — so /cost cards can fire."""
    from agent.usage_pricing import NOTIONAL_OPENROUTER_PROVIDERS

    assert "openai-codex" in NOTIONAL_OPENROUTER_PROVIDERS
    route = resolve_billing_route("gpt-5.5", provider="openai-codex")
    assert route.provider == "openrouter"
    assert route.billing_mode == "official_models_api"
    assert route.model == "gpt-5.5"


def test_openai_codex_priced_from_openrouter_catalog(monkeypatch):
    """A codex turn is priced at the underlying OpenAI model's live OpenRouter
    rate and labelled 'estimated'."""
    # Pin the external source to OpenRouter so this test exercises the OpenRouter
    # catalog path it names (the default is now models.dev, which would answer
    # gpt-5.5 first and ignore the stub).
    monkeypatch.setattr(
        "agent.usage_pricing._pricing_source_order", lambda: ("openrouter",)
    )
    monkeypatch.setattr(
        "agent.usage_pricing.fetch_model_metadata", lambda: _CODEX_STUB_METADATA
    )
    usage = CanonicalUsage(input_tokens=120_000, output_tokens=8_000)
    # 120000*5/1e6 + 8000*30/1e6 = 0.6 + 0.24 = 0.84
    result = estimate_usage_cost("gpt-5.5", usage, provider="openai-codex")
    assert result.status == "estimated"
    assert result.amount_usd is not None and float(result.amount_usd) == 0.84  # type: ignore[arg-type]
    assert result.source == "provider_models_api"


def test_openai_codex_minus_codex_suffix_falls_back_to_base_model(monkeypatch):
    """gpt-5.5-codex is absent from the OpenRouter catalog while gpt-5.5 is
    present; the '-codex' fallback must price it as the base model."""
    monkeypatch.setattr(
        "agent.usage_pricing._pricing_source_order", lambda: ("openrouter",)
    )
    monkeypatch.setattr(
        "agent.usage_pricing.fetch_model_metadata", lambda: _CODEX_STUB_METADATA
    )
    usage = CanonicalUsage(input_tokens=120_000, output_tokens=8_000)
    result = estimate_usage_cost("gpt-5.5-codex", usage, provider="openai-codex")
    assert result.status == "estimated"
    assert result.amount_usd is not None and float(result.amount_usd) == 0.84  # type: ignore[arg-type]


def test_openai_codex_exact_codex_id_preferred_over_fallback(monkeypatch):
    """When the exact '-codex' id IS in the catalog, use it directly (no strip)."""
    monkeypatch.setattr(
        "agent.usage_pricing._pricing_source_order", lambda: ("openrouter",)
    )
    monkeypatch.setattr(
        "agent.usage_pricing.fetch_model_metadata", lambda: _CODEX_STUB_METADATA
    )
    entry = get_pricing_entry("gpt-5.3-codex", provider="openai-codex")
    assert entry is not None
    assert float(entry.input_cost_per_million) == 1.75  # type: ignore[arg-type]
    assert float(entry.output_cost_per_million) == 14.0  # type: ignore[arg-type]


def test_openai_codex_unknown_model_stays_unknown(monkeypatch):
    """A codex model absent from the catalog with no base fallback must degrade
    to 'unknown' (NOT fabricate a price)."""
    # Pin to OpenRouter AND ensure models.dev can't answer the imaginary id
    # either (it can't — but pinning keeps the test source-explicit).
    monkeypatch.setattr(
        "agent.usage_pricing._pricing_source_order", lambda: ("openrouter",)
    )
    monkeypatch.setattr(
        "agent.usage_pricing.fetch_model_metadata", lambda: _CODEX_STUB_METADATA
    )
    usage = CanonicalUsage(input_tokens=1000, output_tokens=500)
    result = estimate_usage_cost("gpt-9-imaginary", usage, provider="openai-codex")
    assert result.status == "unknown"
    assert result.amount_usd is None


def test_estimate_usage_cost_refuses_cache_pricing_without_official_cache_rate(monkeypatch):
    # This exercises the OpenRouter path's refuse-cache-without-official-rate
    # contract: a route with input/output but NO cache rate must NOT fabricate
    # a cache cost. Pin the external source to OpenRouter only — otherwise the
    # default models.dev source supplies a real cache_read rate for
    # gemini-2.5-pro and the turn prices honestly (a different, valid path).
    monkeypatch.setattr(
        "agent.usage_pricing._pricing_source_order", lambda: ("openrouter",)
    )
    monkeypatch.setattr(
        "agent.usage_pricing.fetch_model_metadata",
        lambda: {
            "google/gemini-2.5-pro": {
                "pricing": {
                    "prompt": "0.00000125",
                    "completion": "0.00001",
                }
            }
        },
    )

    result = estimate_usage_cost(
        "google/gemini-2.5-pro",
        CanonicalUsage(input_tokens=1000, output_tokens=500, cache_read_tokens=100),
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
    )

    assert result.status == "unknown"


def test_estimate_usage_cost_prices_cache_write_at_input_rate_when_no_cache_write_rate(monkeypatch):
    """OpenAI-family routes (e.g. gpt-5.x via codex/openrouter) publish no
    separate cache-write rate — cache-write tokens are billed at the input
    rate. The estimator must price the turn (status 'estimated'), NOT drop it
    as unpriced and silently lose real spend. Regression for the lone
    remaining 'unpriced' turn on the tokens.ace by-profile chart (2026-06)."""
    monkeypatch.setattr(
        "agent.usage_pricing.fetch_model_metadata",
        lambda: {
            "gpt-5.5": {
                "pricing": {
                    "prompt": "0.000005",       # $5/M input
                    "completion": "0.00003",    # $30/M output
                    "input_cache_read": "0.0000005",  # $0.50/M cache-read
                    # NO cache-write / input_cache_write key — the real-world gap
                }
            }
        },
    )

    result = estimate_usage_cost(
        "gpt-5.5",
        CanonicalUsage(
            input_tokens=6,
            output_tokens=3104,
            cache_read_tokens=206366,
            cache_write_tokens=114435,
        ),
        provider="openai-codex",
    )

    assert result.status == "estimated"
    assert result.amount_usd is not None
    # cache-write billed at the $5/M input rate, not dropped.
    expected = (6 * 5 + 3104 * 30 + 206366 * 0.5 + 114435 * 5) / 1_000_000
    assert abs(float(result.amount_usd) - expected) < 1e-4
    assert any("input rate" in n for n in result.notes)


def test_estimate_usage_cost_still_unknown_when_input_and_cache_write_both_missing(monkeypatch):
    """If BOTH the input rate AND the cache-write rate are missing, a turn with
    cache-write tokens is genuinely unpriceable — still bail to unknown (the
    input-rate fallback must not paper over a truly missing price)."""
    monkeypatch.setattr(
        "agent.usage_pricing.fetch_model_metadata",
        lambda: {
            "mystery-model": {
                "pricing": {
                    "completion": "0.00001",  # only output priced
                }
            }
        },
    )

    result = estimate_usage_cost(
        "mystery-model",
        CanonicalUsage(input_tokens=0, output_tokens=10, cache_write_tokens=5000),
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
    )

    assert result.status == "unknown"


def test_deepseek_v4_pro_pricing_entry_exists():
    """Regression test: deepseek-v4-pro must have a pricing entry.

    Before this fix, deepseek-v4-pro sessions showed as unknown cost
    in hermes insights because the _OFFICIAL_DOCS_PRICING table had no
    entry for that model.  See #24218.  Rates track the 2026-07 price cut
    ($1.74/$3.48 → $0.435/$0.87).
    """
    entry = get_pricing_entry(
        "deepseek-v4-pro",
        provider="deepseek",
    )

    assert entry is not None
    assert entry.input_cost_per_million is not None
    assert entry.output_cost_per_million is not None
    assert float(entry.input_cost_per_million) == 0.435
    assert float(entry.output_cost_per_million) == 0.87
    assert float(entry.cache_read_cost_per_million) == 0.003625


def test_deepseek_deprecated_aliases_price_as_v4_flash():
    """Invariant: deepseek-chat / deepseek-reasoner are deprecated aliases for
    deepseek-v4-flash's non-thinking / thinking modes (deprecation 2026-07-24)
    — they must bill at identical rates to the flash entry, or sessions on the
    legacy names over/under-report cost."""
    flash = get_pricing_entry("deepseek-v4-flash", provider="deepseek")
    assert flash is not None
    for alias in ("deepseek-chat", "deepseek-reasoner"):
        entry = get_pricing_entry(alias, provider="deepseek")
        assert entry is not None, alias
        assert entry.input_cost_per_million == flash.input_cost_per_million, alias
        assert entry.output_cost_per_million == flash.output_cost_per_million, alias
        assert (
            entry.cache_read_cost_per_million == flash.cache_read_cost_per_million
        ), alias


# ---------------------------------------------------------------------------
# SPEC-C — per-class cost breakdown on CostResult (tokens.ace priciest hover).
# The engine already computes each class term then sums; these assert it now
# ALSO exposes the four parts, that they reconcile to amount_usd, and that the
# OpenAI-family cache-write-at-input-rate path attributes to cache_write.
# ---------------------------------------------------------------------------
def test_cost_result_exposes_per_class_breakdown_summing_to_total():
    usage = CanonicalUsage(
        input_tokens=0,
        output_tokens=130_000,
        cache_read_tokens=109_700_000,
        cache_write_tokens=646_000,
    )
    r = estimate_usage_cost("claude-opus-4-8", usage, provider="claude-api-proxy")
    assert r.status == "estimated"
    # the four new per-class fields exist and are non-None on a priced turn
    assert r.cost_input_usd is not None
    assert r.cost_output_usd is not None
    assert r.cost_cache_read_usd is not None
    assert r.cost_cache_write_usd is not None
    # they reconcile EXACTLY to the total (Decimal arithmetic, same terms)
    parts = (r.cost_input_usd + r.cost_output_usd
             + r.cost_cache_read_usd + r.cost_cache_write_usd)
    assert parts == r.amount_usd
    # and each equals tokens × the published rate
    from agent.usage_pricing import get_pricing_entry
    e = get_pricing_entry("claude-opus-4-8", provider="claude-api-proxy")
    assert r.cost_cache_read_usd == (
        __import__("decimal").Decimal(109_700_000) * e.cache_read_cost_per_million
        / __import__("decimal").Decimal(1_000_000))


def test_per_class_unknown_route_leaves_all_four_none():
    usage = CanonicalUsage(input_tokens=1000, output_tokens=500)
    r = estimate_usage_cost("totally-unknown-model-xyz", usage, provider="mystery")
    assert r.status == "unknown"
    assert r.amount_usd is None
    assert r.cost_input_usd is None
    assert r.cost_output_usd is None
    assert r.cost_cache_read_usd is None
    assert r.cost_cache_write_usd is None


def test_per_class_pricing_is_linear_under_aggregation():
    # INV-7: price(A) + price(B) == price(A+B). The backfill (one aggregate
    # call) reproduces the live per-call sum ONLY if this holds.
    A = CanonicalUsage(input_tokens=1000, output_tokens=500,
                       cache_read_tokens=2_000_000, cache_write_tokens=10_000)
    B = CanonicalUsage(input_tokens=3000, output_tokens=20_000,
                       cache_read_tokens=500_000, cache_write_tokens=0)
    AB = CanonicalUsage(input_tokens=4000, output_tokens=20_500,
                        cache_read_tokens=2_500_000, cache_write_tokens=10_000)
    ra = estimate_usage_cost("claude-opus-4-8", A, provider="claude-api-proxy")
    rb = estimate_usage_cost("claude-opus-4-8", B, provider="claude-api-proxy")
    rab = estimate_usage_cost("claude-opus-4-8", AB, provider="claude-api-proxy")
    assert ra.amount_usd + rb.amount_usd == rab.amount_usd
    # per-class is linear too
    assert ra.cost_cache_read_usd + rb.cost_cache_read_usd == rab.cost_cache_read_usd
    assert ra.cost_output_usd + rb.cost_output_usd == rab.cost_output_usd


def test_cache_write_at_input_rate_attributes_to_cache_write_not_uncached():
    # INV-6: an OpenAI-family route with no separate cache-write rate bills
    # cache-write at the INPUT rate — that $ belongs to cost_cache_write_usd,
    # and cost_input_usd reflects only the real input_tokens.
    from agent.usage_pricing import get_pricing_entry
    e = get_pricing_entry("gpt-5.5", provider="openrouter")
    if e is None or e.input_cost_per_million is None:
        import pytest
        pytest.skip("gpt-5.5 openrouter pricing unavailable in this env")
    # only proceed if this route truly has no separate cache-write rate
    if e.cache_write_cost_per_million is not None:
        import pytest
        pytest.skip("route publishes a separate cache-write rate; INV-6 path N/A")
    usage = CanonicalUsage(input_tokens=1000, output_tokens=0,
                           cache_read_tokens=0, cache_write_tokens=2000)
    r = estimate_usage_cost("gpt-5.5", usage, provider="openrouter")
    from decimal import Decimal
    expect_cw = Decimal(2000) * e.input_cost_per_million / Decimal(1_000_000)
    expect_in = Decimal(1000) * e.input_cost_per_million / Decimal(1_000_000)
    assert r.cost_cache_write_usd == expect_cw
    assert r.cost_input_usd == expect_in
    # the cache-write $ must NOT be folded into uncached/input
    assert r.cost_input_usd != r.cost_cache_write_usd or 1000 == 2000


def test_bedrock_claude_rows_all_carry_cache_pricing():
    """Invariant: every Bedrock Claude pricing row must carry cache-read AND
    cache-write rates, otherwise a cached session prices as ``unknown``.

    Bedrock Claude routes through the AnthropicBedrock SDK and injects
    cache_control, so cached tokens are always reported — the pricing layer
    must be able to value them.  See #50295.
    """
    from agent.usage_pricing import _OFFICIAL_DOCS_PRICING

    claude_rows = [
        (prov, model)
        for (prov, model) in _OFFICIAL_DOCS_PRICING
        if prov == "bedrock" and "claude" in model
    ]
    assert claude_rows, "expected at least one bedrock Claude pricing row"
    for key in claude_rows:
        entry = _OFFICIAL_DOCS_PRICING[key]
        assert entry.input_cost_per_million is not None, key
        assert entry.cache_read_cost_per_million is not None, key
        assert entry.cache_write_cost_per_million is not None, key
        # Cache reads are cheaper than fresh input; cache writes cost more.
        assert entry.cache_read_cost_per_million < entry.input_cost_per_million, key
        assert entry.cache_write_cost_per_million > entry.input_cost_per_million, key


def test_bedrock_current_gen_claude_rows_resolve():
    """Current-gen Claude models (Opus 4.8/4.7, Sonnet 5) must have Bedrock
    pricing rows so cached sessions report a dollar cost, not ``unknown``.
    Assert each resolves via the bare id and a cross-region inference profile
    (us./global. prefix), that every id for a given model resolves to the same
    entry, and that the row carries the cache fields a Bedrock Claude session
    needs.

    (Version-suffixed IDs like ``...-v1:0`` are covered separately by the
    normalizer test in the suffix-strip change; this test intentionally sticks
    to id shapes that resolve on ``main`` so it is independent of that PR.)
    """
    url = "https://bedrock-runtime.us-east-1.amazonaws.com"
    for bare in (
        "anthropic.claude-opus-4-8",
        "anthropic.claude-opus-4-7",
        "anthropic.claude-sonnet-5",
    ):
        ref = get_pricing_entry(bare, provider="bedrock", base_url=url)
        assert ref is not None, bare
        assert ref.input_cost_per_million is not None, bare
        assert ref.output_cost_per_million is not None, bare
        # Output costs more than input across the Claude line; sanity-check the
        # row isn't malformed (input < output).
        assert ref.output_cost_per_million > ref.input_cost_per_million, bare
        # Cache fields present so cached sessions price correctly (the #50295
        # symptom was unknown cost on cached Bedrock Claude sessions).
        assert ref.cache_read_cost_per_million is not None, bare
        assert ref.cache_write_cost_per_million is not None, bare
        # Cross-region inference profiles resolve to the same entry.
        for mid in (f"us.{bare}", f"global.{bare}"):
            entry = get_pricing_entry(mid, provider="bedrock", base_url=url)
            assert entry is not None, mid
            assert entry.input_cost_per_million == ref.input_cost_per_million, mid
            assert entry.output_cost_per_million == ref.output_cost_per_million, mid


def test_bedrock_versioned_inference_profile_resolves_to_bare_pricing():
    """Bedrock profile IDs may include the provider's dated version suffix.

    The pricing table intentionally uses shorter model-family IDs, so the
    lookup needs a longest-prefix fallback after stripping the region scope.
    """
    bare = get_pricing_entry("anthropic.claude-sonnet-4-6", provider="bedrock")
    assert bare is not None

    for model in (
        "us.anthropic.claude-sonnet-4-6-20250514-v1:0",
        "global.anthropic.claude-sonnet-4-6-20250514-v1:0",
    ):
        scoped = get_pricing_entry(model, provider="bedrock")
        assert scoped is not None, model
        assert scoped.input_cost_per_million == bare.input_cost_per_million
        assert scoped.output_cost_per_million == bare.output_cost_per_million
        assert scoped.cache_read_cost_per_million == bare.cache_read_cost_per_million
        assert scoped.cache_write_cost_per_million == bare.cache_write_cost_per_million


def test_bedrock_claude_cached_session_estimates_cost_not_unknown():
    """A Bedrock Claude session with cache hits must produce a dollar estimate,
    not ``unknown`` — the user-visible symptom in #50295.
    """
    bedrock_url = "https://bedrock-runtime.us-east-1.amazonaws.com"
    usage = SimpleNamespace(
        input_tokens=55,
        output_tokens=7113,
        cache_read_input_tokens=1369379,
        cache_creation_input_tokens=42135,
    )
    canonical = normalize_usage(usage, provider="bedrock", api_mode="anthropic_messages")
    assert canonical.cache_read_tokens == 1369379
    assert canonical.cache_write_tokens == 42135

    result = estimate_usage_cost(
        "us.anthropic.claude-opus-4-6",
        canonical,
        provider="bedrock",
        base_url=bedrock_url,
    )
    assert result.status == "estimated"
    assert result.amount_usd is not None


def test_fable_5_prices_at_premium_tier_across_notional_providers():
    """claude-fable-5 (added after real claude-app turns landed unpriced,
    2026-07-01 sweep alert) must price at its premium tier — $10/$50 per M,
    $1.00 cache-read, $12.50 cache-write — through every notional provider
    AND the bare anthropic route, with status 'estimated', never 'unknown'."""
    usage = CanonicalUsage(
        input_tokens=100_000, output_tokens=10_000,
        cache_read_tokens=200_000, cache_write_tokens=4_000,
    )
    # 100000*10/1e6 + 10000*50/1e6 + 200000*1.00/1e6 + 4000*12.50/1e6 = 1.75
    expected = 1.75
    # "" and None cover the provider-less rows the blackbox store actually
    # contains (priced via the vendor-inference fallback, not the notional map).
    for provider in list(_REPRESENTATIVE_NOTIONAL) + ["anthropic", "", None]:
        result = estimate_usage_cost("claude-fable-5", usage, provider=provider)
        assert result.status == "estimated", f"{provider}: {result.status}"
        assert result.amount_usd is not None, f"{provider} priced None"
        assert float(result.amount_usd) == round(expected, 6), (
            f"{provider}: {result.amount_usd}"
        )
        assert has_known_pricing("claude-fable-5", provider=provider), provider


# =========================================================================
# Pluggable external pricing source (models.dev default, OpenRouter fallback)
# =========================================================================
# The external catalog that prices models NOT in the curated snapshot is now
# configurable (config.yaml `pricing.external_source`), defaulting to
# models.dev. These are behavior-contract tests: they assert the INVARIANTS
# (snapshot precedence, source dispatch, fallback ordering, honest cost
# extraction), not frozen provider/model lists.

import agent.usage_pricing as _up  # noqa: E402
import agent.models_dev as _mdev  # noqa: E402


_FAKE_MDEV = {
    "openai": {"models": {"gpt-fake-1": {"cost": {"input": 5, "output": 30, "cache_read": 0.5}}}},
    "google": {"models": {"gemini-fake-1": {"cost": {"input": 2, "output": 12}}}},
    "anthropic": {"models": {"claude-fake-1": {"cost": {"input": 10, "output": 50, "cache_read": 1, "cache_write": 12.5}}}},
}


class TestExternalPricingSource:
    def test_config_can_select_openrouter_primary(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"pricing": {"external_source": "openrouter"}},
        )
        order = _up._pricing_source_order()
        assert order[0] == "openrouter"
        assert "models_dev" in order  # still present as fallback

    def test_default_source_is_models_dev(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
        assert _up._pricing_source_order()[0] == "models_dev"

    def test_unknown_source_degrades_to_default(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"pricing": {"external_source": "nonsense"}},
        )
        assert _up._pricing_source_order()[0] == "models_dev"

    def test_models_dev_entry_builder_extracts_cost_and_cache(self, monkeypatch):
        monkeypatch.setattr(_mdev, "fetch_models_dev", lambda: _FAKE_MDEV)
        route = _up.BillingRoute(
            provider="openai", model="gpt-fake-1", base_url="", billing_mode="official_models_api"
        )
        entry = _up._models_dev_pricing_entry(route)
        assert entry is not None
        assert float(entry.input_cost_per_million) == 5.0
        assert float(entry.output_cost_per_million) == 30.0
        assert float(entry.cache_read_cost_per_million) == 0.5
        assert entry.cache_write_cost_per_million is None  # absent in fixture
        assert entry.pricing_version == "models-dev-api"

    def test_models_dev_entry_none_for_absent_model(self, monkeypatch):
        monkeypatch.setattr(_mdev, "fetch_models_dev", lambda: _FAKE_MDEV)
        route = _up.BillingRoute(
            provider="openai", model="no-such-id", base_url="", billing_mode="official_models_api"
        )
        assert _up._models_dev_pricing_entry(route) is None

    def test_external_entry_falls_back_to_openrouter_when_models_dev_misses(self, monkeypatch):
        # models.dev returns nothing for this id; OpenRouter fallback answers.
        monkeypatch.setattr(_mdev, "fetch_models_dev", lambda: {})
        monkeypatch.setattr(
            "agent.usage_pricing.fetch_model_metadata",
            lambda: {"or-only-model": {"pricing": {"prompt": "0.000004", "completion": "0.00002"}}},
        )
        route = _up.BillingRoute(
            provider="openrouter", model="or-only-model", base_url="", billing_mode="official_models_api"
        )
        entry = _up._external_pricing_entry(route)
        assert entry is not None
        assert float(entry.input_cost_per_million) == 4.0
        assert float(entry.output_cost_per_million) == 20.0
        assert entry.pricing_version == "openrouter-models-api"

    def test_snapshot_precedence_over_models_dev_intro_price(self, monkeypatch):
        # INVARIANT: the curated snapshot wins over the external catalog. Model
        # a models.dev "intro" rate cheaper than the snapshot list rate; the
        # snapshot's list rate must still be returned for a snapshot-covered
        # model. (Mirrors real Sonnet-5 $3/$15 list vs models.dev $2/$10 intro.)
        monkeypatch.setattr(
            _mdev,
            "fetch_models_dev",
            lambda: {"anthropic": {"models": {"claude-sonnet-5": {"cost": {"input": 2, "output": 10}}}}},
        )
        entry = get_pricing_entry("claude-sonnet-5", provider="anthropic")
        assert entry is not None
        assert entry.source == "official_docs_snapshot"
        assert float(entry.input_cost_per_million) == 3.0  # list, NOT the 2.0 intro
        assert float(entry.output_cost_per_million) == 15.0

    def test_models_dev_prices_openrouter_relay_model(self, monkeypatch):
        # A notional-OpenRouter relay model (openai-codex -> openrouter route)
        # absent from the snapshot prices from models.dev (the new default).
        monkeypatch.setattr(_mdev, "fetch_models_dev", lambda: _FAKE_MDEV)
        entry = get_pricing_entry("gpt-fake-1", provider="openai-codex")
        assert entry is not None
        assert float(entry.input_cost_per_million) == 5.0
        assert float(entry.output_cost_per_million) == 30.0
        assert entry.source == "provider_models_api"
        assert entry.pricing_version == "models-dev-api"


def test_fireworks_router_fast_tier_prices_distinctly():
    """Fast serving tiers live under accounts/fireworks/routers/<name>-fast and
    bill at higher rates than the standard model — the routing layer's
    rsplit("/", 1) must land on the distinct fast-tier entry."""
    standard = get_pricing_entry(
        "accounts/fireworks/models/kimi-k2p6",
        provider="fireworks",
        base_url="https://api.fireworks.ai/inference/v1",
    )
    fast = get_pricing_entry(
        "accounts/fireworks/routers/kimi-k2p6-fast",
        provider="fireworks",
        base_url="https://api.fireworks.ai/inference/v1",
    )
    assert standard is not None and fast is not None
    assert fast.input_cost_per_million > standard.input_cost_per_million
    assert fast.output_cost_per_million > standard.output_cost_per_million


def test_google_and_vertex_routes_share_official_pricing_snapshot():
    """Direct Gemini, Vertex, and Vertex's OpenAI-compatible hostname must
    all normalize to the Google official-pricing route.
    """
    routes = (
        resolve_billing_route("model", provider="gemini"),
        resolve_billing_route("google/model", provider="vertex"),
        resolve_billing_route(
            "google/model",
            provider="custom",
            base_url="https://aiplatform.googleapis.com/v1/projects/example",
        ),
    )

    assert all(route.provider == "google" for route in routes)
    assert all(route.billing_mode == "official_docs_snapshot" for route in routes)


def test_vertex_default_model_estimates_cached_usage(monkeypatch):
    """The bundled Vertex profile's default auxiliary model must fall back to
    Google snapshot pricing when the OpenAI-compatible endpoint has no model
    metadata, including for cache-read accounting.
    """
    from providers import get_provider_profile

    monkeypatch.setattr(
        "agent.usage_pricing.fetch_endpoint_model_metadata",
        lambda *_args, **_kwargs: {},
    )
    vertex = get_provider_profile("vertex")
    result = estimate_usage_cost(
        vertex.default_aux_model,
        CanonicalUsage(input_tokens=100, output_tokens=100, cache_read_tokens=100),
        provider=vertex.name,
        base_url=vertex.base_url,
    )

    assert result.status == "estimated"
    # Parity note (2026-08-07): the fork's copy asserted a hardcoded
    # ``== 0.28`` carried over from a 1M-input/500K-output scenario, but the
    # call above passes 100/100/100 — the literal never matched its own
    # inputs and simply tracked whichever model ``default_aux_model`` happened
    # to name (now google/gemini-3.6-flash). Upstream asserts the invariant
    # that actually matters: snapshot pricing produced a positive estimate.
    assert result.amount_usd is not None and result.amount_usd > 0


# ── Delegated custom-lane attribution (custom:<name>) ────────────────────────
# A subagent dispatched at a REGISTERED delegation.base_url records its provider
# as the custom-provider pool key (``custom:<name>``) so the turn ledger names
# the relay. When ``<name>`` is a known notional backend the lane must price
# NOTIONAL, not fall through to billing_mode="unknown" (the Week-30 audit gap:
# 14 Opus subagent turns, $166.70 notional, recorded unpriced).

def test_custom_lane_notional_anthropic_relay_routes_to_anthropic():
    """AC3: custom:<notional-anthropic-relay> prices at official Anthropic rates."""
    route = resolve_billing_route("claude-opus-5", provider="custom:claude-apr")
    assert route.provider == "anthropic"
    assert route.billing_mode == "official_docs_snapshot"


def test_custom_lane_notional_failover_family_covered_by_pattern():
    """The numbered -apx-N/-bpx-N failover family is matched by the SAME
    pattern predicate, so a new lane needs no code change here."""
    for lane in ("custom:claude-apx-7", "custom:claude-bpx-12"):
        route = resolve_billing_route("claude-opus-5", provider=lane)
        assert route.provider == "anthropic", lane
        assert route.billing_mode == "official_docs_snapshot", lane


def test_custom_lane_notional_xai_relay_routes_to_xai():
    """Lane unwrapping re-asks every existing notional predicate, not just the
    Anthropic one — a custom: lane over xai-oauth routes to the xai vendor."""
    route = resolve_billing_route("grok-4.5", provider="custom:xai-oauth")
    assert route.provider == "xai"
    assert route.billing_mode == "official_docs_snapshot"


def test_custom_lane_notional_cost_status_is_not_unknown():
    """AC3 (the symptom): a notional custom lane produces a priced turn with a
    cost_status that is NOT "unknown".

    Uses the gemini-bridge lane and its bridge-only ALIAS ``gemini-flash``
    deliberately. For a vendor-NAMED model id (claude-opus-5, grok-4.5) the M1
    last-resort vendor fallback rescues pricing even with the lane
    classification reverted, which would make this assertion vacuous. The
    alias names no vendor M1 can infer and is only resolvable by unwrapping
    the lane to its registered backend — so "unknown" here is caused solely by
    the missing classification. Mutation-proof: revert the lane unwrap in
    resolve_billing_route and this test goes RED with status == "unknown".
    """
    result = estimate_usage_cost(
        "gemini-flash",
        CanonicalUsage(input_tokens=1_000_000, output_tokens=100_000),
        provider="custom:gemini-bridge",
    )
    assert result.status != "unknown"
    assert result.status == "estimated"
    assert result.amount_usd is not None
    assert result.amount_usd > 0


def test_custom_lane_notional_matches_bare_backend_pricing():
    """Invariant (not a snapshot): the lane-labeled turn must cost EXACTLY what
    the same turn costs under the bare backend name. Ties the two spellings
    together so a future rate change can never diverge them."""
    usage = CanonicalUsage(input_tokens=1_000_000, output_tokens=100_000)
    lane = estimate_usage_cost("claude-opus-5", usage, provider="custom:claude-apr")
    bare = estimate_usage_cost("claude-opus-5", usage, provider="claude-apr")
    assert lane.amount_usd == bare.amount_usd
    assert lane.status == bare.status


def test_non_notional_custom_lane_still_unknown():
    """A registered but NON-notional lane (a real metered vendor behind a
    custom endpoint) is left alone — it must not be silently priced $0."""
    route = resolve_billing_route("some-model", provider="custom:together.ai")
    assert route.billing_mode == "unknown"


def test_bare_custom_provider_unchanged():
    """AC2 at the pricing layer: bare "custom" keeps its existing routing."""
    route = resolve_billing_route("some-model", provider="custom")
    assert route.provider == "custom"
    assert route.billing_mode == "unknown"


def test_custom_lane_classification_imports_predicates_not_a_second_list():
    """AC4 (DRY): the lane classifier must delegate to the EXISTING predicates.
    Behavioral proof — extending the single source at runtime immediately
    covers the corresponding custom: lane, which is only possible if no second
    copy of the provider list exists."""
    from agent import usage_pricing as up

    fake = "totally-new-notional-relay"
    assert up._resolve_notional_custom_lane(f"custom:{fake}") is None
    original = up.NOTIONAL_ANTHROPIC_PROVIDERS
    try:
        up.NOTIONAL_ANTHROPIC_PROVIDERS = frozenset(original | {fake})
        # The lane classifier sees the new member with no change of its own.
        assert up._resolve_notional_custom_lane(f"custom:{fake}") == fake
        assert up.resolve_billing_route(
            "claude-opus-5", provider=f"custom:{fake}"
        ).provider == "anthropic"
    finally:
        up.NOTIONAL_ANTHROPIC_PROVIDERS = original
