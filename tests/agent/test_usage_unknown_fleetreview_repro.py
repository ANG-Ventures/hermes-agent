"""Regression pins for the PR #787 FleetReview P1/P2 fixes (kanban t_a17c6966).

Originally written as a scratch repro to produce handback evidence. It is
committed and runs in CI, so it is held to contract-file standards: every
assertion here pins behaviour we want to KEEP, never a pre-fix artifact
(r6 finding 10).
"""
from types import SimpleNamespace

from agent.usage_pricing import (
    CanonicalUsage, estimate_usage_cost, normalize_usage, prompt_tokens_unknown,
)


def test_f1_f8_null_details_container_is_not_an_unknown_count():
    """FINDINGS 1+8: a null details CONTAINER must not collapse a measured input."""
    usage = normalize_usage({"prompt_tokens": 150, "completion_tokens": 50,
                             "prompt_tokens_details": None})
    assert usage.input_tokens == 150
    assert usage.cache_read_tokens_unknown is False
    assert usage.cache_write_tokens_unknown is False
    assert usage.input_tokens_unknown is False
    assert usage.total_tokens_unknown is False
    assert prompt_tokens_unknown(usage) is False


def test_f1_f8_codex_null_input_tokens_details_container_same_rule():
    usage = normalize_usage({"input_tokens": 150, "output_tokens": 50,
                             "input_tokens_details": None},
                            api_mode="codex_responses")
    assert usage.input_tokens == 150
    assert usage.total_tokens_unknown is False


def test_f1_f8_sdk_typed_null_container_same_rule():
    from openai.types.completion_usage import CompletionUsage
    usage = normalize_usage(CompletionUsage.model_construct(
        prompt_tokens=150, completion_tokens=50, total_tokens=200,
        prompt_tokens_details=None))
    assert usage.total_tokens_unknown is False


def test_f1_f8_null_COUNT_inside_a_present_container_is_still_unknown():
    """The narrowing must not weaken the real declaration."""
    usage = normalize_usage({"prompt_tokens": 150, "completion_tokens": 50,
                             "prompt_tokens_details": {"cached_tokens": None}})
    assert usage.cache_read_tokens_unknown is True
    assert usage.input_tokens_unknown is True


def test_f1_f8_explicit_discriminator_flag_is_still_unknown():
    usage = normalize_usage({"prompt_tokens": 150, "completion_tokens": 50,
                             "cache_read_tokens_unavailable": True})
    assert usage.cache_read_tokens_unknown is True


def test_f4_subscription_included_prices_zero_even_when_usage_unknown(monkeypatch):
    """FINDING 4: a $0 route does not depend on token counts.

    The route is pinned directly (the sibling langfuse/pricing tests do the
    same) because which PROVIDER currently resolves to subscription_included
    is catalog churn — the ORDERING of the two branches is the contract.
    """
    import agent.usage_pricing as up

    monkeypatch.setattr(up, "resolve_billing_route", lambda *a, **k: up.BillingRoute(
        provider="flat-sub", model="x", billing_mode="subscription_included"))
    unknown = CanonicalUsage(input_tokens=0, output_tokens=0,
                             output_tokens_unknown=True, usage_unknown=True)
    result = up.estimate_usage_cost("x", unknown, provider="flat-sub")
    assert result.status == "included"
    assert result.amount_usd == 0


def test_f4_non_subscription_route_still_refuses_unknown():
    unknown = CanonicalUsage(input_tokens=0, output_tokens=0, output_tokens_unknown=True)
    result = estimate_usage_cost("claude-sonnet-4-5", unknown, provider="anthropic")
    assert result.status == "unknown"
    assert result.amount_usd is None


def _guard_agent():
    return SimpleNamespace(model="m", provider="claude-bpx-1", api_mode="chat_completions",
                           base_url=None, api_key=None, _empty_content_retries=0)


def test_f5_unknown_output_does_not_classify_as_deterministic_empty():
    """FINDING 5: an UNMEASURED output must not cut the retry budget."""
    from agent.empty_response_guard import deterministic_empty, record_empty_attempt

    agent = _guard_agent()
    unknown_wire = {"prompt_tokens": 150, "completion_tokens": None,
                    "total_tokens": None, "output_tokens_unavailable": True}
    for i in range(2):
        agent._empty_content_retries = i
        record_empty_attempt(agent, finish_reason="stop",
                             response=SimpleNamespace(usage=unknown_wire))
    assert deterministic_empty(agent) is False


