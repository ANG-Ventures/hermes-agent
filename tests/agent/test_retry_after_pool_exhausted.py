"""A 600s Retry-After on a seat the pool already benched must not be honored.

MEASURED 2026-09-16 00:38-00:55, sessions cron_5a79765af579_20260916_00{3804,
4332,5337} in ~/.hermes/logs/agent.log. The exact observed sequence:

    credential pool: marking CLAUDE_API_PROXY_F1_KEY exhausted (status=429), rotating
    credential pool: no available entries (all exhausted or empty)
    API call failed (attempt 1/3) ... provider=claude-apx-1 ... HTTP 429
    Honoring server Retry-After=600.0s (reason=rate_limit, attempt=2/3)
    Retrying API call in 600.0s (attempt 1/3) ... provider=claude-apx-1

The seat (a 7-day-capped sub) had ~31h until reset; the header said 600s. Those
are different clocks. Honoring the header sleeps the full rate-limit cap and
wakes to the same dead seat, and with a 540s cron budget every one of the three
sessions was killed mid-sleep: 3 sessions, 0 output.

The fix is scoped exactly as the class was stated: ``reason=rate_limit`` AND the
pool reporting no available entry. The guard is NOT the retry ceiling and NOT
the 600s cap — both of those are still in force, unchanged, and are asserted
below as negative controls.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from agent.retry_utils import (
    RETRY_AFTER_CAP_OVERLOAD_S,
    RETRY_AFTER_CAP_RATE_LIMIT_S,
    resolve_retry_after,
)


# --------------------------------------------------------------------------
# The policy helper — the pure half.
# --------------------------------------------------------------------------


def _resolve(**kw):
    """The measured incident's arguments, overridable per-case.

    Defaults reproduce the log line verbatim: a rate-limit 429 carrying
    Retry-After: 600 on attempt 1 of 3, on a pool with no available entry and
    no recovery inside the window.
    """
    base = dict(
        raw_value="600",
        is_rate_limit=True,
        is_overload=False,
        retry_count=1,
        max_retries=3,
        seat_exhausted=True,
        seat_recovery_seconds=None,
    )
    base.update(kw)
    return resolve_retry_after(**base)


class TestExhaustedSeatIsNotHonored:
    def test_measured_incident_is_not_honored(self):
        """RED on origin/main: main returns 600.0 here and sleeps."""
        assert _resolve() is None

    def test_a_healthy_seat_still_honors_the_same_header(self):
        """The discriminator is the SEAT, not the header. Same 600, pool has
        an entry available → unchanged behavior."""
        assert _resolve(seat_exhausted=False) == 600.0

    def test_recovery_beyond_the_window_is_not_honored(self):
        """~31h until the weekly cap resets, header says 600s."""
        assert _resolve(seat_recovery_seconds=31 * 3600.0) is None

    def test_recovery_inside_the_window_is_still_honored(self):
        """Carve-out: the seat genuinely does come back inside the wait we
        were about to serve anyway, so honoring still beats leaving."""
        assert _resolve(seat_recovery_seconds=120.0) == 600.0

    def test_recovery_exactly_at_the_window_is_honored(self):
        assert _resolve(seat_recovery_seconds=600.0) == 600.0

    def test_recovery_one_second_past_the_window_is_not(self):
        assert _resolve(seat_recovery_seconds=601.0) is None

    def test_recovery_compared_against_the_CAPPED_wait_not_the_raw_header(self):
        """The comparison must use the wait actually served. A header of 9999
        caps to 600, so a 900s recovery is OUTSIDE the real window even though
        it is inside the raw header value."""
        assert _resolve(raw_value="9999", seat_recovery_seconds=900.0) is None
        assert _resolve(raw_value="9999", seat_recovery_seconds=500.0) == (
            RETRY_AFTER_CAP_RATE_LIMIT_S
        )

    def test_already_due_seat_is_honored(self):
        """A non-positive recovery means the seat is due back now."""
        assert _resolve(seat_recovery_seconds=0.0) == 600.0

    @pytest.mark.parametrize("junk", ["", "abc", object(), float("nan"), True, False])
    def test_unusable_recovery_values_fail_toward_the_chain(self, junk):
        """On an exhausted seat, an unreadable recovery is NOT an excuse to
        sleep blind — fail toward the fallback chain."""
        assert _resolve(seat_recovery_seconds=junk) is None


class TestOverloadIsDeliberatelyOutOfScope:
    """Sibling case named in the class writeup: the same wait applies on
    overload (60s cap) and is fine there. Overload is about the SERVER's
    capacity, not the seat's quota — pinned so a future widening is a
    deliberate act, not a drive-by."""

    def test_overload_honored_even_on_an_exhausted_seat(self):
        assert _resolve(
            is_rate_limit=False, is_overload=True, raw_value="30"
        ) == 30.0

    def test_overload_cap_unchanged_on_an_exhausted_seat(self):
        assert _resolve(
            is_rate_limit=False, is_overload=True, raw_value="999"
        ) == RETRY_AFTER_CAP_OVERLOAD_S == 60.0


class TestGuardsThatMustNotHaveMoved:
    """The task body forbids 'fix' by changing api_max_retries or the 600s
    cap. These pin that neither moved."""

    def test_rate_limit_cap_is_still_600(self):
        assert RETRY_AFTER_CAP_RATE_LIMIT_S == 600.0

    def test_healthy_seat_still_capped_at_600(self):
        assert _resolve(raw_value="99999", seat_exhausted=False) == 600.0

    def test_final_pre_fallback_retry_carveout_intact(self):
        # retry_count == max_retries-1 is reserved for jitter (Greptile #223).
        assert _resolve(retry_count=2, max_retries=3, seat_exhausted=False) is None

    def test_default_arguments_preserve_legacy_behavior(self):
        """Callers that don't pass the new kwargs get exactly the old answer —
        the new parameters are additive, not a behavior change."""
        assert resolve_retry_after(
            raw_value="600", is_rate_limit=True, is_overload=False,
            retry_count=1, max_retries=3,
        ) == 600.0


# --------------------------------------------------------------------------
# The pool probe — the wiring half. A correct policy that never receives
# seat_exhausted=True is a dead guard, so prove the probe reports the pool
# state the incident actually had.
# --------------------------------------------------------------------------

def pool_seat_exhaustion_state(*args, **kwargs):
    """Import lazily so a module-level ImportError on a tree without the probe
    cannot swallow the policy assertions above into a collection error — those
    must be able to report their own RED."""
    from agent.agent_runtime_helpers import (
        pool_seat_exhaustion_state as _probe,
    )

    return _probe(*args, **kwargs)


class _Pool:
    def __init__(self, *, available, next_at=None, provider="claude-apx-1", raises=False):
        self.provider = provider
        self._available = available
        self._next_at = next_at
        self._raises = raises

    def has_available(self):
        if self._raises:
            raise RuntimeError("pool exploded")
        return self._available

    def next_available_at(self):
        return self._next_at


def _agent(pool, provider="claude-apx-1"):
    return SimpleNamespace(
        _credential_pool=pool,
        provider=provider,
        base_url="http://100.84.177.69:18801/anthropic",
    )


class TestPoolSeatExhaustionProbe:
    def test_reports_exhausted_when_pool_has_no_available_entry(self):
        """The measured state: one seat, 429'd, nothing to rotate to."""
        exhausted, recovery = pool_seat_exhaustion_state(_agent(_Pool(available=False)))
        assert exhausted is True
        assert recovery is None

    def test_reports_healthy_when_an_entry_is_available(self):
        exhausted, recovery = pool_seat_exhaustion_state(_agent(_Pool(available=True)))
        assert exhausted is False
        assert recovery is None

    def test_converts_next_available_at_to_relative_seconds(self):
        pool = _Pool(available=False, next_at=time.time() + 1800.0)
        exhausted, recovery = pool_seat_exhaustion_state(_agent(pool))
        assert exhausted is True
        assert recovery == pytest.approx(1800.0, abs=5.0)

    def test_past_recovery_clamps_to_zero_not_negative(self):
        pool = _Pool(available=False, next_at=time.time() - 60.0)
        _exhausted, recovery = pool_seat_exhaustion_state(_agent(pool))
        assert recovery == 0.0

    def test_no_pool_is_not_exhaustion(self):
        assert pool_seat_exhaustion_state(_agent(None)) == (False, None)

    def test_cross_provider_pool_is_not_read(self):
        """#33088 boundary: on a fallback provider the bound pool belongs to
        the PRIMARY. Reading it would decline a Retry-After based on another
        provider's quota."""
        pool = _Pool(available=False, provider="claude-apx-1")
        assert pool_seat_exhaustion_state(_agent(pool, provider="openai-codex")) == (
            False, None,
        )

    def test_a_raising_pool_fails_closed_to_legacy_behavior(self):
        """A broken probe must never silently disable the honor policy."""
        assert pool_seat_exhaustion_state(_agent(_Pool(available=False, raises=True))) == (
            False, None,
        )


# --------------------------------------------------------------------------
# Wiring lock. The policy above is only reachable if the loop actually passes
# the probe's output into it — that is the half that was missing, so pin it in
# the source rather than trusting that it stays.
# --------------------------------------------------------------------------


class TestCallSiteIsWired:
    def test_conversation_loop_passes_seat_state_into_resolve_retry_after(self):
        import ast
        import inspect

        import agent.conversation_loop as cl

        tree = ast.parse(inspect.getsource(cl))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "resolve_retry_after"
        ]
        assert calls, "resolve_retry_after is no longer called from the loop"
        for call in calls:
            kwargs = {kw.arg for kw in call.keywords}
            assert "seat_exhausted" in kwargs, (
                "a resolve_retry_after call site does not pass seat_exhausted — "
                "the exhausted-seat guard is dead on that path"
            )
            assert "seat_recovery_seconds" in kwargs, (
                "a resolve_retry_after call site does not pass "
                "seat_recovery_seconds"
            )

    def test_probe_is_called_in_the_loop(self):
        import inspect

        import agent.conversation_loop as cl

        assert "pool_seat_exhaustion_state(agent)" in inspect.getsource(cl)
