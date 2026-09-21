"""Cron's drain allowance must come off the SAME deadline as the chat drain.

The adaptive teardown reserve shrinks the chat drain by
``max(LAUNCHD_STOP_CLEANUP_RESERVE_S, last measured teardown)``. Cron's
allowance was never given ``last_teardown_s``: ``resolve_cron_drain_budget``
subtracted only the fixed ``CRON_DRAIN_CLEANUP_RESERVE_S`` (10s), so on a host
that had MEASURED a long teardown the chat drain shrank while cron's deadline
stayed pinned at ``clamp - 10``:

    last_teardown | chat_drain | cron_due | left before SIGKILL | needed
    None          | 45.0       | 50.0     | 10.0                | 15.0
    22.0          | 38.0       | 50.0     | 10.0                | 22.0
    58.0          |  2.0       | 50.0     | 10.0                | 58.0

and ``_drain_active_agents._still_draining()`` returns True on the cron branch
independently of the chat ``deadline``, so in-flight cron work genuinely held
the drain open to ``cron_deadline`` after the shortened chat drain expired —
leaving less time before launchd's SIGKILL than the teardown this machine had
demonstrated it needs.

Both allowances are now derived from one absolute deadline
(``resolve_launchd_drain_deadline_s``). These tests assert the COMPOSED budget
that ``gateway/run.py``'s stop path actually builds, because green isolated
helpers do not prove the composition: the pre-existing leash test passes an
unarmed ``watchdog_delay`` and never sees the reserve at all.

No minimum-drain floor is asserted anywhere here. A deadline fully consumed by
a measured teardown is a real zero-second answer, not a value to be raised.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.restart import (
    CRON_DRAIN_CLEANUP_RESERVE_S,
    LAUNCHD_STOP_CLEANUP_RESERVE_S,
    effective_stop_drain_timeout,
    resolve_cron_drain_budget,
    resolve_launchd_drain_deadline_s,
    resolve_launchd_shutdown_watchdog_delay,
)
from gateway.shutdown_watchdog import resolve_shutdown_watchdog_delay
from tests.gateway.restart_test_helpers import make_restart_runner

CLAMP = 60.0
CRON_CFG = 600.0
TEARDOWNS = (None, 22.0, 35.0, 45.0, 58.0)


@pytest.fixture(autouse=True)
def _reset_cron_running_set():
    import cron.scheduler as sched

    sched._running_job_ids.clear()
    sched._interrupted_job_ids.clear()
    yield
    sched._running_job_ids.clear()
    sched._interrupted_job_ids.clear()


def _armed_watchdog(chat_drain: float, clamp: float) -> float:
    """The hard-exit wall the stop path actually arms, per gateway/run.py."""
    return resolve_launchd_shutdown_watchdog_delay(
        resolve_shutdown_watchdog_delay(chat_drain),
        clamp,
        signal_driven=True,
    )


# ---------------------------------------------------------------------------
# The shared deadline
# ---------------------------------------------------------------------------


class TestResolveLaunchdDrainDeadline:
    def test_deadline_shrinks_with_the_measured_teardown(self):
        # Cold host: the 15s floor reserve applies.
        assert resolve_launchd_drain_deadline_s(CLAMP) == 35.0
        # A measured teardown longer than the floor widens the reserve.
        assert resolve_launchd_drain_deadline_s(CLAMP, last_teardown_s=22.0) == 28.0
        assert resolve_launchd_drain_deadline_s(CLAMP, last_teardown_s=45.0) == 5.0

    def test_a_shorter_measurement_never_shrinks_the_floor_reserve(self):
        assert (
            resolve_launchd_drain_deadline_s(CLAMP, last_teardown_s=3.0)
            == resolve_launchd_drain_deadline_s(CLAMP)
        )

    def test_saturating_teardown_yields_a_real_zero_not_a_floor(self):
        """Legitimate zero-drain saturation: no minimum is imposed."""
        assert resolve_launchd_drain_deadline_s(CLAMP, last_teardown_s=58.0) == 0.0
        assert resolve_launchd_drain_deadline_s(CLAMP, last_teardown_s=900.0) == 0.0

    def test_no_launchd_budget_imposes_no_deadline(self):
        assert resolve_launchd_drain_deadline_s(None) is None
        assert resolve_launchd_drain_deadline_s(0.0) is None
        assert resolve_launchd_drain_deadline_s(-5.0) is None
        assert resolve_launchd_drain_deadline_s("nope") is None  # type: ignore[arg-type]

    def test_garbage_teardown_degrades_to_the_floor_reserve(self):
        assert (
            resolve_launchd_drain_deadline_s(CLAMP, last_teardown_s="nope")  # type: ignore[arg-type]
            == resolve_launchd_drain_deadline_s(CLAMP)
        )


# ---------------------------------------------------------------------------
# Cron's allowance, bounded by that deadline
# ---------------------------------------------------------------------------


class TestCronBudgetHonoursTheDeadline:
    @pytest.mark.parametrize("last_teardown", TEARDOWNS)
    def test_cron_never_outlives_the_measured_teardown_reserve(self, last_teardown):
        """The card's table, recomposed with the deadline threaded through."""
        elapsed = 1.0
        chat = effective_stop_drain_timeout(
            _runner_like(drain=50.0, clamp=CLAMP, teardown=last_teardown)
        )
        deadline = resolve_launchd_drain_deadline_s(CLAMP, last_teardown_s=last_teardown)
        cron = resolve_cron_drain_budget(
            chat,
            CRON_CFG,
            watchdog_delay=resolve_shutdown_watchdog_delay(chat),
            elapsed=elapsed,
            deadline_s=deadline,
        )
        armed = _armed_watchdog(chat, CLAMP)
        reserve = max(LAUNCHD_STOP_CLEANUP_RESERVE_S, last_teardown or 0.0)
        assert deadline is not None, "a live launchd clamp must impose a deadline"

        # 1. Cron lands inside the one shared deadline. ``elapsed`` is time
        #    already spent before the drain, so what is left of the deadline is
        #    ``deadline - elapsed`` — clamped at 0 when the reserve already
        #    consumed the whole budget (a real zero, not a floor).
        assert cron <= max(deadline - elapsed, 0.0) + 1e-9, (
            f"cron ({cron}) overran the remaining deadline "
            f"({max(deadline - elapsed, 0.0)}) for teardown={last_teardown}"
        )
        # 2. ...and the drain cannot outlive the chat drain's own budget.
        assert cron <= max(chat, 0.0) + 1e-9

        # 3. The consequence the incident is about: the window left between the
        #    end of cron work and the armed hard exit is the reserve — or all
        #    the time that exists, when the measurement saturates the budget.
        window = armed - (cron + elapsed)
        assert window >= min(reserve, armed - elapsed) - 1e-9, (
            f"teardown={last_teardown}: only {window}s before the hard exit, "
            f"reserve is {reserve}s"
        )

    def test_the_old_fixed_reserve_formula_fails_this_invariant(self):
        """Non-vacuity: without ``deadline_s`` the same row is short.

        Pins the regression itself — if the deadline stops being threaded
        through, this is what the budget goes back to.
        """
        elapsed = 1.0
        chat = effective_stop_drain_timeout(
            _runner_like(drain=50.0, clamp=CLAMP, teardown=22.0)
        )
        old = resolve_cron_drain_budget(
            chat,
            CRON_CFG,
            watchdog_delay=min(resolve_shutdown_watchdog_delay(chat), CLAMP),
            elapsed=elapsed,
            # deadline_s omitted == the pre-fix call site
        )
        assert old == CLAMP - CRON_DRAIN_CLEANUP_RESERVE_S - elapsed  # 49.0, pinned
        armed = _armed_watchdog(chat, CLAMP)
        assert armed - (old + elapsed) < 22.0, "old formula must be SHORT of the reserve"

    def test_cron_floor_still_extends_a_short_drain_within_the_deadline(self):
        """The #82161 intent is preserved: the floor only ever EXTENDS."""
        deadline = resolve_launchd_drain_deadline_s(CLAMP)  # 35.0
        budget = resolve_cron_drain_budget(
            0.0, CRON_CFG, watchdog_delay=95.0, elapsed=0.0, deadline_s=deadline
        )
        assert budget == 35.0 > 0.0

    def test_configured_long_drain_is_never_shortened_below_the_deadline(self):
        deadline = resolve_launchd_drain_deadline_s(CLAMP)  # 35.0
        assert (
            resolve_cron_drain_budget(
                35.0, 0.0, watchdog_delay=95.0, elapsed=0.0, deadline_s=deadline
            )
            == 35.0
        )

    def test_no_deadline_keeps_the_pre_existing_behaviour(self):
        with_none = resolve_cron_drain_budget(
            0.0, CRON_CFG, watchdog_delay=95.0, elapsed=0.0, deadline_s=None
        )
        assert with_none == 95.0 - CRON_DRAIN_CLEANUP_RESERVE_S

    def test_saturated_deadline_gives_cron_zero_without_a_floor(self):
        deadline = resolve_launchd_drain_deadline_s(CLAMP, last_teardown_s=58.0)
        assert deadline == 0.0
        assert (
            resolve_cron_drain_budget(
                0.0, CRON_CFG, watchdog_delay=95.0, elapsed=1.0, deadline_s=deadline
            )
            == 0.0
        )


