"""Regression: an identified 7-day quota window must survive into the cooldown.

Follow-up to the 5h/7d labelling work. Naming the window in the failover
announce was only half the value — the other half is ACTING on it.

THE BUG (three separate clamps, all on the same path):

1. ``_primary_cooldown_seconds`` clamped every provider-supplied reset to 6h.
   Anthropic's own ``retry-after`` on a 7d-exhausted sub is ~2 days, so the
   cooldown was cut to 6h and the harness re-probed a provably-dead sub ~8 times
   before it could possibly recover — each probe a full turn's context marshaled,
   429'd, and thrown away.

2. The rate-limit backoff then re-clamped that base AGAIN via
   ``min(base * 2**count, 14400)`` — 4h, i.e. even shorter than the 6h it had
   just survived.

3. Every RECURSIVE fallback skip (unavailable rung, invalid entry, same-backend,
   chain-walk) called ``_try_activate_fallback(reason)`` WITHOUT ``error_context``.
   So if the first fallback rung was skipped for any reason, the second
   invocation re-armed the primary with the generic 60s default — collapsing a
   two-day window to a minute.

The fix is window-aware, not blanket-longer: an *identified* 7d window may cool
down up to 7 days; every other context keeps the historical 6h/4h bounds exactly.
"""

from __future__ import annotations

import time

import pytest

from agent.chat_completion_helpers import _primary_cooldown_seconds


SIX_HOURS = 6 * 60 * 60
SEVEN_DAYS = 7 * 24 * 60 * 60
TWO_DAYS = 2 * 24 * 60 * 60


class TestSevenDayWindowIsHonored:
    def test_two_day_7d_reset_is_not_clamped_to_six_hours(self):
        """The headline case: a real 7d exhaustion from the 2026-08-08 incident."""
        ctx = {"reset_at": time.time() + TWO_DAYS, "quota_window": "7d"}
        assert _primary_cooldown_seconds(ctx) > 47 * 3600

    def test_7d_window_is_still_bounded(self):
        """Honoring the window is not the same as trusting an absurd number."""
        ctx = {"reset_at": time.time() + 30 * 24 * 3600, "quota_window": "7d"}
        assert _primary_cooldown_seconds(ctx) <= SEVEN_DAYS

    def test_short_7d_reset_is_not_inflated(self):
        """A 7d window that resets SOON must cool down soon — we widen the
        ceiling, we do not impose a floor."""
        ctx = {"reset_at": time.time() + 900, "quota_window": "7d"}
        assert _primary_cooldown_seconds(ctx) == pytest.approx(900, abs=5)


class TestNoCollateralDamage:
    """Every non-7d context keeps its exact historical behaviour."""

    def test_5h_window_keeps_the_six_hour_ceiling(self):
        ctx = {"reset_at": time.time() + TWO_DAYS, "quota_window": "5h"}
        assert _primary_cooldown_seconds(ctx) == pytest.approx(SIX_HOURS, abs=5)

    def test_unidentified_window_keeps_the_six_hour_ceiling(self):
        """No quota_window at all — the conservative bound still applies."""
        ctx = {"reset_at": time.time() + TWO_DAYS}
        assert _primary_cooldown_seconds(ctx) == pytest.approx(SIX_HOURS, abs=5)

    def test_no_context_still_returns_the_sixty_second_default(self):
        assert _primary_cooldown_seconds(None) == 60.0
        assert _primary_cooldown_seconds({}) == 60.0

    def test_expired_reset_returns_the_default(self):
        ctx = {"reset_at": time.time() - 500, "quota_window": "7d"}
        assert _primary_cooldown_seconds(ctx) == 60.0

    def test_garbage_reset_returns_the_default(self):
        assert _primary_cooldown_seconds({"reset_at": "banana"}) == 60.0

    def test_small_retry_after_style_number_is_treated_as_a_duration(self):
        """Some providers send seconds-from-now rather than an epoch."""
        assert _primary_cooldown_seconds({"reset_at": 300}) == pytest.approx(300, abs=1)


class TestBackoffCeilingDoesNotUndoTheWindow:
    """The escalation ceiling must not clamp a longer PROVIDER-STATED window.

    The arithmetic tests below mirror the live expression; the source-contract
    test binds them to the REAL code so a future edit that reintroduces the bare
    ``min(..., 14400)`` clamp fails here instead of silently passing an
    arithmetic model that no longer matches production.
    """

    @staticmethod
    def _backoff(base, count):
        return min(base * (2 ** count), max(base, 14400))

    def test_live_clamp_uses_the_provider_base_as_a_floor(self):
        """Source contract: the ceiling must be max(base_cooldown, 14400).

        A bare `min(base * 2**count, 14400)` re-clamps a 2-day provider window
        back to 4h — the exact bug. Asserting on the source keeps the arithmetic
        model below honest.
        """
        import inspect

        from agent import chat_completion_helpers as mod

        src = inspect.getsource(mod.try_activate_fallback)
        assert "max(base_cooldown, 14400)" in src, (
            "the backoff ceiling no longer floors at the provider-stated base — "
            "a 7d window will be re-clamped to 4h"
        )
        assert "min(base_cooldown * (2 ** backoff_count), 14400)" not in src, (
            "the bare 4h clamp is back; it undoes the window-aware cooldown"
        )

    def test_seven_day_base_survives_the_four_hour_ceiling(self):
        base = TWO_DAYS
        assert self._backoff(base, 0) > 47 * 3600

    def test_seven_day_base_survives_repeated_escalation(self):
        base = TWO_DAYS
        for count in range(4):
            assert self._backoff(base, count) >= base

    def test_ordinary_base_still_escalates_and_caps_at_four_hours(self):
        base = 60.0
        assert self._backoff(base, 0) == 60
        assert self._backoff(base, 1) == 120
        assert self._backoff(base, 20) == 14400  # capped, not unbounded

    def test_ceiling_is_still_enforced_for_sub_ceiling_bases(self):
        """Regression guard on the fix itself: max(base, 14400) must not become
        an escape hatch that lets an ordinary base escalate past 4h."""
        assert self._backoff(3600.0, 10) == 14400


class TestErrorContextSurvivesRecursiveSkips:
    """A skipped fallback rung must not lose the window.

    Source-level contract test: every recursive ``_try_activate_fallback`` call
    inside ``try_activate_fallback`` must forward ``error_context``. If one is
    added later without it, a skipped first rung silently collapses a two-day
    cooldown to the 60s default — the exact bug, and one that unit-testing the
    happy path would never catch.
    """

    def test_every_recursive_call_forwards_error_context(self):
        import inspect

        from agent import chat_completion_helpers as mod

        src = inspect.getsource(mod.try_activate_fallback)
        bare = [
            line.strip()
            for line in src.splitlines()
            if "_try_activate_fallback(reason)" in line
        ]
        assert not bare, (
            "recursive fallback call(s) drop error_context — a skipped rung will "
            f"collapse the cooldown to the 60s default: {bare}"
        )

    def test_helper_accepts_error_context_keyword(self):
        import inspect

        from agent import chat_completion_helpers as mod

        params = inspect.signature(mod.try_activate_fallback).parameters
        assert "error_context" in params
