"""Persisted skew calibration must survive a restart when scale-up is enabled.

PR #506 lifted the ``min(1.0, ...)`` clamp in TWO places — ``record_skew_from_real``
(which records the ratio) and ``_current_skew`` (which applies it) — so an
UNDER-counting estimate can finally be corrected upward. It missed a THIRD site:
``seed_skew_calibration``, the restart-resume path, still enforced the pre-#506
invariant ``0 < r <= 1.0``.

Net effect before this fix: every scale-up ratio the session learned was written to
the session row by ``_persist_skew_history`` and then SILENTLY DISCARDED on the next
restart. The calibration could never accumulate across restarts, which is precisely
when it matters — a fresh session has no readings at all.

Measured on the deployed tree: persisted ``[1.38, 1.22, 1.45]`` seeded as ``[]``.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import agent.context_engine as ce_mod  # noqa: E402
from agent.context_engine import (  # noqa: E402
    _SKEW_SCALE_UP_MAX,
    ContextEngine,
)


class _Engine(ContextEngine):
    """Minimal concrete engine — exercises the base-class calibration methods."""

    @property
    def name(self) -> str:
        return "test-engine"

    def update_from_response(self, *a, **k):  # pragma: no cover - not used
        pass

    def should_compress(self, *a, **k):  # pragma: no cover - not used
        return False

    def compress(self, *a, **k):  # pragma: no cover - not used
        return []


def _engine():
    e = object.__new__(_Engine)
    e._recent_skews = []
    return e


@pytest.fixture(autouse=True)
def _scale_up_on(monkeypatch):
    """Scale-up is the default; pin it so the band under test is the wide one.

    The knob lives in config.yaml (``compression.skew_scale_up``), not an env
    var — AGENTS.md: behavioral settings go in config.yaml, .env is secrets
    only. So pin the reader, not the environment.
    """
    monkeypatch.setattr(ce_mod, "_scale_up_calibration_enabled", lambda: True)
    yield


# --------------------------------------------------------------- the regression


def test_undercount_ratios_survive_a_restart():
    """The bug: scale-up ratios were dropped on seed, so calibration reset."""
    e = _engine()
    e.seed_skew_calibration([1.38, 1.22, 1.45])
    assert e._recent_skews == [1.38, 1.22, 1.45], (
        "persisted under-count ratios must seed; dropping them silently resets "
        "calibration on every restart"
    )


def test_seeded_undercount_actually_reaches_the_applied_skew():
    """Seeding is pointless if _current_skew won't apply the value."""
    e = _engine()
    e.seed_skew_calibration([1.38, 1.38, 1.38])
    assert e._current_skew() > 1.0, (
        "a seeded under-count must produce a scale-UP skew, otherwise the "
        "restart-resume path is decorative"
    )


def test_mixed_history_seeds_whole():
    e = _engine()
    e.seed_skew_calibration([0.92, 1.31, 1.0])
    assert e._recent_skews == [0.92, 1.31, 1.0]


# --------------------------------------------------- the band must stay coupled


def test_seed_band_matches_what_current_skew_will_apply():
    """CLASS GUARD: the accept-band and the apply-band must not drift apart.

    This is the invariant that was violated. Any ratio seed accepts must be one
    _current_skew is willing to apply, and vice versa — otherwise a future edit
    to one clamp silently re-opens the same hole.
    """
    probe = _SKEW_SCALE_UP_MAX
    e = _engine()
    e.seed_skew_calibration([probe] * 3)
    assert e._recent_skews, (
        f"_SKEW_SCALE_UP_MAX ({probe}) is the ceiling _current_skew clamps to, "
        "so seed must accept it"
    )
    assert e._current_skew() == pytest.approx(probe)


def test_absurd_values_are_still_rejected():
    """Widening the band must not mean accepting anything."""
    e = _engine()
    e.seed_skew_calibration([_SKEW_SCALE_UP_MAX * 10])
    assert e._recent_skews == [], "a corrupt persisted value must not seed"


@pytest.mark.parametrize("bad", [0.0, -1.0, "abc", None, float("nan")])
def test_invalid_input_ignored(bad):
    e = _engine()
    e.seed_skew_calibration([bad])
    assert e._recent_skews == []


# ------------------------------------------------------ pre-existing invariants


def test_live_history_is_never_clobbered():
    """A live in-memory history is fresher than any snapshot."""
    e = _engine()
    e._recent_skews = [0.97]
    e.seed_skew_calibration([1.4, 1.4])
    assert e._recent_skews == [0.97]


def test_overcount_ratios_still_seed():
    """Negative control — the original behavior must not regress."""
    e = _engine()
    e.seed_skew_calibration([0.85, 0.90])
    assert e._recent_skews == [0.85, 0.90]


def test_history_is_capped():
    e = _engine()
    e.seed_skew_calibration([1.1] * (ContextEngine._SKEW_HISTORY + 25))
    assert len(e._recent_skews) == ContextEngine._SKEW_HISTORY


def test_scale_up_disabled_restores_the_narrow_band(monkeypatch):
    """With scale-up off, the old 1.0 ceiling must come back."""
    monkeypatch.setattr(ce_mod, "_scale_up_calibration_enabled", lambda: False)
    e = _engine()
    e.seed_skew_calibration([1.38])
    assert e._recent_skews == [], (
        "scale-up disabled means the estimate is only corrected DOWN; an "
        "under-count ratio must not seed"
    )
    e2 = _engine()
    e2.seed_skew_calibration([0.88])
    assert e2._recent_skews == [0.88]


def test_round_trip_persist_then_seed():
    """The two halves must agree: what persist writes, seed must accept."""
    src = _engine()
    src._recent_skews = [1.33, 1.41]
    persisted = list(src._recent_skews)

    dst = _engine()
    dst.seed_skew_calibration(persisted)
    assert dst._recent_skews == persisted, (
        "persist and seed are two halves of one contract; if seed rejects what "
        "persist wrote, the write was pointless"
    )