def _runner_like(*, drain: float, clamp: float | None, teardown: float | None):
    from types import SimpleNamespace

    return SimpleNamespace(
        _restart_drain_timeout=drain,
        _launchd_exit_timeout_s=clamp,
        _stop_requested_by_signal=True,
        _last_shutdown_teardown_s=teardown,
    )


# ---------------------------------------------------------------------------
# The composed stop path (this is what the isolated helpers do not prove)
# ---------------------------------------------------------------------------


def _stop_runner(*, clamp: float | None, teardown: float | None, drain: float = 50.0):
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    runner.session_store = MagicMock()
    runner.session_store._entries = {}
    runner.session_store.mark_resume_pending = MagicMock(return_value=True)
    runner.session_store.clear_resume_pending = MagicMock(return_value=True)
    runner._restart_drain_timeout = drain
    runner._cron_drain_timeout = CRON_CFG
    runner._launchd_exit_timeout_s = clamp
    runner._stop_requested_by_signal = True
    runner._last_shutdown_teardown_s = teardown
    return runner


async def _composed_budgets(runner) -> tuple[float, float]:
    """Run the real stop() and capture the (chat, cron) pair it composed."""
    captured: dict[str, float] = {}
    real_drain = runner._drain_active_agents

    async def _spy(timeout, cron_timeout=None):
        captured["chat"] = timeout
        captured["cron"] = timeout if cron_timeout is None else cron_timeout
        # Do not actually wait out the budget — the composition is the subject.
        import cron.scheduler as sched

        sched._running_job_ids.clear()
        return await real_drain(0.0, 0.0)

    runner._drain_active_agents = _spy
    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()
    assert "cron" in captured, "stop() never reached the drain"
    return captured["chat"], captured["cron"]


