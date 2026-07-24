"""Regression: SyntheticHeavyAgent streamed-delta cadence must track REAL
elapsed wall-time (including the per-chunk CPU burn / slow-consumer stall),
not a timestamp sampled BEFORE the burn.

Bug (manifests at tui_gateway/synthetic_turn.py, the `if now - last_delta >=
interval` gate + `last_delta = now`): the loop samples ``now = time.monotonic()``
at the TOP of each iteration, then does the GIL-holding burn and an optional
per-chunk ``time.sleep`` (the slow-consumer / mixed-regime knob), and THEN
decides whether to emit a streamed delta using that STALE ``now``. Under a slow
consumer (per-chunk stall >= the delta interval) the emission gate and the
recorded ``last_delta`` both lag real time by one full consumer-delay, so the
first delta fires a whole stall late and the overall delta count is short —
the certify harness under-reports the streamed-frame pressure it exists to
measure.

Fix: re-sample the clock AFTER the burn/sleep (``tick = time.monotonic()``) and
gate/record on ``tick``, so cadence reflects true elapsed wall-time.

Deterministic via an injected fake clock (no real sleeping): ``sleep`` advances
the clock; ``monotonic`` reads it. We record the REAL clock value at each
streamed delta and assert the FIRST delta fires promptly once real elapsed time
crosses ``interval`` — which it cannot if the gate reads a pre-burn timestamp.
"""

from __future__ import annotations

import importlib

import pytest

synthetic_turn = importlib.import_module("tui_gateway.synthetic_turn")


class _FakeClock:
    """monotonic() returns the accumulated clock; sleep(s) advances it.

    Models a slow consumer: each per-chunk ``time.sleep(sleep_s)`` is a real
    wall-time stall, but the test runs instantly.
    """

    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def _run_turn(monkeypatch, *, duration, interval, sleep_s, chunk=1):
    """Drive a real SyntheticHeavyAgent turn under a fake clock; return the
    list of REAL clock times at which streamed deltas were emitted."""
    clock = _FakeClock()
    monkeypatch.setattr(synthetic_turn.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(synthetic_turn.time, "sleep", clock.sleep)

    agent = synthetic_turn.SyntheticHeavyAgent("sess-cadence")
    spec = {
        "duration_s": duration,
        "chunk": chunk,
        "delta_interval_s": interval,
        "tokens_per_delta": 1,
        "sleep_s": sleep_s,
    }
    emit_times: list[float] = []
    agent.run_conversation(
        __import__("json").dumps(spec),
        stream_callback=lambda _delta: emit_times.append(round(clock.t, 6)),
    )
    return emit_times


def test_first_delta_reflects_real_elapsed_under_slow_consumer(monkeypatch):
    # Slow consumer: each chunk stalls 0.20s, interval is 0.05s. The FIRST
    # burn+stall already advances real time past the interval, so a correct
    # cadence emits the first delta at real t ~= sleep_s (0.20). A stale-``now``
    # gate checks the pre-burn timestamp (0.0 < interval) on pass 1 and only
    # emits on pass 2 -> first delta at ~2*sleep_s (0.40).
    interval, sleep_s = 0.05, 0.20
    emit_times = _run_turn(monkeypatch, duration=1.0, interval=interval, sleep_s=sleep_s)

    assert emit_times, "expected at least one streamed delta"
    # Contract: the first delta must fire within one consumer-stall of the
    # real interval crossing -- i.e. it must NOT be delayed by an extra full
    # stall caused by gating on a pre-burn timestamp.
    assert emit_times[0] <= sleep_s + interval, (
        f"first delta emitted at real t={emit_times[0]}s, expected <= "
        f"{sleep_s + interval}s; a later value means the cadence gate read a "
        f"timestamp sampled BEFORE the per-chunk stall (stale-`now` bug)."
    )


def test_delta_count_matches_real_wall_time(monkeypatch):
    # Over a 1.0s turn with a 0.20s per-chunk consumer stall and 0.05s interval,
    # real time crosses the interval on every chunk, so a correct cadence emits
    # one delta per chunk that completes within the duration: at real t =
    # 0.2,0.4,0.6,0.8,1.0 -> 5 deltas. Stale-`now` gating drops the first ->
    # 4 deltas.
    emit_times = _run_turn(monkeypatch, duration=1.0, interval=0.05, sleep_s=0.20)
    assert len(emit_times) == 5, (
        f"expected 5 deltas tracking real wall-time, got {len(emit_times)} "
        f"at {emit_times}; a short count means the streamed-frame cadence "
        f"lags real elapsed time."
    )
