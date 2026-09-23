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


# ===========================================================================
# r6 round-4 finding 3 — the cost of the F5 trade, pinned AT REAL CONTEXT SIZE.
#
# The F5 hunk above is deliberate and correct: a declared-unmeasured output is
# not a measured zero, so it must not classify as a deterministic empty. But it
# removed the LAST protection for those payloads, because the cost-aware guard
# beside it was ALREADY inert for them — `empty_retry_budget` asks
# `estimate_usage_cost`, which REFUSES any usage with `total_tokens_unknown`,
# gets None, and falls back to the full 3-retry budget.
#
# So a bridge that declares only its OUTPUT unmeasured lost both protections at
# once, and every unsignaled empty re-sent the whole prompt three times — the
# "charged ~$2.33 for an empty answer" incident class that module's own
# docstring names. The F5 pin above could not see it: at `prompt_tokens: 150`
# the cost is nil, so the trade read as free.
#
# The recovery is `measured_cost_floor` — price the MEASURED buckets only, for
# a DECISION, never for display or persistence. It can only understate the bill,
# so a ceiling that trips on it cannot trip early.
# ===========================================================================

_REALISTIC_PROMPT = 400_000
# The bridge route this PR targets, speaking the OpenAI-compatible dialect the
# wire below uses. The dialect matters: normalize_usage(provider="anthropic")
# reads `input_tokens`, not `prompt_tokens`, so an anthropic-labelled route over
# this wire normalizes the prompt to 0 and the fixture silently proves nothing.
_PRICED_ROUTE = {"model": "claude-opus-5", "provider": "claude-bpx-1"}


def _cost_agent():
    return SimpleNamespace(
        model=_PRICED_ROUTE["model"],
        provider=_PRICED_ROUTE["provider"],
        api_mode="chat_completions",
        base_url=None,
        api_key=None,
        _empty_content_retries=0,
    )


def _unsignaled_empty_wire(prompt_tokens):
    """The bridge shape: measured prompt, explicitly UNMEASURED output."""
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": None,
        "total_tokens": None,
        "output_tokens_unavailable": True,
    }


def test_f3_fixture_soundness_the_wire_really_normalizes_to_a_measured_prompt():
    """Guard the fixture itself — a 0-normalized prompt makes every F3 pin vacuous.

    These pins only mean something if the wire's 400k really survives
    `normalize_usage` on the route under test AND the output really is declared
    unmeasured. Get the dialect wrong (e.g. label the route `anthropic`, which
    reads `input_tokens` rather than `prompt_tokens`) and the prompt silently
    normalizes to 0 — both arms then agree for the wrong reason and the suite
    reports a regression as fixed.
    """
    usage = normalize_usage(
        _unsignaled_empty_wire(_REALISTIC_PROMPT),
        provider=_PRICED_ROUTE["provider"],
        api_mode="chat_completions",
    )
    assert usage.prompt_tokens == _REALISTIC_PROMPT, "dialect mismatch — pins vacuous"
    assert usage.output_tokens_unknown is True
    assert usage.total_tokens_unknown is True
    assert usage.input_tokens_unknown is False


def test_f3_the_cost_ceiling_bites_on_an_unmeasured_output_at_real_context_size():
    """The regression the 150-token pin could not see.

    At 400k measured input on a catalogued route, ONE empty attempt is already
    far over the cost threshold — so the budget must drop to 1, not stay at 3.
    Three full re-sends of a 400k prompt is the whole incident class.
    """
    from agent.empty_response_guard import (
        DEFAULT_EMPTY_RETRY_BUDGET,
        REDUCED_EMPTY_RETRY_BUDGET,
        empty_retry_budget,
    )

    agent = _cost_agent()
    response = SimpleNamespace(usage=_unsignaled_empty_wire(_REALISTIC_PROMPT))

    budget = empty_retry_budget(agent, response)
    assert budget == REDUCED_EMPTY_RETRY_BUDGET, (
        "an unmeasured OUTPUT must not buy the full retry budget for a 400k "
        "MEASURED prompt — the input is what an empty attempt actually bills"
    )
    assert budget != DEFAULT_EMPTY_RETRY_BUDGET


def test_f3_a_cheap_prompt_still_gets_the_full_budget():
    """NARROWNESS control — and the reason the old pin read as free.

    The floor must not collapse the budget for every unmeasured output; it is a
    COST ceiling, so a small prompt keeps its retries. This is the same 150-token
    shape `test_f5_unknown_output_does_not_classify_as_deterministic_empty`
    pins, asserted on the cost lane.
    """
    from agent.empty_response_guard import DEFAULT_EMPTY_RETRY_BUDGET, empty_retry_budget

    agent = _cost_agent()
    response = SimpleNamespace(usage=_unsignaled_empty_wire(150))

    assert empty_retry_budget(agent, response) == DEFAULT_EMPTY_RETRY_BUDGET


