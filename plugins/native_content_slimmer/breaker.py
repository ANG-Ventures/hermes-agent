"""Per-lane expansion-rate circuit breaker for PRD-5 native compression.

The breaker consumes realized canary results: one boolean per compressed-view
result saying whether the model expanded to the stored original. It is deliberately
small and deterministic so callers can table-test the Invariant 10 boundaries.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Hashable, Iterable
from dataclasses import dataclass
from typing import Deque

WINDOW_SIZE = 50
MIN_SAMPLES = 20
TRIP_THRESHOLD = 0.25

ACTION_COMPRESS = "compress"
ACTION_OFFLOAD = "offload"


@dataclass(frozen=True)
class BreakerState:
    lane: Hashable
    sample_count: int
    expansion_count: int
    expansion_rate: float
    tripped: bool
    action: str
    reason: str

    @property
    def allow_compression(self) -> bool:
        return self.action == ACTION_COMPRESS


class ExpansionRateCircuitBreaker:
    """Sliding-window, latched per-lane expansion breaker.

    - window: last 50 canary samples by default
    - cold start: offload until at least 20 samples are present
    - trip: expansion rate strictly greater than 0.25
    - latch: once tripped, a lane stays offload until ``rearm`` is called
    """

    def __init__(
        self,
        *,
        window_size: int = WINDOW_SIZE,
        min_samples: int = MIN_SAMPLES,
        trip_threshold: float = TRIP_THRESHOLD,
    ) -> None:
        if window_size <= 0:
            raise ValueError("window_size must be positive")
        if min_samples <= 0:
            raise ValueError("min_samples must be positive")
        if min_samples > window_size:
            raise ValueError("min_samples cannot exceed window_size")
        if trip_threshold < 0:
            raise ValueError("trip_threshold must be non-negative")
        self.window_size = int(window_size)
        self.min_samples = int(min_samples)
        self.trip_threshold = float(trip_threshold)
        self._samples: dict[Hashable, Deque[bool]] = defaultdict(
            lambda: deque(maxlen=self.window_size)
        )
        self._latched: set[Hashable] = set()

    def record_result(self, lane: Hashable, *, expanded: bool) -> BreakerState:
        """Record one realized canary result and return the lane's decision."""

        self._samples[lane].append(bool(expanded))
        state = self.evaluate(lane)
        if state.reason == "tripped":
            self._latched.add(lane)
        return state

    # Short alias for callers/tests that name the event as a sample.
    record = record_result

    def evaluate(self, lane: Hashable) -> BreakerState:
        """Evaluate a lane without adding a sample."""

        samples = list(self._samples.get(lane, ()))
        if lane in self._latched:
            return _state_from_samples(
                lane,
                samples,
                tripped=True,
                action=ACTION_OFFLOAD,
                reason="latched",
            )
        return evaluate_expansion_window(
            samples,
            lane=lane,
            window_size=self.window_size,
            min_samples=self.min_samples,
            trip_threshold=self.trip_threshold,
        )

    def should_compress(self, lane: Hashable) -> bool:
        return self.evaluate(lane).allow_compression

    def rearm(self, lane: Hashable, *, clear_samples: bool = True) -> BreakerState:
        """Manual re-arm for a latched lane.

        By default the old window is cleared so the lane re-enters the safe
        cold-start/offload state instead of immediately re-tripping on stale data.
        """

        self._latched.discard(lane)
        if clear_samples:
            self._samples.pop(lane, None)
        return self.evaluate(lane)

    def reset_lane(self, lane: Hashable) -> BreakerState:
        self._latched.discard(lane)
        self._samples.pop(lane, None)
        return self.evaluate(lane)


def evaluate_expansion_window(
    samples: Iterable[bool],
    *,
    lane: Hashable = "lane",
    window_size: int = WINDOW_SIZE,
    min_samples: int = MIN_SAMPLES,
    trip_threshold: float = TRIP_THRESHOLD,
) -> BreakerState:
    """Pure table-test helper for Invariant 10's boundary cases."""

    window = list(samples)[-int(window_size):]
    count = len(window)
    expansions = sum(1 for sample in window if sample)
    rate = (expansions / count) if count else 0.0
    if count < min_samples:
        return _state_from_counts(lane, count, expansions, rate, False, ACTION_OFFLOAD, "cold_start")
    if rate > trip_threshold:
        return _state_from_counts(lane, count, expansions, rate, True, ACTION_OFFLOAD, "tripped")
    return _state_from_counts(lane, count, expansions, rate, False, ACTION_COMPRESS, "healthy")


# Compatibility aliases for the likely names callers reach for.
LaneExpansionBreaker = ExpansionRateCircuitBreaker
ExpansionCircuitBreaker = ExpansionRateCircuitBreaker
CircuitBreaker = ExpansionRateCircuitBreaker


def _state_from_samples(
    lane: Hashable,
    samples: list[bool],
    *,
    tripped: bool,
    action: str,
    reason: str,
) -> BreakerState:
    count = len(samples)
    expansions = sum(1 for sample in samples if sample)
    rate = (expansions / count) if count else 0.0
    return _state_from_counts(lane, count, expansions, rate, tripped, action, reason)


def _state_from_counts(
    lane: Hashable,
    count: int,
    expansions: int,
    rate: float,
    tripped: bool,
    action: str,
    reason: str,
) -> BreakerState:
    return BreakerState(
        lane=lane,
        sample_count=int(count),
        expansion_count=int(expansions),
        expansion_rate=round(float(rate), 6),
        tripped=bool(tripped),
        action=action,
        reason=reason,
    )
