"""Physical-call attribution passed from the MoA loop to Blackbox."""

from agent.usage_pricing import USAGE_UNKNOWN_FIELDS, CanonicalUsage


def test_build_moa_pricing_calls_appends_real_aggregator_after_advisors():
    from agent.conversation_loop import _build_moa_pricing_calls

    advisor_calls = [{
        "model": "gpt-5.5",
        "provider": "openai-codex",
        "base_url": "https://codex.invalid",
        "input_tokens": 1000,
        "output_tokens": 100,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
    }]
    aggregator_usage = CanonicalUsage(
        input_tokens=200,
        output_tokens=50,
        cache_read_tokens=300,
        cache_write_tokens=40,
        reasoning_tokens=10,
    )

    calls = _build_moa_pricing_calls(
        advisor_calls,
        aggregator_usage,
        aggregator_model="anthropic/claude-opus-4.8",
        aggregator_provider="openrouter",
        aggregator_base_url="https://openrouter.ai/api/v1",
    )

    assert calls[0] == advisor_calls[0]
    expected_measured = {
        "model": "anthropic/claude-opus-4.8",
        "provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "input_tokens": 200,
        "output_tokens": 50,
        "cache_read_tokens": 300,
        "cache_write_tokens": 40,
        "reasoning_tokens": 10,
    }
    # Attribution + counters are exactly as before. The serialized call also
    # carries the UNKNOWN discriminators, which must be present and False for
    # this fully-measured usage -- Blackbox prefers these nested calls, so
    # dropping the flags here is what let an unknown input get priced as 0.
    assert {k: calls[1][k] for k in expected_measured} == expected_measured
    assert set(calls[1]) == set(expected_measured) | set(USAGE_UNKNOWN_FIELDS)
    assert all(calls[1][flag] is False for flag in USAGE_UNKNOWN_FIELDS)
    # The helper must not mutate the facade-owned pending list.
    assert len(advisor_calls) == 1


def test_build_moa_pricing_calls_preserves_unknown_discriminators():
    """Unknown flags must survive the aggregator -> physical-call seam."""
    from agent.conversation_loop import _build_moa_pricing_calls

    usage = CanonicalUsage(
        input_tokens=200,
        output_tokens=0,
        output_tokens_unknown=True,
        cache_read_tokens_unknown=True,
    )
    call = _build_moa_pricing_calls(
        [],
        usage,
        aggregator_model="claude-sonnet-4-5",
        aggregator_provider="anthropic",
        aggregator_base_url=None,
    )[0]
    assert call["output_tokens_unknown"] is True
    assert call["cache_read_tokens_unknown"] is True
    assert call["input_tokens_unknown"] is False
    assert call["usage_unknown"] is False