def test_f5_measured_zero_output_still_classifies_as_deterministic():
    """Control: the guard must keep working on a genuinely MEASURED zero."""
    from agent.empty_response_guard import deterministic_empty, record_empty_attempt

    agent = _guard_agent()
    measured_wire = {"prompt_tokens": 150, "completion_tokens": 0, "total_tokens": 150}
    for i in range(2):
        agent._empty_content_retries = i
        record_empty_attempt(agent, finish_reason="stop",
                             response=SimpleNamespace(usage=measured_wire))
    assert deterministic_empty(agent) is True


def test_f3_thin_fallback_renderer_is_callable_and_honors_unknown():
    """FINDING 3, the half that belongs to #787: the renderer is EXTRACTED.

    The producer-side plumbing (SessionDB flag columns, absorbing session
    flags, get_last_turn_usage carrying the discriminators) is owned by the
    STACKED PR #797 / card t_25f50547, which already shipped it — see the
    handback. What #787 owes is that the shipped renderer is (a) callable
    without AST-lifting it out of an 11k-line module and (b) correct when the
    producer DOES hand it a flag.
    """
    from gateway.slash_commands import render_thin_last_turn_lines

    unmeasured = {"input_tokens": 150, "output_tokens": 0, "cache_read_tokens": 0,
                  "cache_write_tokens": 0, "reasoning_tokens": 0,
                  "output_tokens_unknown": True}
    text = "\n".join(render_thin_last_turn_lines(unmeasured, "test"))
    assert "Tokens out: unknown" in text
    assert "Total (billed in+out): unknown" in text
    assert "Total (billed in+out): 0" not in text


def test_f3_a_flagless_all_zero_snapshot_is_not_pinned_as_a_measured_zero():
    """r6 finding 10 — this used to ASSERT the defect as expected output.

    The original form asserted ``"Total (billed in+out): 0" in text`` for a
    flagless all-zero snapshot, to document the pre-fix rendering. But that is
    the exact shape the resident ``/usage`` lane still produces (r6 finding 8),
    so the assertion LOCKED IN the measured-looking zero: the conservative
    rendering that finding 8 asks for would have turned this test red. A
    pre-fix behaviour belongs in the PR description, not in a green pin.

    What is worth pinning is the contract that made the fix necessary: the
    renderer must key off the UNKNOWN discriminators, so the flagged shape and
    the flagless shape must not render the same text.
    """
    from gateway.slash_commands import render_thin_last_turn_lines

    zeros = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
             "cache_write_tokens": 0, "reasoning_tokens": 0}
    flagless = "\n".join(render_thin_last_turn_lines(dict(zeros), "test"))
    flagged = "\n".join(
        render_thin_last_turn_lines(
            dict(zeros, output_tokens_unknown=True, input_tokens_unknown=True), "test"
        )
    )
    assert flagged != flagless, (
        "the UNKNOWN discriminators must change what the card renders"
    )
    assert "Total (billed in+out): 0" not in flagged


def test_f6_extracted_helpers_are_callable_without_parsing_source():
    """FINDING 6: the three lifted blocks are now real functions."""
    from agent.usage_pricing import cache_stats_line, verbose_token_usage_log_args

    unknown = normalize_usage({"prompt_tokens": None, "completion_tokens": 50,
                               "total_tokens": None, "prompt_tokens_unavailable": True,
                               "unavailable": True})
    prompt, completion, total = verbose_token_usage_log_args(
        unknown, unknown.prompt_tokens, unknown.output_tokens, unknown.total_tokens)
    assert prompt == "unknown"
    assert completion == "50"
    assert total == "unknown"
    assert cache_stats_line(unknown, unknown.prompt_tokens) == "💾 Cache: unknown"

    measured = normalize_usage({"prompt_tokens": 100, "completion_tokens": 50,
                                "prompt_tokens_details": {"cached_tokens": 100}})
    assert cache_stats_line(measured, measured.prompt_tokens) == (
        "💾 Cache: 100/100 tokens (100% hit, 0 written)")
    assert verbose_token_usage_log_args(measured, 120_000, 8_000, 128_000) == (
        "120,000", "8,000", "128,000")
    assert cache_stats_line(
        normalize_usage({"prompt_tokens": 100, "completion_tokens": 50}), 100) is None