def test_f3_the_floor_understates_and_never_exceeds_the_real_bill():
    """The floor's safety property, by execution.

    `measured_cost_floor` prices what is known and omits what is not, so it is
    wrong only in the safe direction. A ceiling comparison against it can never
    trip EARLY — which is what makes it legitimate for a decision even though
    `estimate_usage_cost` correctly refuses to state this usage's cost at all.
    """
    from agent.usage_pricing import measured_cost_floor

    unmeasured_output = CanonicalUsage(
        input_tokens=_REALISTIC_PROMPT, output_tokens=0, output_tokens_unknown=True
    )
    # The settled contract is untouched: the real cost is still refused.
    assert estimate_usage_cost(
        _PRICED_ROUTE["model"], unmeasured_output, provider=_PRICED_ROUTE["provider"]
    ).amount_usd is None

    floor = measured_cost_floor(
        _PRICED_ROUTE["model"], unmeasured_output, provider=_PRICED_ROUTE["provider"]
    )
    assert floor is not None and floor > 0

    # Same call had its output been measured at a plausible size: strictly more.
    with_output = CanonicalUsage(input_tokens=_REALISTIC_PROMPT, output_tokens=2_000)
    real = estimate_usage_cost(
        _PRICED_ROUTE["model"], with_output, provider=_PRICED_ROUTE["provider"]
    ).amount_usd
    assert real is not None
    assert floor <= real, "a floor that exceeds the bill would trip the ceiling early"


def test_f3_nothing_measured_has_no_floor_to_state():
    """A wholly unmeasured usage yields None, not a fabricated 0.

    Inventing a $0 floor here would be the silent-zero defect in the decision
    lane — it would read as "this attempt is free" and restore the full budget
    under a different name.
    """
    from agent.usage_pricing import measured_cost_floor

    assert (
        measured_cost_floor(
            _PRICED_ROUTE["model"],
            CanonicalUsage.fully_unknown(),
            provider=_PRICED_ROUTE["provider"],
        )
        is None
    )


def test_f3_the_streak_line_says_at_least_when_the_figure_is_a_floor():
    """A floor must not be presented to the user as the estimate of the whole.

    The streak line is user-facing money. Showing a measured-buckets-only lower
    bound as `~$X` is an UNKNOWN rendered as an exact figure — the same defect
    class this PR removes, one level down.
    """
    from agent.empty_response_guard import (
        record_empty_attempt,
        streak_cost_is_floor,
        streak_cost_usd,
    )

    agent = _cost_agent()
    for i in range(2):
        agent._empty_content_retries = i
        record_empty_attempt(
            agent,
            finish_reason="stop",
            response=SimpleNamespace(usage=_unsignaled_empty_wire(_REALISTIC_PROMPT)),
        )

    assert streak_cost_usd(agent) is not None
    assert streak_cost_is_floor(agent) is True

    # MEASURED control: a fully measured streak is an estimate, not a floor.
    measured = _cost_agent()
    for i in range(2):
        measured._empty_content_retries = i
        record_empty_attempt(
            measured,
            finish_reason="stop",
            response=SimpleNamespace(
                usage={
                    "prompt_tokens": _REALISTIC_PROMPT,
                    "completion_tokens": 0,
                    "total_tokens": _REALISTIC_PROMPT,
                }
            ),
        )
    assert streak_cost_usd(measured) is not None
    assert streak_cost_is_floor(measured) is False


def test_f3_the_f5_trade_itself_is_preserved():
    """The hunk F3 is about is NOT reverted — both guards now hold at once.

    F5 stays: a declared-unmeasured output still refuses to classify as a
    deterministic empty (that would be a measured-zero claim). F3 restores the
    OTHER protection beside it, so the payload is no longer unguarded.
    """
    from agent.empty_response_guard import (
        REDUCED_EMPTY_RETRY_BUDGET,
        deterministic_empty,
        empty_retry_budget,
        record_empty_attempt,
    )

    agent = _cost_agent()
    wire = _unsignaled_empty_wire(_REALISTIC_PROMPT)
    for i in range(2):
        agent._empty_content_retries = i
        record_empty_attempt(agent, finish_reason="stop", response=SimpleNamespace(usage=wire))

    assert deterministic_empty(agent) is False, "F5's trade stands"
    assert (
        empty_retry_budget(agent, SimpleNamespace(usage=wire))
        == REDUCED_EMPTY_RETRY_BUDGET
    ), "but the cost ceiling is no longer inert for the same payload"
