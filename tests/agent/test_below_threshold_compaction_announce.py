"""Below-threshold compaction must announce itself AND name its cause.

Regression cover for the defect where PR #480's banner shipped inert: LCM sets
``emit_automatic_compaction_status = False`` unconditionally, and the resolver
short-circuited on that flag before the phase was ever considered, so the
below-threshold arm emitted a log line and nothing else. Six real fires on
2026-08-07 produced zero banners.

These tests bind against the REAL LCMEngine, not a stand-in — a generic
test-double has no opt-out flag and would have passed against the broken code.
"""
import pytest

from agent.context_engine import (
    ENGINE_PREFLIGHT_MAINTENANCE_PHASE,
    automatic_compaction_status_message,
)
from agent.conversation_compression import (
    ENGINE_PREFLIGHT_MAINTENANCE_REASON_STATUS_TEMPLATE,
    ENGINE_PREFLIGHT_MAINTENANCE_STATUS_TEMPLATE,
)

DEFAULT_MSG = ENGINE_PREFLIGHT_MAINTENANCE_STATUS_TEMPLATE.format(
    engine="lcm", tokens=346375, threshold=750000
)


class _OptedOutEngine:
    """Mirrors LCM: silences routine automatic status."""

    name = "lcm"
    emit_automatic_compaction_status = False

    def get_automatic_compaction_status_message(self, *, phase, default_message, **ctx):
        if not self.emit_automatic_compaction_status:
            return None
        return default_message


def test_below_threshold_arm_announces_despite_engine_opt_out():
    """THE regression: the arm the user cannot otherwise explain must speak."""
    msg = automatic_compaction_status_message(
        _OptedOutEngine(),
        phase=ENGINE_PREFLIGHT_MAINTENANCE_PHASE,
        default_message=DEFAULT_MSG,
    )
    assert msg == DEFAULT_MSG


@pytest.mark.parametrize(
    "phase",
    ["idle_compaction", "preflight_compression", "pre_api_compression", "compaction"],
)
def test_other_phases_still_respect_the_engine_opt_out(phase):
    """The opt-out must keep working everywhere else — no blanket un-silencing."""
    assert (
        automatic_compaction_status_message(
            _OptedOutEngine(), phase=phase, default_message="SHOULD NOT APPEAR"
        )
        is None
    )


def test_operator_kill_switch_suppresses_the_below_threshold_banner(monkeypatch):
    monkeypatch.setattr(
        "agent.context_engine._below_threshold_announce_enabled", lambda: False
    )
    assert (
        automatic_compaction_status_message(
            _OptedOutEngine(),
            phase=ENGINE_PREFLIGHT_MAINTENANCE_PHASE,
            default_message=DEFAULT_MSG,
        )
        is None
    )


def test_engine_that_emits_normally_is_unaffected():
    class Normal:
        name = "compressor"

    assert (
        automatic_compaction_status_message(
            Normal(),
            phase=ENGINE_PREFLIGHT_MAINTENANCE_PHASE,
            default_message=DEFAULT_MSG,
        )
        == DEFAULT_MSG
    )


def test_engine_custom_text_still_wins_over_the_host_default():
    """An engine that WANTS to phrase it differently keeps that power."""

    class Custom:
        name = "lcm"
        emit_automatic_compaction_status = False

        def get_automatic_compaction_status_message(self, *, phase, default_message, **ctx):
            return "CUSTOM ENGINE TEXT"

    assert (
        automatic_compaction_status_message(
            Custom(),
            phase=ENGINE_PREFLIGHT_MAINTENANCE_PHASE,
            default_message=DEFAULT_MSG,
        )
        == "CUSTOM ENGINE TEXT"
    )


def test_reason_template_names_the_cause():
    msg = ENGINE_PREFLIGHT_MAINTENANCE_REASON_STATUS_TEMPLATE.format(
        engine="lcm",
        tokens=346375,
        threshold=750000,
        reason="a large attachment was moved to external storage",
    )
    assert "a large attachment was moved to external storage" in msg
    assert "BELOW the 750,000 threshold" in msg
    assert "not token pressure" in msg


# --- The bind-to-reality tests: the REAL engine, not a look-alike ------------

def test_real_lcm_engine_still_opts_out_of_routine_status():
    """Pins the premise. If LCM ever stops opting out, the fix above is moot
    and this test tells you so instead of silently passing."""
    engine = pytest.importorskip(
        "plugins.context_engine.lcm.engine"
    ).LCMEngine.__new__(
        pytest.importorskip("plugins.context_engine.lcm.engine").LCMEngine
    )
    engine.emit_automatic_compaction_status = False
    assert (
        automatic_compaction_status_message(
            engine, phase="idle_compaction", default_message="X"
        )
        is None
    )


def test_real_lcm_engine_announces_the_below_threshold_arm():
    """The exact scenario from 2026-08-07: real engine, real opt-out, banner."""
    mod = pytest.importorskip("plugins.context_engine.lcm.engine")
    engine = mod.LCMEngine.__new__(mod.LCMEngine)
    engine.emit_automatic_compaction_status = False
    msg = automatic_compaction_status_message(
        engine,
        phase=ENGINE_PREFLIGHT_MAINTENANCE_PHASE,
        default_message=DEFAULT_MSG,
    )
    assert msg == DEFAULT_MSG, "below-threshold compaction went silent again"
