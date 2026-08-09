"""Compaction pre-emption: the maintenance floor + un-clamped skew calibration.

Two defects, both measured on live sessions 2026-08-08.

A. LCM's divergent-replay preflight path ran the leaf-compaction eligibility
   check with NO token gate, while the NON-divergent path gated the identical
   check behind ``rough >= threshold_tokens``. The divergent path is entered
   whenever ingest externalized something (media/base64/large tool result), so a
   screenshot-heavy session compacted at any size:

       01:25:37  ~239,896 tokens = 32% of the 750,000 threshold
       01:31:40  ~158,262 tokens = 21%
       01:33:59  ~175,730 tokens = 23%

   Three compactions in eight minutes, freeing 27-47% where pressure-driven
   compactions free 84-86%, each costing a summarizer call + the prompt cache.

B. ``record_skew_from_real`` hard-clamped its ratio to <= 1.0, so a measured
   UNDER-count (1.15-1.39x live) recorded as a clean ratio=1.000 and the
   threshold gate compared against a number well below the real prompt.
"""

import pytest

from agent.context_engine import (
    _SKEW_SCALE_UP_MAX,
    _UNDERCOUNT_WARN_RATIO,
    ContextEngine,
)


# ── A. maintenance pressure floor ───────────────────────────────────────────


class _Cfg:
    def __init__(self, ratio=0.0):
        self.maintenance_min_pressure_ratio = ratio


class _Mixin:
    """Bare carrier for the pressure-floor helper under test."""

    def __init__(self, ratio=0.0, threshold=750_000):
        self._config = _Cfg(ratio)
        self.threshold_tokens = threshold
        # The warm-cache gate is a SECOND term inside _maintenance_pressure_met.
        # These tests exercise the SIZE floor in isolation, so pin the cache term
        # to its fail-open state (no metrics reported => no evidence to defer on).
        # A dedicated suite covers the cache term:
        # tests/context_engine/test_warm_cache_maintenance_gate.py
        self.cache_metrics_available = False
        self.cache_read_ratio = 0.0

    def _maintenance_cache_cost_acceptable(self, observed_tokens):
        from plugins.context_engine.lcm.compaction import CompactionMixin

        return CompactionMixin._maintenance_cache_cost_acceptable(
            self, observed_tokens
        )


def _pressure_met(engine, tokens):
    from plugins.context_engine.lcm.compaction import CompactionMixin

    return CompactionMixin._maintenance_pressure_met(engine, tokens)


def test_floor_disabled_by_default_preserves_old_behavior():
    """Default 0.0 must allow maintenance at ANY size (upstream behavior)."""
    e = _Mixin(ratio=0.0)
    assert _pressure_met(e, 1) is True
    assert _pressure_met(e, 239_896) is True


@pytest.mark.parametrize("tokens", [239_896, 158_262, 175_730])
def test_the_three_measured_premature_fires_are_blocked(tokens):
    """The exact production fires Ace reported must not pass a 50% floor."""
    e = _Mixin(ratio=0.5)  # floor = 375,000
    assert _pressure_met(e, tokens) is False


def test_real_pressure_still_compacts():
    """Sessions at genuine pressure must be unaffected by the floor."""
    e = _Mixin(ratio=0.5)
    assert _pressure_met(e, 422_558) is True   # 56% — a real prior compaction
    assert _pressure_met(e, 443_783) is True   # 59% — ditto
    assert _pressure_met(e, 752_603) is True   # over threshold


def test_floor_boundary_is_inclusive():
    e = _Mixin(ratio=0.5)  # floor = 375,000
    assert _pressure_met(e, 374_999) is False
    assert _pressure_met(e, 375_000) is True


def test_no_threshold_configured_never_blocks():
    """threshold_tokens=0 means 'unknown'; the floor must not gate on it."""
    e = _Mixin(ratio=0.5, threshold=0)
    assert _pressure_met(e, 1) is True


def test_missing_config_attribute_defaults_to_allow():
    """A config object without the knob (older/plugin config) must not break."""
    class _Bare:
        pass

    e = _Mixin(ratio=0.0)
    e._config = _Bare()
    assert _pressure_met(e, 1) is True


