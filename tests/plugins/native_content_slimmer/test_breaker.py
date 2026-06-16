from __future__ import annotations

from plugins.native_content_slimmer.breaker import (
    ACTION_COMPRESS,
    ACTION_OFFLOAD,
    ExpansionRateCircuitBreaker,
)


def _feed(breaker: ExpansionRateCircuitBreaker, lane: str, samples: list[bool]):
    state = None
    for expanded in samples:
        state = breaker.record_result(lane, expanded=expanded)
    assert state is not None
    return state


def test_breaker_table_prd5_invariant_10_boundaries() -> None:
    one = ExpansionRateCircuitBreaker().record_result("n1", expanded=True)
    assert one.sample_count == 1
    assert one.action == ACTION_OFFLOAD
    assert one.tripped is False
    assert one.reason == "cold_start"

    nineteen = _feed(ExpansionRateCircuitBreaker(), "n19", [True] * 19)
    assert nineteen.sample_count == 19
    assert nineteen.action == ACTION_OFFLOAD
    assert nineteen.tripped is False
    assert nineteen.reason == "cold_start"

    n20_high = _feed(ExpansionRateCircuitBreaker(), "n20-high", [True] * 6 + [False] * 14)
    assert n20_high.sample_count == 20
    assert n20_high.expansion_rate == 0.30
    assert n20_high.action == ACTION_OFFLOAD
    assert n20_high.tripped is True
    assert n20_high.reason == "tripped"

    n20_low = _feed(ExpansionRateCircuitBreaker(), "n20-low", [True] * 4 + [False] * 16)
    assert n20_low.sample_count == 20
    assert n20_low.expansion_rate == 0.20
    assert n20_low.action == ACTION_COMPRESS
    assert n20_low.tripped is False
    assert n20_low.reason == "healthy"


def test_breaker_latches_until_manual_rearm() -> None:
    breaker = ExpansionRateCircuitBreaker()
    tripped = _feed(breaker, "lane", [True] * 6 + [False] * 14)
    assert tripped.tripped is True

    recovered_window = _feed(breaker, "lane", [False] * 50)
    assert recovered_window.expansion_rate == 0.0
    assert recovered_window.tripped is True
    assert recovered_window.action == ACTION_OFFLOAD
    assert recovered_window.reason == "latched"

    rearmed = breaker.rearm("lane")
    assert rearmed.tripped is False
    assert rearmed.action == ACTION_OFFLOAD
    assert rearmed.reason == "cold_start"
