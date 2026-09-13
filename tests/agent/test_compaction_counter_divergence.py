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


def test_divergence_line_reports_both_readings_without_blaming_the_estimator():
    """The line must report both numbers and NAME the sampling offset.

    The old wording ("local estimate reads 1.38x under") was a false
    accusation: the estimator scores 1.03x against a real tokenizer over
    identical wire-shaped objects. The gap is a sampling offset — the provider
    reading covers more messages than the compacted set.
    """
    line = _counter_divergence_line(503_180, 693_766)
    assert line is not None, "a large reading gap must not be silent"
    assert "503K" in line
    assert "693K" in line
    # The accusation must be gone.
    assert "1.38x" not in line
    assert "under" not in line
    assert "Counters disagree" not in line
    # The real reason must be named.
    assert "different message sets" in line


def test_divergence_line_never_blames_the_estimator_in_either_direction():
    """Neither direction may render an estimator-skew claim."""
    for est, real in ((700_000, 400_000), (400_000, 700_000)):
        line = _counter_divergence_line(est, real)
        assert line is not None
        assert "x under" not in line
        assert "x over" not in line
        assert "local estimate reads" not in line


def test_agreeing_counters_stay_silent():
    """Ordinary estimator noise must not add a line to every compaction."""
    assert _counter_divergence_line(500_000, 505_000) is None
    assert _counter_divergence_line(500_000, 495_000) is None
    assert _counter_divergence_line(500_000, 500_000) is None


def test_tolerance_boundary_is_respected():
    """Just inside tolerance is silent; just outside speaks."""
    assert _counter_divergence_line(100_000, 114_000) is None      # 1.14x
    assert _counter_divergence_line(100_000, 116_000) is not None  # 1.16x


def test_over_counting_is_reported_without_a_direction_claim():
    """An over-reading provider figure is reported, but not as estimator skew."""
    line = _counter_divergence_line(700_000, 400_000)
    assert line is not None
    assert "over" not in line
    assert "under" not in line
    assert "different message sets" in line


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


def test_announce_includes_divergence_when_readings_differ():
    out = _format_granular_announce(
        "🗜️ Context compacted",
        _Stats(),
        "claude-apr/claude-opus-5",
        False,
        None,
        None,
        real_prompt_tokens=693_766,
    )
    assert "693K" in out
    assert "different message sets" in out
    assert "Counters disagree" not in out


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
    """Both Context-line branches must carry the note, not just one."""
    stats = _Stats(pre_tokens=503_180, post_tokens=503_180)  # freed == 0
    out = _format_granular_announce(
        "🗜️ Context compacted", stats, "m", False, None, None,
        real_prompt_tokens=693_766,
    )
    assert "no net token reduction" in out
    assert "different message sets" in out


# ── 🔴 FOOTER PARITY (2026-09-12) ───────────────────────────────────────────
#
# Ace reads the runtime footer after every message, so the compaction banner's
# headline must agree with it. The footer renders
# context_compressor.last_prompt_tokens (gateway/run.py:25414 <- 7413); manual
# /compress already passes that same basis as wire_before
# (slash_commands.py:5566 <- session_entry.last_prompt_tokens, persisted from
# the same agent_result value at run.py:25744). These pin that the AUTOMATIC
# announce now reaches the same measured-before renderer.


def test_measured_before_renderer_reachable_on_the_live_basis():
    """The auto announce (basis='live') must reach wire_mode when given wire numbers.

    Regression for the inert-fix shape: wire_mode used to be gated on
    basis=='stored', so passing wire_before/wire_after from the automatic path
    changed nothing and the banner kept rendering an estimate headline that
    disagreed with the footer.
    """
    out = _format_granular_announce(
        "🗜️ Context compacted", _Stats(), "m", False, None, None,
        basis="live",
        wire_before=554_000,
        wire_after=149_000,
    )
    # The MEASURED provider number leads, exactly as the footer shows it.
    assert "554,000" in out
    assert "before measured" in out
    # And the estimate-only headline must not be what we shipped.
    assert "~503K → " not in out


def test_measured_before_headline_matches_the_footer_value():
    """The headline's leading number must be the footer's number, verbatim."""
    footer_value = 427_526  # a real context_used reading from the Blackbox ledger
    out = _format_granular_announce(
        "🗜️ Context compacted", _Stats(), "m", False, None, None,
        basis="live",
        wire_before=footer_value,
        wire_after=133_476,
    )
    assert f"{footer_value:,}" in out


def test_no_estimator_accusation_when_measured_pair_present():
    """With a measured pair, the misleading comparison must not appear at all."""
    out = _format_granular_announce(
        "🗜️ Context compacted", _Stats(), "m", False, None, None,
        basis="live",
        wire_before=554_000,
        wire_after=149_000,
        real_prompt_tokens=554_000,
    )
    assert "Counters disagree" not in out
    assert "local estimate reads" not in out


def test_live_basis_without_wire_numbers_is_unchanged():
    """Back-compat: no wire kwargs on the live basis renders the old shape."""
    stats = _Stats()
    baseline = _format_granular_announce(
        "🗜️ Context compacted", stats, "m", False, None, None, basis="live",
    )
    assert "before measured" not in baseline
    assert "Context:" in baseline


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