def test_the_floor_is_actually_wired_into_the_preflight_path():
    """The helper must GATE the real call sites, not merely exist.

    A correct helper that nothing calls is the classic inert fix: every
    behavioral test above still passes while production compacts exactly as
    before. This asserts the wiring itself, at both opportunistic arms on the
    divergent-replay path.
    """
    import inspect

    from plugins.context_engine.lcm.compaction import CompactionMixin

    src = inspect.getsource(CompactionMixin.should_compress_preflight)
    assert src.count("_maintenance_pressure_met") >= 2, (
        "both maintenance arms (compactable backlog + ignored-message backlog) "
        "must be gated; found fewer call sites, so the floor is partly inert"
    )


def test_deterministic_cleanup_is_not_gated_by_the_floor():
    """Ingest-cleanup adoption must stay available at ANY size.

    It is deterministic, already durable, and costs no summarizer spend — the
    floor exists to stop expensive opportunistic summarization, not to strand
    a cleanup the store has already committed.
    """
    import inspect

    from plugins.context_engine.lcm.compaction import CompactionMixin

    src = inspect.getsource(CompactionMixin.should_compress_preflight)
    cleanup_idx = src.index("if cleanup_requested:")
    first_gate = src.index("_maintenance_pressure_met")
    assert cleanup_idx < first_gate, (
        "the cleanup return must come BEFORE the pressure floor"
    )


# ── B. skew calibration may now scale UP ────────────────────────────────────


class _Engine:
    _SKEW_HISTORY = 8
    _SKEW_FLOOR_DEFAULT = 0.55

    def __init__(self, rough):
        self._last_rough_sent = rough
        self._recent_skews = []
        self.rough_at_last_real = 0
        self._skew_floor = 0.55

    def _persist_skew_history(self):
        pass

    def _emit_skew_telemetry(self, rough, real, ratio):
        pass


def _record(engine, real):
    return ContextEngine.record_skew_from_real.__get__(engine, type(engine))(real)


def _skew(engine):
    return ContextEngine._current_skew.__get__(engine, type(engine))()


def test_undercount_is_now_recorded_not_clamped_away():
    """The measured 1.38x must survive into the calibration history."""
    e = _Engine(rough=503_180)
    _record(e, 693_766)
    assert e._recent_skews, "an under-count must record a sample"
    assert e._recent_skews[0] > 1.0, "1.38x must NOT be clamped to 1.0"
    assert e._recent_skews[0] == pytest.approx(1.379, abs=0.01)


def test_undercount_actually_reaches_the_trigger():
    """_current_skew must not silently re-clamp the correction to 1.0.

    This is the half that makes the fix real rather than inert: recording an
    un-clamped ratio is useless if the consumer clamps it back down.
    """
    e = _Engine(rough=503_180)
    _record(e, 693_766)
    assert _skew(e) > 1.0


def test_scale_up_is_bounded():
    """One anomalous pair must not drive a wildly premature compaction."""
    e = _Engine(rough=100_000)
    _record(e, 1_000_000)  # absurd 10x
    assert _skew(e) <= _SKEW_SCALE_UP_MAX


def test_overcount_still_scales_down_as_before():
    """The original direction must be untouched."""
    e = _Engine(rough=700_000)
    _record(e, 400_000)
    assert e._recent_skews[0] < 1.0
    assert _skew(e) < 1.0


def test_skew_floor_still_applies():
    """The lower clamp must survive the ceiling change."""
    e = _Engine(rough=1_000_000)
    _record(e, 10_000)  # 0.01x
    assert _skew(e) >= 0.55


def test_empty_history_is_identity():
    e = _Engine(rough=100)
    assert _skew(e) == 1.0


def test_warn_threshold_is_below_the_measured_range():
    """The warn ratio must actually catch the measured 1.15-1.39x band."""
    assert _UNDERCOUNT_WARN_RATIO <= 1.15
    assert _SKEW_SCALE_UP_MAX >= 1.39, "ceiling must not clip the measured range"
