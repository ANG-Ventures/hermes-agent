"""Counter-divergence transparency in the compaction announce.

Regression cover for the 2026-08-08 report: the banner said `~503K` while the
previous turn's runtime footer said `733.5k/1M`. Both were correct — the banner
renders a LOCAL ESTIMATE over messages, the footer renders the PROVIDER's real
prompt_tokens — but with only one number visible the compaction reads as
unexplained.

Measured on session 20260806_084344_145f6c6d (COMPACTION_SKEW telemetry):

    rough=451863 real=619510   1.37x
    rough=462914 real=626197   1.35x
    rough=503180 real=693766   1.38x   <- the ~503K banner
    rough=530213 real=735755   1.39x   <- the 733.5k footer

The estimate read ~72% of real. Because `record_skew_from_real` clamps its ratio
to <= 1.0, every one of those was recorded as ratio=1.000.
"""

import logging

import pytest

from agent.compaction_stats import CompactionStats
from agent.fork_ext.compaction_ext import (
    _counter_divergence_line,
    _format_granular_announce,
)


def _Stats(pre_tokens=503_180, post_tokens=62_000):
    """Build a REAL CompactionStats, shaped like the incident's compaction.

    Uses the production dataclass rather than a hand-rolled stub: a stub that
    happens to omit a field the renderer reads would pass here and break in
    production (and a stub that drifts from the dataclass silently stops
    testing the real thing). ``freed_tokens``/``freed_pct`` are derived
    properties, so they are not passed.
    """
    return CompactionStats(
        pre_messages=782,
        post_messages=102,
        eligible_count=458,
        kept_messages=101,
        kept_pre_messages=101,
        summary_messages=1,
        anchor_messages=0,
        cleared_count=324,
        folded_count=357,
        pre_tokens=pre_tokens,
        post_tokens=post_tokens,
        kept_tokens=14_000,
        kept_pre_tokens=14_000,
        summary_tokens=48_000,
        anchor_tokens=0,
        cleared_tokens=143_000,
        folded_tokens=346_180,
    )


# ── _counter_divergence_line ────────────────────────────────────────────────


def test_divergence_line_reports_the_real_measured_incident():
    """The exact numbers Ace reported must produce a visible divergence line."""
    line = _counter_divergence_line(503_180, 693_766)
    assert line is not None, "a 1.38x counter gap must not be silent"
    assert "503K" in line
    assert "693K" in line
    assert "1.38x" in line
    assert "under" in line, "direction must be named; under-counting is the risky one"


def test_agreeing_counters_stay_silent():
    """Ordinary estimator noise must not add a line to every compaction."""
    assert _counter_divergence_line(500_000, 505_000) is None
    assert _counter_divergence_line(500_000, 495_000) is None
    assert _counter_divergence_line(500_000, 500_000) is None


def test_tolerance_boundary_is_respected():
    """Just inside tolerance is silent; just outside speaks."""
    assert _counter_divergence_line(100_000, 114_000) is None      # 1.14x
    assert _counter_divergence_line(100_000, 116_000) is not None  # 1.16x


def test_over_counting_is_reported_with_the_other_direction():
    """An over-counting estimate is benign but still worth naming correctly."""
    line = _counter_divergence_line(700_000, 400_000)
    assert line is not None
    assert "over" in line
    assert "under" not in line


@pytest.mark.parametrize(
    "est,real",
    [
        (0, 500_000),
        (500_000, 0),
        (None, 500_000),
        (500_000, None),
        (None, None),
        (-1, 500_000),
        (500_000, -1),
    ],
)
def test_missing_or_nonsense_inputs_never_render(est, real):
    """No provider reading yet (or a sentinel) must not fabricate a line."""
    assert _counter_divergence_line(est, real) is None


def test_non_numeric_input_does_not_raise():
    """Display code must never break a compaction."""
    assert _counter_divergence_line("banana", 500_000) is None
    assert _counter_divergence_line(500_000, object()) is None


# ── rendered into the actual announce ───────────────────────────────────────


def test_announce_includes_divergence_when_counters_disagree():
    out = _format_granular_announce(
        "🗜️ Context compacted",
        _Stats(),
        "claude-apr/claude-opus-5",
        False,
        None,
        None,
        real_prompt_tokens=693_766,
    )
    assert "Counters disagree" in out
    assert "693K" in out


def test_announce_omits_divergence_when_counters_agree():
    out = _format_granular_announce(
        "🗜️ Context compacted",
        _Stats(),
        "claude-apr/claude-opus-5",
        False,
        None,
        None,
        real_prompt_tokens=510_000,
    )
    assert "Counters disagree" not in out


def test_announce_unchanged_without_a_provider_reading():
    """Back-compat: callers that pass nothing get the previous output."""
    stats = _Stats()
    baseline = _format_granular_announce(
        "🗜️ Context compacted", stats, "m", False, None, None,
    )
    explicit_none = _format_granular_announce(
        "🗜️ Context compacted", stats, "m", False, None, None,
        real_prompt_tokens=None,
    )
    assert baseline == explicit_none
    assert "Counters disagree" not in baseline


def test_divergence_renders_on_the_no_reduction_branch_too():
    """Both Context-line branches must carry the divergence, not just one."""
    stats = _Stats(pre_tokens=503_180, post_tokens=503_180)  # freed == 0
    out = _format_granular_announce(
        "🗜️ Context compacted", stats, "m", False, None, None,
        real_prompt_tokens=693_766,
    )
    assert "no net token reduction" in out
    assert "Counters disagree" in out


# ── the clamp that hid this ─────────────────────────────────────────────────


class _Engine:
    """Bare carrier for record_skew_from_real (mixin-style method under test)."""

    _SKEW_HISTORY = 8

    def __init__(self, rough):
        self._last_rough_sent = rough
        self._recent_skews = []
        self.rough_at_last_real = 0

    def _persist_skew_history(self):
        pass

    def _emit_skew_telemetry(self, rough, real, ratio):
        pass


def _bind_record_skew(engine):
    from agent.context_engine import ContextEngine

    return ContextEngine.record_skew_from_real.__get__(engine, type(engine))


def test_undercount_is_logged_even_though_the_ratio_is_clamped(caplog):
    """Under-counting must be loud in the logs regardless of calibration policy.

    The recorded ratio is no longer clamped to 1.0 (see PR: the clamp WAS the
    bug), but the warning is the half that matters here: an under-counting
    estimate must never be silent.
    """
    engine = _Engine(rough=503_180)
    with caplog.at_level(logging.WARNING, logger="agent.context_engine"):
        _bind_record_skew(engine)(693_766)

    assert "COMPACTION_ESTIMATE_UNDERCOUNT" in caplog.text
    assert "1.38" in caplog.text
    # the measured under-count now survives into the calibration history
    assert engine._recent_skews and engine._recent_skews[0] > 1.0


def test_normal_overcount_does_not_warn(caplog):
    """The common case (rough over-counts) stays quiet."""
    engine = _Engine(rough=700_000)
    with caplog.at_level(logging.WARNING, logger="agent.context_engine"):
        _bind_record_skew(engine)(500_000)

    assert "COMPACTION_ESTIMATE_UNDERCOUNT" not in caplog.text
    assert engine._recent_skews and engine._recent_skews[0] < 1.0


def test_small_undercount_stays_below_the_warn_threshold(caplog):
    engine = _Engine(rough=100_000)
    with caplog.at_level(logging.WARNING, logger="agent.context_engine"):
        _bind_record_skew(engine)(110_000)  # 1.10x

    assert "COMPACTION_ESTIMATE_UNDERCOUNT" not in caplog.text