class TestComposedStopPath:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("teardown", TEARDOWNS)
    async def test_stop_path_leaves_the_measured_reserve_before_the_clamp(self, teardown):
        """Active cron work + a live launchd clamp: the budget the stop path
        actually hands ``_drain_active_agents`` must fit inside the deadline."""
        import cron.scheduler as sched

        sched._running_job_ids.add("in-flight-cron")
        runner = _stop_runner(clamp=CLAMP, teardown=teardown)

        chat, cron = await _composed_budgets(runner)

        deadline = resolve_launchd_drain_deadline_s(CLAMP, last_teardown_s=teardown)
        armed = _armed_watchdog(chat, CLAMP)
        reserve = max(LAUNCHD_STOP_CLEANUP_RESERVE_S, teardown or 0.0)
        assert deadline is not None, "a live launchd clamp must impose a deadline"

        assert chat <= deadline + 1e-9
        assert cron <= deadline + 1e-9, (
            f"teardown={teardown}: cron budget {cron}s overran the shared "
            f"deadline {deadline}s — in-flight cron work can hold the drain "
            f"past the reserve this host measured"
        )
        assert armed - cron >= min(reserve, armed) - 1e-9

    @pytest.mark.asyncio
    async def test_cron_budget_is_pinned_to_the_deadline_not_the_raw_clamp(self):
        """The binding term must be ``clamp - hard_exit_reserve - teardown``,
        never the raw clamp: at teardown=22 the old call site produced 50.0."""
        import cron.scheduler as sched

        sched._running_job_ids.add("in-flight-cron")
        runner = _stop_runner(clamp=CLAMP, teardown=22.0)

        _chat, cron = await _composed_budgets(runner)

        assert cron == pytest.approx(28.0, abs=0.5)
        assert cron < CLAMP - CRON_DRAIN_CLEANUP_RESERVE_S

    @pytest.mark.asyncio
    async def test_non_launchd_stop_keeps_the_configured_cron_floor(self):
        """No clamp, no deadline: the #82161 floor behaviour is untouched."""
        import cron.scheduler as sched

        sched._running_job_ids.add("in-flight-cron")
        runner = _stop_runner(clamp=None, teardown=22.0, drain=0.0)

        chat, cron = await _composed_budgets(runner)

        assert chat == 0.0
        assert cron > 0.0, "cron floor must still extend a zero chat drain"

    @pytest.mark.asyncio
    async def test_in_band_restart_is_not_launchd_timed(self):
        """A non-signal stop keeps the operator's configured drain."""
        import cron.scheduler as sched

        sched._running_job_ids.add("in-flight-cron")
        runner = _stop_runner(clamp=CLAMP, teardown=45.0)
        runner._stop_requested_by_signal = False

        chat, cron = await _composed_budgets(runner)

        assert chat == 50.0
        assert cron >= 50.0
