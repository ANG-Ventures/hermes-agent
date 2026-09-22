"""Signal-driven stops under launchd must fit the live ``ExitTimeOut``.

launchd's per-user (gui) domain clamps ``ExitTimeOut`` (measured 60s on
macOS 26: plist 215 -> live 60). A gateway configured with a longer
``restart_drain_timeout`` drains past that budget and is SIGKILLed mid
SQLite teardown — the unclean-exit half of the state.db corruption class.
The gateway therefore reads the live value at boot and caps only the
signal-driven stop drain (in-band SIGUSR1 restarts and --replace
takeovers are not launchd-timed and keep the configured drain).
"""

from __future__ import annotations

import subprocess
import threading
from unittest.mock import AsyncMock, patch

import pytest

from gateway.restart import (
    LAUNCHD_STOP_CLEANUP_RESERVE_S,
    launchd_service_label,
    parse_launchd_exit_timeout,
    read_launchd_exit_timeout_s,
    resolve_launchd_capped_drain,
    resolve_launchd_shutdown_watchdog_delay,
)
from gateway.shutdown_watchdog import (
    DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S,
    arm_shutdown_watchdog,
    resolve_shutdown_watchdog_delay,
)

_PRINT_OUTPUT = """\
ai.hermes.gateway-aegis = {
\tactive count = 1
\tpath = /Users/x/Library/LaunchAgents/ai.hermes.gateway-aegis.plist
\tstate = running

\tprogram = /bin/sh
\targuments = {
\t\t/bin/sh
\t\t-lc
\t\texec hermes gateway run
\t}

\tdefault environment = {
\t\tPATH => /usr/bin:/bin:/usr/sbin:/sbin
\t}

\tenvironment = {
\t\tXPC_SERVICE_NAME => ai.hermes.gateway-aegis
\t}

\tdomain = gui/501 [100010]
\tminimum runtime = 10
\texit timeout = 60
\truns = 3
\tpid = 83601
\tsuccessive crashes = 0
"""


# ---------------------------------------------------------------------------
# parse / read
# ---------------------------------------------------------------------------


def test_parse_exit_timeout_from_launchctl_print():
    assert parse_launchd_exit_timeout(_PRINT_OUTPUT) == 60.0


@pytest.mark.parametrize("text", ["", None, "state = running\n", "exit timeout = abc\n"])
def test_parse_exit_timeout_absent_returns_none(text):
    assert parse_launchd_exit_timeout(text) is None


def test_parse_exit_timeout_ignores_lookalike_keys():
    # "minimum runtime" and other "= N" lines must not be mistaken for it.
    assert parse_launchd_exit_timeout("\tminimum runtime = 10\n\tpid = 5\n") is None


def test_service_label_from_xpc_env():
    assert launchd_service_label({"XPC_SERVICE_NAME": "ai.hermes.gateway"}) == "ai.hermes.gateway"
    assert launchd_service_label({"XPC_SERVICE_NAME": "0"}) is None
    assert launchd_service_label({"XPC_SERVICE_NAME": ""}) is None
    assert launchd_service_label({}) is None


def test_read_exit_timeout_queries_gui_domain_for_label():
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        assert kwargs.get("timeout"), "launchctl probe must be bounded"
        return subprocess.CompletedProcess(argv, 0, stdout=_PRINT_OUTPUT, stderr="")

    value = read_launchd_exit_timeout_s(
        environ={"XPC_SERVICE_NAME": "ai.hermes.gateway-aegis"}, uid=501, run=fake_run
    )
    assert value == 60.0
    assert calls == [["launchctl", "print", "gui/501/ai.hermes.gateway-aegis"]]


def test_read_exit_timeout_uses_system_domain_for_root():
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=_PRINT_OUTPUT, stderr="")

    read_launchd_exit_timeout_s(
        environ={"XPC_SERVICE_NAME": "ai.hermes.gateway"}, uid=0, run=fake_run
    )
    assert calls == [["launchctl", "print", "system/ai.hermes.gateway"]]


def test_read_exit_timeout_not_launchd_owned_skips_probe():
    def fake_run(*_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("launchctl must not run when XPC_SERVICE_NAME is unset")

    assert read_launchd_exit_timeout_s(environ={}, uid=501, run=fake_run) is None
    assert (
        read_launchd_exit_timeout_s(environ={"XPC_SERVICE_NAME": "0"}, uid=501, run=fake_run)
        is None
    )


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("launchctl"),
        subprocess.TimeoutExpired(cmd="launchctl", timeout=5),
        OSError("boom"),
    ],
)
def test_read_exit_timeout_fails_open_on_probe_errors(failure):
    def fake_run(*_a, **_k):
        raise failure

    assert (
        read_launchd_exit_timeout_s(
            environ={"XPC_SERVICE_NAME": "ai.hermes.gateway"}, uid=501, run=fake_run
        )
        is None
    )


def test_read_exit_timeout_fails_open_on_nonzero_rc():
    def fake_run(argv, **_k):
        return subprocess.CompletedProcess(argv, 113, stdout="", stderr="Could not find service")

    assert (
        read_launchd_exit_timeout_s(
            environ={"XPC_SERVICE_NAME": "ai.hermes.gateway"}, uid=501, run=fake_run
        )
        is None
    )


# ---------------------------------------------------------------------------
# resolve_launchd_capped_drain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("configured", "last_teardown", "expected"),
    [
        # clamp 60 - hard-exit reserve 10 - teardown reserve 15 = 35
        (50.0, None, 35.0),
        (30.0, None, 30.0),
        # clamp 60 - hard-exit reserve 10 - measured teardown 22 = 28
        (50.0, 22.0, 28.0),
    ],
)
def test_capped_drain_preserves_teardown_headroom(configured, last_teardown, expected):
    assert (
        resolve_launchd_capped_drain(
            configured,
            60.0,
            last_teardown_s=last_teardown,
        )
        == expected
    )


def test_capped_drain_never_extends_a_short_drain():
    assert resolve_launchd_capped_drain(20.0, 60.0) == 20.0
    assert resolve_launchd_capped_drain(0.0, 60.0) == 0.0


@pytest.mark.parametrize(
    ("clamp", "configured", "last_teardown"),
    [
        (60.0, 50.0, None),
        (60.0, 50.0, 22.0),
        (60.0, 180.0, None),
        (50.0, 50.0, None),
        (30.0, 50.0, None),
        (60.0, 50.0, 35.0),
        # FleetReview's worked counterexample: a system-domain launchd job
        # is NOT gui-clamped to 60, so a sample above the watchdog grace
        # reaches a clamp high enough for the inner leash to bind. The
        # old span-only ceiling accepted the 70s sample (ceiling 290),
        # giving drain 180 / armed 240 — a 60s window for a 70s reserve.
        (300.0, 180.0, 70.0),
        (180.0, 180.0, 70.0),
        (120.0, 180.0, 90.0),
    ],
)
def test_teardown_reserve_fits_before_the_watchdog_that_actually_fires(
    clamp, configured, last_teardown
):
    """The two reserves are additive, not overlapping.

    The drain cap protects a teardown window ending at launchd's SIGKILL,
    but the process hard-exits ``LAUNCHD_HARD_EXIT_RESERVE_S`` earlier. The
    reserve must therefore fit between the end of the drain and the *armed
    watchdog*, otherwise os._exit lands mid-persistence — the state.db
    corruption class this card exists to close.

    The reserve is read through the same bound production uses at boot
    (``read_last_teardown_seconds(max_seconds=...)``), because a sample the
    gateway would never load is not a reserve it promises to honour.
    """
    from gateway.restart import (
        LAUNCHD_STOP_CLEANUP_RESERVE_S,
        resolve_armed_shutdown_watchdog_delay,
        resolve_max_actionable_teardown_reserve_s,
    )

    # Production boot bounds the sample before it ever becomes a reserve.
    ceiling = resolve_max_actionable_teardown_reserve_s(clamp)
    effective_teardown = last_teardown
    if (
        effective_teardown is not None
        and ceiling is not None
        and effective_teardown >= ceiling
    ):
        effective_teardown = None

    drain = resolve_launchd_capped_drain(
        configured, clamp, last_teardown_s=effective_teardown
    )
    # Arm with the same reserve the production call site passes: the leash
    # is max(grace, reserve), so a sample above the grace is only honoured
    # when it reaches the arming site (see
    # test_stop_arms_the_watchdog_with_the_measured_teardown_reserve).
    armed = resolve_armed_shutdown_watchdog_delay(
        drain, clamp, signal_driven=True, last_teardown_s=effective_teardown
    )
    promised_reserve = max(
        LAUNCHD_STOP_CLEANUP_RESERVE_S, effective_teardown or 0.0
    )
    assert armed - drain >= promised_reserve, (
        f"clamp={clamp} drain={drain} watchdog={armed}: only "
        f"{armed - drain}s before hard exit for a {promised_reserve}s teardown"
    )
    # The reserve is carved out of the drain, never borrowed past the wall.
    assert drain + promised_reserve <= clamp


def test_watchdog_leash_widens_to_the_reserve_instead_of_discarding_it():
    """A measured teardown above the grace widens the leash, not the bin.

    The armed deadline is ``min(drain + leash, hard_exit)``. Bounding the
    teardown CEILING by the grace was the wrong lever: it discarded a valid
    measurement and collapsed the promise to the fixed 15s cleanup reserve,
    which is strictly worse than the sample it replaced — at clamp 120 /
    configured 180 / measured 65 that yielded drain 95 and a 15s window for
    a teardown known to take 65s. Widening the leash to ``max(grace,
    reserve)`` honours the measurement, and the outer ``min()`` keeps the
    deadline inside the hard exit.

    Both regions are pinned deliberately: at the production clamp the
    hard-exit line binds and this is inert, and at a high clamp the grace
    branch binds and the widening is what does the work. A later change to
    the grace therefore goes red here instead of silently flipping the
    production case.
    """
    from gateway.restart import (
        LAUNCHD_HARD_EXIT_RESERVE_S,
        resolve_armed_shutdown_watchdog_delay,
        resolve_max_actionable_teardown_reserve_s,
    )
    from gateway.shutdown_watchdog import (
        DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S as GRACE,
    )

    # The ceiling is the hard-exit span at EVERY clamp: the grace is not a
    # bound on the sample any more.
    for clamp in (60.0, 300.0):
        assert resolve_max_actionable_teardown_reserve_s(clamp) == (
            clamp - LAUNCHD_HARD_EXIT_RESERVE_S
        )

    # Production clamp: the hard-exit line binds, so the leash never shows.
    drain_60 = resolve_launchd_capped_drain(50.0, 60.0, last_teardown_s=22.0)
    assert drain_60 == 28.0
    assert (
        resolve_armed_shutdown_watchdog_delay(
            drain_60, 60.0, signal_driven=True, last_teardown_s=22.0
        )
        == 60.0 - LAUNCHD_HARD_EXIT_RESERVE_S
    )

    # High clamp, sample ABOVE the grace: the leash widens to the reserve.
    drain_300 = resolve_launchd_capped_drain(180.0, 300.0, last_teardown_s=70.0)
    armed_300 = resolve_armed_shutdown_watchdog_delay(
        drain_300, 300.0, signal_driven=True, last_teardown_s=70.0
    )
    assert armed_300 - drain_300 >= 70.0
    assert armed_300 == drain_300 + 70.0  # leash = reserve, not grace
    assert armed_300 < 300.0 - LAUNCHD_HARD_EXIT_RESERVE_S

    # High clamp, sample BELOW the grace: the grace still wins.
    drain_g = resolve_launchd_capped_drain(180.0, 300.0, last_teardown_s=20.0)
    assert (
        resolve_armed_shutdown_watchdog_delay(
            drain_g, 300.0, signal_driven=True, last_teardown_s=20.0
        )
        == drain_g + GRACE
    )

    # The window never under-promises the reserve, at any clamp.
    for clamp in (30.0, 60.0, 120.0, 300.0, 600.0):
        ceiling = resolve_max_actionable_teardown_reserve_s(clamp)
        assert ceiling is not None
        sample = ceiling - 0.5
        drain = resolve_launchd_capped_drain(600.0, clamp, last_teardown_s=sample)
        armed = resolve_armed_shutdown_watchdog_delay(
            drain, clamp, signal_driven=True, last_teardown_s=sample
        )
        assert armed - drain >= sample, (
            f"clamp={clamp}: reserve {sample} promises more than the "
            f"{armed - drain}s window the watchdog leaves"
        )


def test_oversized_reserve_cannot_starve_the_drain_at_the_arithmetic():
    """The bound lives in the resolver, not only at the ledger read.

    ``last_teardown_s`` also reaches the cap through
    ``effective_stop_drain_timeout`` from a runner attribute, so the
    read-boundary filter alone is not a choke point. A sample at or above
    the actionable ceiling must degrade to the fixed cleanup reserve rather
    than zero the drain — dropping every in-flight session with no drain
    while still not making the teardown fit.
    """
    from gateway.restart import resolve_max_actionable_teardown_reserve_s

    ceiling = resolve_max_actionable_teardown_reserve_s(60.0)
    assert ceiling == 50.0

    # At the ceiling and above: the reserve is not honoured, drain survives.
    assert resolve_launchd_capped_drain(50.0, 60.0, last_teardown_s=50.0) == 35.0
    assert resolve_launchd_capped_drain(50.0, 60.0, last_teardown_s=55.0) == 35.0
    assert resolve_launchd_capped_drain(50.0, 60.0, last_teardown_s=1e9) == 35.0
    # Below the ceiling: honoured exactly as before.
    assert resolve_launchd_capped_drain(50.0, 60.0, last_teardown_s=22.0) == 28.0
    assert resolve_launchd_capped_drain(50.0, 60.0, last_teardown_s=45.0) == 5.0


def test_short_budget_window_is_monotonic_in_the_budget():
    """A larger ExitTimeOut must never arm a SMALLER shutdown window.

    The original guard (``reserve >= budget``) left a cliff just above the
    fixed reserve: budget 10 armed 7.5s but budget 11 armed 1.0s and 12
    armed 2.0s. Moving the gate to ``budget - reserve < budget * FRACTION``
    only moves the cliff (at FRACTION=0.25, budget 13.33 armed 9.9975s and
    13.34 armed 3.34s). A 1s watchdog calls ``os._exit`` one second into
    the stop — before the drain, before agents are interrupted, before the
    SQLite checkpoint — the silent-loss outcome the fallback exists to
    prevent.
    """
    from gateway.restart import resolve_launchd_shutdown_watchdog_delay

    previous = None
    for step in range(2, 8001):
        budget = step / 100.0
        armed = resolve_launchd_shutdown_watchdog_delay(
            999.0, budget, signal_driven=True
        )
        assert 0.0 < armed < budget, f"budget={budget} armed={armed}"
        if previous is not None:
            assert armed >= previous - 1e-9, (
                f"budget={budget} armed {armed} < {previous} at the "
                f"previous (smaller) budget"
            )
        previous = armed

    # Inert at every clamp launchd actually hands out: the fixed reserve
    # is affordable there, so these are unchanged by the fallback.
    assert resolve_launchd_shutdown_watchdog_delay(
        999.0, 30.0, signal_driven=True
    ) == 20.0
    assert resolve_launchd_shutdown_watchdog_delay(
        999.0, 60.0, signal_driven=True
    ) == 50.0


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [(0.0, 28.0), (5.0, 23.0), (15.0, 13.0), (28.0, 0.0), (99.0, 0.0)],
)
def test_pre_drain_elapsed_comes_out_of_the_drain_not_the_reserve(
    elapsed, expected
):
    """The watchdog deadline is absolute; the drain budget is relative.

    ``arm_shutdown_watchdog`` fires at ``clamp - hard_exit_reserve`` measured
    from the top of the stop, but the drain only starts after the pre-drain
    phases (reconnect cancel, per-session notify sends, boot-resume cancel,
    resume_pending marking). Without subtracting that elapsed time the
    post-drain teardown gets ``reserve - elapsed``: at the production clamp
    a 15s pre-drain phase consumes the whole 15s default reserve and puts
    os._exit inside the SQLite checkpoint.
    """
    from gateway.restart import resolve_elapsed_adjusted_drain

    drain = resolve_launchd_capped_drain(50.0, 60.0, last_teardown_s=22.0)
    assert drain == 28.0
    # The recorded sample has to reach BOTH derivations. The cap sized this
    # drain against a 22s teardown reserve, so the resolver must fit it
    # against that same deadline (hard exit 50 - 22 = 28) -- the production
    # call site passes the runner's `_last_shutdown_teardown_s` to each.
    assert (
        resolve_elapsed_adjusted_drain(
            drain,
            60.0,
            signal_driven=True,
            elapsed_s=elapsed,
            last_teardown_s=22.0,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("configured", "elapsed", "expected"),
    [
        # FleetReview's two worked examples at the production clamp
        # (budget 60, hard-exit reserve 10, teardown reserve 15 ->
        # the drain must merely END by +35s).
        #
        # configured 45 sits ABOVE the deadline, so the deadline binds and
        # the elapsed genuinely comes off: min(45, 35-12) = 23.
        (45.0, 12.0, 23.0),
        # configured 20 sits BELOW the deadline. The operator asked for a
        # 20s drain and 20s still fits (12 + 20 = 32 <= 35), so the full
        # configured drain stands: min(20, 35-12) = 20, NOT 20 - 12 = 8.
        (20.0, 12.0, 20.0),
        # The live geometry of the 09-21 incident host: ExitTimeOut 60,
        # restart_drain_timeout 30. 30s still fits under the +35s deadline
        # for any elapsed <= 5s, so a 2s pre-drain phase must not cost the
        # in-flight turns 2s of drain while 18s of window sits idle.
        (30.0, 2.0, 30.0),
        (30.0, 5.0, 30.0),
        # Past that point the deadline binds and the drain shrinks to fit.
        (30.0, 12.0, 23.0),
        (30.0, 30.0, 5.0),
        # Saturation: nothing left to drain with, but never negative.
        (30.0, 35.0, 0.0),
        (30.0, 99.0, 0.0),
    ],
)
def test_drain_is_bounded_by_the_deadline_not_reduced_by_a_full_subtraction(
    configured, elapsed, expected
):
    """The drain is ``min(configured, deadline - elapsed)``, not ``drain - elapsed``.

    The pre-drain phases consume the *absolute* window between the top of
    ``stop()`` and the hard exit, so what the elapsed time can take away is
    the deadline, never the operator's configured drain directly. Treating
    the launchd budget as a boolean gate and always returning
    ``drain - elapsed`` charges the elapsed twice whenever the configured
    drain already fits inside the remaining window: it drops in-flight
    sessions early while the supervisor window still has unused headroom
    (measured: 41.5% of 43,920 swept geometries over-subtract, worst case
    60s of drain discarded at clamp 300).

    The deadline here is ``exit_timeout - hard_exit_reserve - teardown``
    = ``60 - 10 - 15`` = 35s measured from the start of the stop.
    """
    from gateway.restart import resolve_elapsed_adjusted_drain

    drain = resolve_launchd_capped_drain(configured, 60.0)
    assert resolve_elapsed_adjusted_drain(
        drain, 60.0, signal_driven=True, elapsed_s=elapsed
    ) == pytest.approx(expected)


def test_stop_hands_the_measured_teardown_to_the_drain_resolver():
    """The recorded sample must reach BOTH derivations of the one deadline.

    ``effective_stop_drain_timeout`` already shrinks the cap with the
    runner's ``_last_shutdown_teardown_s``. If the resolver does not also
    receive it, the resolver fits the drain against the DEFAULT 15s-reserve
    deadline while the watchdog is actually armed for the larger measured
    reserve — the drain then runs past the window the process really has,
    which is the SIGKILL-mid-persistence failure this card exists to close.

    Guards the production wiring at ``gateway/run.py``: removing the
    ``last_teardown_s=`` argument there has to go red.
    """
    from gateway.restart import resolve_elapsed_adjusted_drain

    # clamp 60 -> hard exit 50. A recorded 22s teardown moves the deadline
    # from 50-15=35 down to 50-22=28.
    drain = resolve_launchd_capped_drain(20.0, 60.0, last_teardown_s=22.0)
    assert drain == 20.0

    unwired = resolve_elapsed_adjusted_drain(
        drain, 60.0, signal_driven=True, elapsed_s=15.0
    )
    wired = resolve_elapsed_adjusted_drain(
        drain, 60.0, signal_driven=True, elapsed_s=15.0, last_teardown_s=22.0
    )
    # Unwired fits against the wrong (wider) deadline and overruns.
    assert unwired == pytest.approx(20.0)
    assert wired == pytest.approx(13.0)
    assert 15.0 + wired == pytest.approx(28.0), (
        "the drain must end exactly at the measured-teardown deadline"
    )


def test_elapsed_adjusted_drain_never_runs_past_the_hard_exit_deadline():
    """The bound that must hold everywhere: drain must END before os._exit.

    Sweeps the supervisor geometries launchd actually hands out. For every
    row the drain has to finish with the full teardown reserve still intact
    -- that is the invariant the whole card exists to protect, and it is
    what stops the "just give the drain more time" correction from
    re-opening the SIGKILL-mid-persistence failure.

    The expected deadline is measured against the deadline the watchdog is
    ACTUALLY ARMED with (``resolve_armed_shutdown_watchdog_delay``, the
    value ``gateway/run.py`` hands ``arm_shutdown_watchdog``), NOT against
    ``exit_timeout - hard_exit_reserve_s``. The two agree only when the
    outer ``min()`` of the arming expression binds -- true at the
    gui-clamped 60 and false on any clamp where the inner
    ``drain + max(grace, reserve)`` leash binds. Re-deriving the hard-exit
    expression here was finding 3 of the #838 review: the assertion
    restated the implementation's own assumption and so held by
    construction while the real window was being starved.
    """
    from gateway.restart import (
        resolve_armed_shutdown_watchdog_delay,
        resolve_elapsed_adjusted_drain,
        resolve_stop_teardown_reserve_s,
    )

    violations = []
    for clamp in (30.0, 45.0, 60.0, 90.0, 120.0, 300.0):
        for configured in (5.0, 20.0, 30.0, 45.0, 60.0, 120.0):
            for teardown in (None, 22.0, 70.0):
                drain = resolve_launchd_capped_drain(
                    configured, clamp, last_teardown_s=teardown
                )
                reserve = resolve_stop_teardown_reserve_s(
                    clamp, last_teardown_s=teardown
                )
                for elapsed in (0.0, 2.0, 5.0, 12.0, 20.0, 35.0, 60.0):
                    # The watchdog the process is running under, re-armed
                    # with this elapsed exactly as _stop_impl_body does.
                    armed = resolve_armed_shutdown_watchdog_delay(
                        drain,
                        clamp,
                        signal_driven=True,
                        last_teardown_s=teardown,
                        elapsed_s=elapsed,
                    )
                    adjusted = resolve_elapsed_adjusted_drain(
                        drain,
                        clamp,
                        signal_driven=True,
                        elapsed_s=elapsed,
                        last_teardown_s=teardown,
                        armed_deadline_s=armed,
                    )
                    if adjusted <= 0.0:
                        continue
                    window = armed - (elapsed + adjusted)
                    if window < reserve - 1e-9:
                        violations.append(
                            f"clamp={clamp} configured={configured} "
                            f"teardown={teardown} elapsed={elapsed} "
                            f"drain={adjusted} ends at {elapsed + adjusted} "
                            f"against armed {armed}: {window}s left for a "
                            f"{reserve}s teardown"
                        )
    assert not violations, (
        "drain runs into the teardown window the watchdog grants:\n"
        + "\n".join(violations)
    )


def test_drain_deadline_is_the_armed_watchdog_at_the_inner_leash_geometry():
    """Hard-coded oracle at the geometry where the two deadlines DIFFER.

    Every number below is computed BY HAND from the review's worked
    example, not from the functions under test -- that independence is the
    point (finding 3). Geometry: ``ExitTimeOut`` 300 (system-domain
    launchd is not gui-clamped to 60), configured drain 180, measured
    teardown 70, watchdog grace 60.

    By hand::

        hard_exit = 300 - 10 (hard_exit_reserve)          = 290
        reserve   = max(15, 70)                           =  70
        capped    = min(180, 290 - 70)                    = 180
        armed     = min(180 + max(60, 70), 290)           = 250   <- INNER binds
        deadline  = 250 - 70                              = 180

    The hard-exit derivation this replaces gives ``290 - 70`` = **220**,
    40s later than the process actually has. Fitting the drain to 220
    charged every second of pre-drain elapsed to the 70s teardown reserve,
    which is the exact failure #838 was supposed to close.
    """
    from gateway.restart import (
        resolve_armed_shutdown_watchdog_delay,
        resolve_stop_drain_deadline_s,
    )

    CLAMP, CONFIGURED, TEARDOWN = 300.0, 180.0, 70.0

    capped = resolve_launchd_capped_drain(
        CONFIGURED, CLAMP, last_teardown_s=TEARDOWN
    )
    assert capped == pytest.approx(180.0)

    armed = resolve_armed_shutdown_watchdog_delay(
        capped, CLAMP, signal_driven=True, last_teardown_s=TEARDOWN
    )
    # The INNER leash binds here: 250, not the hard exit 290.
    assert armed == pytest.approx(250.0), (
        "the inner drain+max(grace,reserve) leash must bind at clamp 300 -- "
        "if this is 290 the geometry no longer exercises the defect"
    )
    assert armed < 290.0

    deadline = resolve_stop_drain_deadline_s(
        capped,
        CLAMP,
        signal_driven=True,
        last_teardown_s=TEARDOWN,
        armed_deadline_s=armed,
    )
    # Hard-coded: 250 - 70. NOT 290 - 70 = 220.
    assert deadline == pytest.approx(180.0), (
        f"deadline {deadline} must be the armed watchdog (250) minus the "
        f"measured reserve (70) = 180, not the hard-exit wall's 220"
    )
    assert deadline != pytest.approx(220.0)


def test_rearmed_watchdog_preserves_the_teardown_reserve_across_elapsed():
    """The re-arm (finding 4) buys the elapsed back from the WALL, not the reserve.

    Hard-coded from the same clamp-300 geometry. The top-of-``stop()``
    arming cannot know the pre-drain cost, so it arms at 250; the drain
    then starts at ``elapsed`` and ends at ``elapsed + 180``, leaving
    ``70 - elapsed`` for a teardown measured at 70. Re-arming once the
    elapsed is measured moves ``os._exit`` out by exactly that elapsed
    (bounded by the 290 hard-exit wall), so the window stays 70.

    By hand, drain 180 / reserve 70 / hard exit 290::

        elapsed  0 -> armed min(  0+250, 290) = 250, drain 180, ends 180, window 70
        elapsed 20 -> armed min( 20+250, 290) = 270, drain 180, ends 200, window 70
        elapsed 40 -> armed min( 40+250, 290) = 290, drain 180, ends 220, window 70
        elapsed 60 -> armed                     290, drain 160, ends 220, window 70

    At elapsed 40 the wall binds and the re-arm saturates; past that the
    DRAIN gives way instead (160 at elapsed 60), which is the correct
    trade -- the reserve is never the thing that pays.
    """
    from gateway.restart import (
        resolve_armed_shutdown_watchdog_delay,
        resolve_elapsed_adjusted_drain,
    )

    CLAMP, TEARDOWN, RESERVE = 300.0, 70.0, 70.0
    capped = resolve_launchd_capped_drain(180.0, CLAMP, last_teardown_s=TEARDOWN)

    expected = {
        0.0: (250.0, 180.0),
        20.0: (270.0, 180.0),
        40.0: (290.0, 180.0),
        60.0: (290.0, 160.0),
    }
    for elapsed, (want_armed, want_drain) in expected.items():
        armed = resolve_armed_shutdown_watchdog_delay(
            capped,
            CLAMP,
            signal_driven=True,
            last_teardown_s=TEARDOWN,
            elapsed_s=elapsed,
        )
        assert armed == pytest.approx(want_armed), (
            f"elapsed={elapsed}: re-armed deadline {armed}, expected "
            f"{want_armed}"
        )
        drain = resolve_elapsed_adjusted_drain(
            capped,
            CLAMP,
            signal_driven=True,
            elapsed_s=elapsed,
            last_teardown_s=TEARDOWN,
            armed_deadline_s=armed,
        )
        assert drain == pytest.approx(want_drain), (
            f"elapsed={elapsed}: drain {drain}, expected {want_drain}"
        )
        # The invariant, stated against the armed value: the teardown
        # window never shrinks below the measured reserve.
        assert armed - (elapsed + drain) == pytest.approx(RESERVE), (
            f"elapsed={elapsed}: only {armed - (elapsed + drain)}s left for "
            f"a {RESERVE}s teardown"
        )
    # The re-arm only ever EXTENDS: never earlier than the original arming.
    base = resolve_armed_shutdown_watchdog_delay(
        capped, CLAMP, signal_driven=True, last_teardown_s=TEARDOWN
    )
    for elapsed in (0.0, 5.0, 20.0, 40.0, 90.0, 400.0):
        assert (
            resolve_armed_shutdown_watchdog_delay(
                capped,
                CLAMP,
                signal_driven=True,
                last_teardown_s=TEARDOWN,
                elapsed_s=elapsed,
            )
            >= base
        )


def test_cron_branch_consumes_the_one_deadline_instead_of_the_sigkill_wall():
    """The cron floor may not raise the budget past the drain deadline (finding 2).

    Hard-coded at the PRODUCTION geometry, all by hand: ``ExitTimeOut`` 60,
    ``restart_drain_timeout`` 30, ``cron_drain_timeout`` 30, default 15s
    teardown reserve, 20s of pre-drain elapsed::

        hard_exit = 60 - 10                     = 50
        armed     = min(30 + max(60, 15), 50)   = 50   <- outer min binds
        deadline  = 50 - 15                     = 35
        drain     = min(30, 35 - 20)            = 15   (elapsed-adjusted)

    The OLD cron leash clamped to the raw ``ExitTimeOut`` (60, launchd's
    SIGKILL wall) and held back only the 10s ``CRON_DRAIN_CLEANUP_RESERVE_S``:
    ceiling ``60 - 20 - 10`` = 30, so it returned 30 -- ending at +50
    absolute, exactly when ``os._exit`` fires, with the entire 15s teardown
    reserve consumed. Consuming the deadline yields 15, ending at +35.
    """
    from gateway.restart import (
        resolve_armed_shutdown_watchdog_delay,
        resolve_cron_drain_budget,
        resolve_elapsed_adjusted_drain,
        resolve_stop_drain_deadline_s,
    )

    CLAMP, CRON_FLOOR, ELAPSED = 60.0, 30.0, 20.0
    capped = resolve_launchd_capped_drain(30.0, CLAMP)

    armed = resolve_armed_shutdown_watchdog_delay(
        capped, CLAMP, signal_driven=True, elapsed_s=ELAPSED
    )
    assert armed == pytest.approx(50.0)
    deadline = resolve_stop_drain_deadline_s(
        capped, CLAMP, signal_driven=True, armed_deadline_s=armed
    )
    assert deadline is not None
    assert deadline == pytest.approx(35.0)

    adjusted = resolve_elapsed_adjusted_drain(
        capped,
        CLAMP,
        signal_driven=True,
        elapsed_s=ELAPSED,
        armed_deadline_s=armed,
    )
    assert adjusted == pytest.approx(15.0)

    # The defect: re-deriving from the raw SIGKILL wall raises it back up.
    re_derived = resolve_cron_drain_budget(
        adjusted, CRON_FLOOR, watchdog_delay=CLAMP, elapsed=ELAPSED
    )
    assert re_derived == pytest.approx(30.0), (
        "geometry drifted -- this row must still reproduce the overrun the "
        "fix removes"
    )
    assert ELAPSED + re_derived == pytest.approx(50.0)
    # ...which is exactly the armed hard-exit instant: the whole reserve gone.
    assert armed == pytest.approx(50.0)

    # The fix: consume the deadline. Ends at 35, reserve intact.
    consumed = resolve_cron_drain_budget(
        adjusted,
        CRON_FLOOR,
        watchdog_delay=CLAMP,
        elapsed=ELAPSED,
        deadline_s=deadline,
    )
    assert consumed == pytest.approx(15.0), (
        f"cron budget {consumed} must not exceed the {deadline - ELAPSED}s "
        f"the deadline leaves"
    )
    assert ELAPSED + consumed == pytest.approx(35.0)
    assert armed - (ELAPSED + consumed) == pytest.approx(15.0), (
        "the full 15s teardown reserve must survive the cron wait"
    )

    # Sweep: the cron budget may never end past the deadline, at any
    # elapsed or floor.
    violations = []
    for elapsed in (0.0, 5.0, 12.0, 20.0, 30.0, 45.0):
        for floor in (0.0, 10.0, 30.0, 120.0):
            _armed = resolve_armed_shutdown_watchdog_delay(
                capped, CLAMP, signal_driven=True, elapsed_s=elapsed
            )
            _deadline = resolve_stop_drain_deadline_s(
                capped, CLAMP, signal_driven=True, armed_deadline_s=_armed
            )
            assert _deadline is not None
            _drain = resolve_elapsed_adjusted_drain(
                capped,
                CLAMP,
                signal_driven=True,
                elapsed_s=elapsed,
                armed_deadline_s=_armed,
            )
            _cron = resolve_cron_drain_budget(
                _drain,
                floor,
                watchdog_delay=CLAMP,
                elapsed=elapsed,
                deadline_s=_deadline,
            )
            if _cron > 0.0 and elapsed + _cron > _deadline + 1e-9:
                violations.append(
                    f"elapsed={elapsed} floor={floor} cron={_cron} ends at "
                    f"{elapsed + _cron} > deadline {_deadline}"
                )
    assert not violations, "cron wait overruns the deadline:\n" + "\n".join(
        violations
    )


def test_elapsed_adjustment_only_applies_to_launchd_timed_signal_stops():
    """Every other stop path has no absolute deadline to protect."""
    from gateway.restart import resolve_elapsed_adjusted_drain

    # Not launchd-owned (systemd, s6, foreground).
    assert (
        resolve_elapsed_adjusted_drain(
            50.0, None, signal_driven=True, elapsed_s=15.0
        )
        == 50.0
    )
    # In-band restart (SIGUSR1 -> after-turn -> stop()) is not launchd-timed.
    assert (
        resolve_elapsed_adjusted_drain(
            50.0, 60.0, signal_driven=False, elapsed_s=15.0
        )
        == 50.0
    )
    # Garbage inputs fail open to the configured drain.
    assert (
        resolve_elapsed_adjusted_drain(
            50.0, "nope", signal_driven=True, elapsed_s=15.0  # type: ignore[arg-type]
        )
        == 50.0
    )
    assert (
        resolve_elapsed_adjusted_drain(
            50.0, 60.0, signal_driven=True, elapsed_s="nope"  # type: ignore[arg-type]
        )
        == 50.0
    )


def test_budget_too_small_for_the_reserve_saturates_the_drain_to_zero():
    """When the clamp cannot hold the teardown, persistence wins, not the drain.

    This is honest degradation rather than a satisfiable window: an 8s
    ``ExitTimeOut`` has no 15s teardown to protect, so the drain goes to
    zero and the whole (short) budget is left for persistence. The property
    that must survive is that the hard exit still lands before SIGKILL.
    """
    from gateway.restart import resolve_launchd_shutdown_watchdog_delay

    for clamp in (8.0, 10.0, 4.0):
        drain = resolve_launchd_capped_drain(50.0, clamp)
        armed = resolve_launchd_shutdown_watchdog_delay(
            resolve_shutdown_watchdog_delay(drain), clamp, signal_driven=True
        )
        assert drain == 0.0
        assert 0.0 < armed < clamp


def test_short_launchd_budget_still_drains_and_persists():
    """A clamp at/below the hard-exit reserve must not mean 'exit immediately'.

    Returning a zero-second watchdog skips all drain and persistence work
    instead of using the little time that genuinely exists.
    """
    from gateway.restart import resolve_launchd_shutdown_watchdog_delay

    for clamp in (8.0, 10.0, 4.0):
        armed = resolve_launchd_shutdown_watchdog_delay(
            95.0, clamp, signal_driven=True
        )
        assert 0.0 < armed < clamp, (
            f"clamp={clamp}: watchdog {armed} leaves no time to persist"
        )


def test_capped_drain_no_launchd_budget_returns_configured():
    assert resolve_launchd_capped_drain(180.0, None) == 180.0
    assert resolve_launchd_capped_drain(180.0, 0.0) == 180.0
    assert resolve_launchd_capped_drain(180.0, -5.0) == 180.0


def test_capped_drain_tiny_budget_clamps_to_zero_not_negative():
    assert resolve_launchd_capped_drain(180.0, 5.0) == 0.0


def test_capped_drain_tolerates_garbage_inputs():
    assert resolve_launchd_capped_drain("nope", 60.0) == 0.0  # type: ignore[arg-type]
    assert resolve_launchd_capped_drain(180.0, "nope") == 180.0  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# GatewayRunner wiring
# ---------------------------------------------------------------------------


def _runner(*, drain: float, launchd: float | None, by_signal: bool):
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._restart_drain_timeout = drain
    runner._launchd_exit_timeout_s = launchd
    runner._stop_requested_by_signal = by_signal
    return runner


def test_effective_drain_capped_only_for_signal_stops_under_launchd():
    assert _runner(drain=180.0, launchd=60.0, by_signal=True)._effective_stop_drain_timeout() == 35.0
    # In-band restart (SIGUSR1 → after-turn → stop()) is not launchd-timed.
    assert _runner(drain=180.0, launchd=60.0, by_signal=False)._effective_stop_drain_timeout() == 180.0
    # Not launchd-owned (systemd, s6, foreground): configured drain stands.
    assert _runner(drain=180.0, launchd=None, by_signal=True)._effective_stop_drain_timeout() == 180.0


def test_effective_drain_getattr_guarded_for_bare_doubles():
    from types import SimpleNamespace

    from gateway.restart import effective_stop_drain_timeout
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._restart_drain_timeout = 45.0
    # No _stop_requested_by_signal / _launchd_exit_timeout_s set at all.
    assert runner._effective_stop_drain_timeout() == 45.0
    # Shutdown-path suites drive _stop_impl from non-GatewayRunner doubles.
    assert effective_stop_drain_timeout(SimpleNamespace(_restart_drain_timeout=45.0)) == 45.0
    assert (
        effective_stop_drain_timeout(
            SimpleNamespace(
                _restart_drain_timeout=180.0,
                _stop_requested_by_signal=True,
                _launchd_exit_timeout_s=60.0,
            )
        )
        == 35.0
    )


def test_load_launchd_exit_timeout_warns_when_drain_exceeds_budget(monkeypatch, caplog):
    import logging

    from gateway import run as run_mod

    monkeypatch.setattr(run_mod, "read_launchd_exit_timeout_s", lambda: 60.0)
    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.hermes.gateway-test")
    with caplog.at_level(logging.INFO, logger=run_mod.logger.name):
        assert run_mod.GatewayRunner._load_launchd_exit_timeout(180.0) == 60.0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "misconfiguration must be loud at boot"
    msg = warnings[0].getMessage()
    assert "180s" in msg and "60s" in msg and "ai.hermes.gateway-test" in msg


def test_load_launchd_exit_timeout_quiet_when_drain_fits(monkeypatch, caplog):
    import logging

    from gateway import run as run_mod

    monkeypatch.setattr(run_mod, "read_launchd_exit_timeout_s", lambda: 60.0)
    with caplog.at_level(logging.INFO, logger=run_mod.logger.name):
        assert run_mod.GatewayRunner._load_launchd_exit_timeout(30.0) == 60.0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_load_launchd_exit_timeout_none_when_not_launchd(monkeypatch):
    from gateway import run as run_mod

    monkeypatch.setattr(run_mod, "read_launchd_exit_timeout_s", lambda: None)
    assert run_mod.GatewayRunner._load_launchd_exit_timeout(180.0) is None


def test_cron_leash_under_launchd_cannot_exceed_exit_timeout():
    """The cron drain floor (#82161) is clamped to the launchd budget too.

    Without the clamp the cron ceiling is watchdog(drain+grace) - reserve,
    which for a capped 35s drain is 95s — past launchd's 60s SIGKILL.

    NOTE: the cron leash itself is sized against the 60s SIGKILL wall
    rather than the clamp-10 hard exit, so the cron floor can still
    stretch over the chat drain's teardown window. That is a real but
    SEPARATE defect, carved off to card t_f753b2b5 / PR #835; it is
    deliberately not fixed here to keep this branch's diff to the
    reserve arithmetic it owns.
    """
    from gateway.restart import CRON_DRAIN_CLEANUP_RESERVE_S, resolve_cron_drain_budget

    drain = resolve_launchd_capped_drain(180.0, 60.0)  # 35
    leash = min(resolve_shutdown_watchdog_delay(drain), 60.0)
    budget = resolve_cron_drain_budget(drain, 600.0, watchdog_delay=leash, elapsed=0.0)
    assert budget == max(drain, 60.0 - CRON_DRAIN_CLEANUP_RESERVE_S)
    assert budget <= 60.0
    # Sanity: the un-clamped leash would have blown the budget.
    unclamped = resolve_cron_drain_budget(
        drain, 600.0, watchdog_delay=resolve_shutdown_watchdog_delay(drain), elapsed=0.0
    )
    assert unclamped == drain + DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S - CRON_DRAIN_CLEANUP_RESERVE_S
    assert unclamped > 60.0


def test_launchd_shutdown_watchdog_hard_exits_before_supervisor_sigkill():
    assert (
        resolve_launchd_shutdown_watchdog_delay(
            95.0,
            60.0,
            signal_driven=True,
        )
        == 50.0
    )
    assert (
        resolve_launchd_shutdown_watchdog_delay(
            95.0,
            60.0,
            signal_driven=False,
        )
        == 95.0
    )


@pytest.mark.parametrize(
    "clamp, configured, last_teardown",
    [
        (60.0, 50.0, 22.0),   # FleetReview's worked example
        (60.0, 50.0, None),   # unmeasured teardown
        (60.0, 180.0, None),  # the 2026-09-21 incident shape
        (50.0, 50.0, None),
        (30.0, 50.0, None),
    ],
)
def test_armed_watchdog_lands_after_drain_plus_reserve(clamp, configured, last_teardown):
    """The armed deadline must clear ``drain + reserve``, measured end-to-end.

    FleetReview read the arming as "``effective_stop_drain_timeout()``
    (already reserve-subtracted) plus a small grace", concluding a 35s cap
    arms a ~40s watchdog and leaves persistence ~5s again. The grace is
    not small — it is ``DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S`` (60s) — so
    under launchd the inner leash always loses the ``min()`` and the armed
    deadline is ``clamp - LAUNCHD_HARD_EXIT_RESERVE_S``.

    This asserts the wall-clock deadline against the *production* helper
    ``gateway.run`` arms with, so a regression toward FleetReview's reading
    (arming from the reserve-subtracted delay) goes red here.
    """
    from gateway.restart import (
        LAUNCHD_HARD_EXIT_RESERVE_S,
        LAUNCHD_STOP_CLEANUP_RESERVE_S,
        resolve_armed_shutdown_watchdog_delay,
    )

    drain = resolve_launchd_capped_drain(
        configured, clamp, last_teardown_s=last_teardown
    )
    armed = resolve_armed_shutdown_watchdog_delay(drain, clamp, signal_driven=True)
    reserve = max(LAUNCHD_STOP_CLEANUP_RESERVE_S, last_teardown or 0.0)

    assert armed >= drain + reserve, (
        f"clamp={clamp} drain={drain} armed={armed}: only {armed - drain}s "
        f"before os._exit for a {reserve}s teardown"
    )
    # The armed deadline is the hard-exit line itself, not drain+grace.
    assert armed == clamp - LAUNCHD_HARD_EXIT_RESERVE_S
    # ...and it still lands before launchd's uncatchable SIGKILL.
    assert armed < clamp


def test_armed_watchdog_is_not_the_reserve_subtracted_delay():
    """Pin the refutation: arming from the capped drain is NOT drain+grace.

    This is the mutation FleetReview's finding describes. If the stop path
    ever armed the inner leash directly (``drain + grace``) without the
    launchd pull-back, the deadline would sit past SIGKILL; if it armed
    ``drain + small_grace``, persistence would get that small grace. Both
    shapes are excluded here by measurement.
    """
    from gateway.restart import resolve_armed_shutdown_watchdog_delay
    from gateway.shutdown_watchdog import resolve_shutdown_watchdog_delay

    drain = resolve_launchd_capped_drain(50.0, 60.0)  # 35.0
    armed = resolve_armed_shutdown_watchdog_delay(drain, 60.0, signal_driven=True)

    # FleetReview's predicted ~40s (drain + a small grace) would starve the
    # 15s reserve down to ~5s. The real deadline is 50.0.
    assert armed == 50.0
    assert armed - drain == 15.0
    # The un-clamped inner leash is way past the wall; the min() is load-bearing.
    assert resolve_shutdown_watchdog_delay(drain) == 95.0
    assert armed < resolve_shutdown_watchdog_delay(drain)


def test_grace_branch_binds_only_on_a_clamp_far_above_the_drain():
    """Document where ``drain + grace`` actually wins the ``min()``.

    The armed deadline is ``min(drain + grace, clamp - hard_exit_reserve)``.
    The inner term binds only when ``drain + grace < clamp - reserve``,
    i.e. on a clamp well above the drain — never at the production clamp
    (60, gui-domain-clamped). Pinning both sides means a later change to
    ``DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S`` cannot silently flip the
    production case with nothing going red: if the grace shrank to the
    "small grace" FleetReview assumed, the clamp-60 row below would start
    arming at ``drain + grace`` and fail.
    """
    from gateway.restart import (
        LAUNCHD_HARD_EXIT_RESERVE_S,
        resolve_armed_shutdown_watchdog_delay,
    )
    from gateway.shutdown_watchdog import (
        DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S as GRACE,
    )

    # Production geometry: the hard-exit line binds, so the full teardown
    # reserve survives.
    drain_60 = resolve_launchd_capped_drain(50.0, 60.0)
    assert drain_60 == 35.0
    assert (
        resolve_armed_shutdown_watchdog_delay(drain_60, 60.0, signal_driven=True)
        == 60.0 - LAUNCHD_HARD_EXIT_RESERVE_S
    )
    assert drain_60 + GRACE > 60.0 - LAUNCHD_HARD_EXIT_RESERVE_S

    # A clamp far above the drain: the inner leash binds instead, and the
    # deadline is still strictly inside the SIGKILL wall.
    drain_300 = resolve_launchd_capped_drain(50.0, 300.0)
    armed_300 = resolve_armed_shutdown_watchdog_delay(
        drain_300, 300.0, signal_driven=True
    )
    assert drain_300 == 50.0  # configured drain fits whole
    assert armed_300 == drain_300 + GRACE
    assert armed_300 < 300.0 - LAUNCHD_HARD_EXIT_RESERVE_S


def test_stop_arms_the_watchdog_at_the_hard_exit_deadline(monkeypatch, tmp_path):
    """Drive the real ``stop()`` and read the deadline it actually arms.

    The invariant above is only meaningful if ``gateway.run`` arms with the
    same expression. This measures the wall-clock delay passed at the
    production arming call site — not a call count, and not arithmetic
    re-derived in the test — so a refactor that re-inlines the old
    reserve-subtracted arming (``drain + grace`` with no launchd pull-back,
    which FleetReview read as leaving persistence ~5s) goes red here.
    """
    import asyncio

    from gateway import run as run_mod
    from gateway.restart import LAUNCHD_HARD_EXIT_RESERVE_S
    from tests.gateway.restart_test_helpers import make_restart_runner

    monkeypatch.setattr(run_mod, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    runner._restart_drain_timeout = 50.0
    runner._cron_drain_timeout = 0.01
    runner._launchd_exit_timeout_s = 60.0
    runner._stop_requested_by_signal = True
    runner._last_shutdown_teardown_s = 22.0
    adapter.disconnect = AsyncMock()

    armed: list[float] = []
    snapshots: list[dict] = []

    def _capture(delay, *, done_event=None, snapshot_fn=None, exit_code=None):
        armed.append(delay)
        if snapshot_fn is not None:
            snapshots.append(snapshot_fn())

    monkeypatch.setattr(run_mod, "arm_shutdown_watchdog", _capture)
    # stop() skips arming under pytest so unit tests don't inherit a
    # delayed hard-exit; clear the marker so the real arming path runs.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    with patch("gateway.status.remove_pid_file"), \
         patch("gateway.status.write_runtime_status"):
        asyncio.run(runner.stop())

    # drain = 60 - 10 (hard exit) - 22 (measured teardown) = 28
    assert runner._effective_stop_drain_timeout() == 28.0
    assert armed == [60.0 - LAUNCHD_HARD_EXIT_RESERVE_S]
    # The full measured teardown fits between the drain and the hard exit.
    assert armed[0] - 28.0 >= 22.0
    # The diagnostic snapshot reports the same deadline it armed with.
    assert snapshots and snapshots[0]["watchdog_delay_s"] == armed[0]


def test_stop_spends_pre_drain_elapsed_out_of_the_drain(monkeypatch, tmp_path):
    """The drain the real ``stop()`` starts must be elapsed-adjusted.

    The watchdog is armed at the top of the stop with an ABSOLUTE deadline
    (50s at the production clamp), but the drain is a RELATIVE budget that
    only begins after the pre-drain phases (reconnect cancel, per-session
    notify sends, boot-resume cancel, resume_pending marking). Without
    subtracting that elapsed time the teardown reserve between the drain
    and ``os._exit`` pays for it.

    This drives the real ``stop()`` and asserts the budget handed to
    ``_drain_active_agents`` is the elapsed-adjusted value — including
    that the adjustment is called with the production arguments and a live
    elapsed reading. The arithmetic itself is pinned separately by
    ``test_pre_drain_elapsed_comes_out_of_the_drain_not_the_reserve``.

    The elapsed must be read at the POINT OF USE, so the pre-drain
    ``resume_pending`` marking loop falls INSIDE it. A snapshot taken
    before that loop charges the loop's wall time to neither the drain
    nor the teardown reserve: at the live geometry (clamp 60, drain 30,
    armed 50) an 8s marking loop left a 12s window for a 15s reserve —
    ``os._exit`` inside persistence, the 09-21 incident's failure mode.
    The marking loop below therefore burns a measurable amount of time
    and the assertion requires the elapsed to account for it, so moving
    the reading back above the loop (or pinning ``elapsed_s=0.0``) is red.
    """
    import asyncio
    import time

    from gateway import run as run_mod
    from tests.gateway.restart_test_helpers import make_restart_runner

    monkeypatch.setattr(run_mod, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    runner._restart_drain_timeout = 50.0
    runner._cron_drain_timeout = 0.01
    runner._launchd_exit_timeout_s = 60.0
    runner._stop_requested_by_signal = True
    runner._last_shutdown_teardown_s = 22.0
    adapter.disconnect = AsyncMock()

    # Give the pre-drain marking loop real sessions AND a measurable cost,
    # so the elapsed reading can be distinguished from a pre-loop snapshot.
    MARK_COST_S = 0.05
    N_SESSIONS = 4
    MARKING_LOOP_S = MARK_COST_S * N_SESSIONS

    class _StubAgent:
        pass

    for _i in range(N_SESSIONS):
        runner._running_agents[f"drain-sess-{_i}"] = _StubAgent()

    def _slow_mark(_self, _session_key, **_kw):
        time.sleep(MARK_COST_S)
        return (True, "shutdown", False)

    monkeypatch.setattr(
        run_mod.GatewayRunner,
        "_mark_resume_pending_for_shutdown",
        _slow_mark,
    )

    calls: list[dict] = []
    SENTINEL = 16.0

    def _spy(
        drain_timeout,
        launchd_exit_timeout_s,
        *,
        signal_driven,
        elapsed_s,
        last_teardown_s=None,
        armed_deadline_s=None,
    ):
        calls.append(
            {
                "drain": drain_timeout,
                "clamp": launchd_exit_timeout_s,
                "signal_driven": signal_driven,
                "elapsed": elapsed_s,
                "last_teardown_s": last_teardown_s,
                "armed_deadline_s": armed_deadline_s,
            }
        )
        return SENTINEL

    monkeypatch.setattr(run_mod, "resolve_elapsed_adjusted_drain", _spy)

    drain_budgets: list[float] = []

    async def _fake_drain(_self, timeout, cron_timeout=None):
        drain_budgets.append(timeout)
        return ({}, False)

    monkeypatch.setattr(
        run_mod.GatewayRunner, "_drain_active_agents", _fake_drain
    )

    with patch("gateway.status.remove_pid_file"), \
         patch("gateway.status.write_runtime_status"):
        asyncio.run(runner.stop())

    # The stop consulted the adjustment exactly once, with the live
    # launchd clamp, the signal-driven flag, and a real elapsed reading.
    assert len(calls) == 1, f"expected one adjustment, got {calls}"
    call = calls[0]
    # Unadjusted the drain is 28.0 (60 - 10 hard exit - 22 measured).
    assert call["drain"] == 28.0
    assert call["clamp"] == 60.0
    assert call["signal_driven"] is True
    # The elapsed must be read at the POINT OF USE, which puts the
    # pre-drain marking loop inside it. `>= 0.0` would pass for any
    # snapshot position — including one taken before the loop, which
    # charges the loop to neither the drain nor the teardown reserve.
    # Requiring the marking loop's own cost is what distinguishes a
    # working guard from a disabled one.
    assert isinstance(call["elapsed"], float)
    assert call["elapsed"] >= MARKING_LOOP_S, (
        f"elapsed {call['elapsed']:.3f}s does not account for the "
        f"{MARKING_LOOP_S:.3f}s pre-drain resume_pending marking loop; the "
        f"reading is being taken before the loop, so its wall time is "
        f"charged to neither the drain nor the teardown reserve and the "
        f"window before os._exit silently shrinks below the reserve"
    )

    # The measured teardown must reach the resolver too. The cap already
    # sized this drain against the runner's 22s sample; if the resolver is
    # not given the same sample it fits the drain against the default
    # 15s-reserve deadline (35s) instead of the real one (28s) and the drain
    # overruns the window the watchdog actually granted.
    assert call["last_teardown_s"] == 22.0, (
        f"last_teardown_s={call['last_teardown_s']!r} — the resolver is "
        f"deriving the deadline from the DEFAULT cleanup reserve while the "
        f"cap and the watchdog use the measured 22.0s sample; the two "
        f"derivations of the one deadline disagree and the drain runs past "
        f"the window before os._exit"
    )

    # ...and the drain it actually ran is the adjusted value, not the raw
    # budget. This is what goes red if the wiring is removed.
    assert drain_budgets == [SENTINEL], (
        f"drain budget {drain_budgets} is not the elapsed-adjusted value; "
        f"the teardown reserve pays for the pre-drain phases instead"
    )


def test_stop_arms_the_watchdog_with_the_measured_teardown_reserve(
    monkeypatch, tmp_path
):
    """The arming call site must hand the watchdog the measured reserve.

    ``resolve_armed_shutdown_watchdog_delay`` widens its inner leash to
    ``max(grace, reserve)`` so a teardown longer than the 60s grace is still
    honoured. That widening is dead unless ``gateway.run`` actually passes
    ``last_teardown_s`` — the resolver defaults it to ``None`` and silently
    collapses back to the bare grace.

    Measured on the unwired call site at clamp 300 with a recorded 70s
    teardown: drain 180, armed 240 — a 60s window for a teardown known to
    take 70s, so ``os._exit`` lands mid-persistence. This drives the real
    ``stop()`` and reads the wall-clock delay handed to
    ``arm_shutdown_watchdog``, so dropping the argument goes red here
    rather than silently reverting to the grace.
    """
    import asyncio

    from gateway import run as run_mod
    from tests.gateway.restart_test_helpers import make_restart_runner

    monkeypatch.setattr(run_mod, "_hermes_home", tmp_path)

    # A clamp high enough that the inner leash binds (system-domain launchd
    # is not gui-clamped to 60), with a teardown sample above the grace.
    runner, adapter = make_restart_runner()
    runner._restart_drain_timeout = 180.0
    runner._cron_drain_timeout = 0.01
    runner._launchd_exit_timeout_s = 300.0
    runner._stop_requested_by_signal = True
    runner._last_shutdown_teardown_s = 70.0
    adapter.disconnect = AsyncMock()

    armed: list[float] = []
    snapshots: list[dict] = []

    def _capture(delay, *, done_event=None, snapshot_fn=None, exit_code=None):
        armed.append(delay)
        if snapshot_fn is not None:
            snapshots.append(snapshot_fn())

    monkeypatch.setattr(run_mod, "arm_shutdown_watchdog", _capture)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    with patch("gateway.status.remove_pid_file"), \
         patch("gateway.status.write_runtime_status"):
        asyncio.run(runner.stop())

    drain = runner._effective_stop_drain_timeout()
    assert drain == 180.0  # configured drain fits inside the wide clamp
    assert armed, "stop() never armed the shutdown watchdog"

    # The measured teardown must fit between the end of the drain and the
    # hard exit. With the bare grace this window is 60.0 for a 70s reserve.
    window = armed[0] - drain
    assert window >= 70.0, (
        f"armed={armed[0]} drain={drain}: only {window}s before os._exit "
        f"for a measured 70.0s teardown — the reserve is not wired into "
        f"the arming call site"
    )
    # Still strictly inside launchd's SIGKILL wall.
    assert armed[0] < 300.0
    # The diagnostic snapshot reports the same deadline it armed with.
    assert snapshots and snapshots[0]["watchdog_delay_s"] == armed[0]


def test_stop_rearms_the_watchdog_with_REMAINING_time_not_the_absolute_deadline(
    monkeypatch, tmp_path
):
    """The re-arm must convert the absolute deadline to a relative delay.

    ``_armed_shutdown_deadline_s`` and everything that consumes it (the
    drain fit, the cron leash) are ABSOLUTE, measured from the start of
    ``stop()``. ``arm_shutdown_watchdog`` is RELATIVE: it computes
    ``deadline = time.monotonic() + delay``. At the top-of-stop() arming
    (t=0) the two coincide, which is exactly why handing the absolute value
    straight into a LATER re-arm is an easy and invisible mistake — it
    charges the pre-drain elapsed a second time and schedules ``os._exit``
    at ``elapsed + deadline``, past launchd's SIGKILL whenever the elapsed
    exceeds the 10s hard-exit reserve. The backstop then never runs: no
    forensic dump, no ordered lock/PID release, just an uncatchable SIGKILL
    mid-teardown — the failure class this whole change closes.

    This drives the REAL ``stop()`` with ``PYTEST_CURRENT_TEST`` cleared
    (the re-arm returns early otherwise, which is why the pure-resolver
    tests could not see this) and a genuine pre-drain cost, then reads both
    values handed to ``arm_shutdown_watchdog``. The second must be the
    REMAINING time, not the published absolute deadline.
    """
    import asyncio

    from gateway import run as run_mod
    from tests.gateway.restart_test_helpers import make_restart_runner

    monkeypatch.setattr(run_mod, "_hermes_home", tmp_path)

    # Clamp 300: system-domain launchd is not gui-clamped, and the inner
    # leash binds here, so a re-arm has room to extend.
    runner, adapter = make_restart_runner()
    runner._restart_drain_timeout = 180.0
    runner._cron_drain_timeout = 0.01
    runner._launchd_exit_timeout_s = 300.0
    runner._stop_requested_by_signal = True
    runner._last_shutdown_teardown_s = 70.0
    adapter.disconnect = AsyncMock()

    # A real pre-drain cost, comfortably past the 0.5s re-arm hysteresis.
    PRE_DRAIN_S = 0.9

    async def _slow_notify():
        await asyncio.sleep(PRE_DRAIN_S)

    monkeypatch.setattr(
        runner, "_notify_active_sessions_of_shutdown", _slow_notify
    )

    armed: list[float] = []

    def _capture(delay, *, done_event=None, snapshot_fn=None, exit_code=None):
        armed.append(delay)
        return done_event if done_event is not None else threading.Event()

    monkeypatch.setattr(run_mod, "arm_shutdown_watchdog", _capture)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    with patch("gateway.status.remove_pid_file"), \
         patch("gateway.status.write_runtime_status"):
        asyncio.run(runner.stop())

    assert len(armed) == 2, (
        f"expected the top-of-stop() arming plus one re-arm, got {armed} — "
        f"if this is 1 the re-arm never fired and finding 4 is unwired"
    )
    first, second = armed

    # Hand-computed, independent of the resolvers: capped drain 180, leash
    # max(grace 60, reserve 70) = 70, so the t=0 arming is 250 and the
    # re-arm's absolute deadline is min(elapsed + 250, 290).
    assert first == pytest.approx(250.0), (
        f"top-of-stop() arming {first} is not the inner-leash value 250 — "
        f"the geometry no longer exercises the re-arm"
    )
    published = runner._armed_shutdown_deadline_s
    assert published == pytest.approx(250.0 + PRE_DRAIN_S, abs=0.5), (
        f"published absolute deadline {published} should be the t=0 value "
        f"extended by the measured pre-drain cost"
    )

    # THE ASSERTION: what was ARMED is the remaining time, which is ~250 —
    # the absolute deadline MINUS the elapsed already spent. Passing the
    # absolute value through would arm ~250.9 and fire at ~251.8 from the
    # stop start, i.e. the elapsed counted twice.
    assert second == pytest.approx(250.0, abs=0.35), (
        f"re-arm handed arm_shutdown_watchdog {second}; the absolute "
        f"deadline is {published} and ~{PRE_DRAIN_S}s is already spent, so "
        f"the RELATIVE delay must be ~250. Arming the absolute value "
        f"double-counts the elapsed and pushes os._exit past the wall"
    )
    assert second < published, (
        "the armed delay must be strictly less than the absolute deadline "
        "once any time has been spent"
    )

    # And the firing instant, measured from the stop start, never reaches
    # launchd's SIGKILL wall.
    fires_at = PRE_DRAIN_S + second
    assert fires_at <= 300.0 - 10.0 + 0.5, (
        f"os._exit scheduled at stop()+{fires_at}s against a SIGKILL at 300"
    )


def test_stop_threads_the_one_deadline_into_both_the_drain_and_the_cron_leash(
    monkeypatch, tmp_path
):
    """The call-site wiring, not the pure resolvers.

    ``resolve_stop_drain_deadline_s`` being correct proves nothing if
    ``gateway.run`` does not hand its result to BOTH consumers. Under
    ``PYTEST_CURRENT_TEST`` the arming site returns early, so
    ``_armed_shutdown_deadline_s`` stays ``None`` and every existing spy
    sees ``armed_deadline_s=None`` / ``deadline_s=None`` — the wiring is
    structurally invisible to them. This clears the marker so the real
    values flow, then asserts both consumers received the SAME deadline the
    watchdog was armed from.

    Geometry (clamp 60, production): capped drain 28 (60 - 10 hard exit -
    22 measured), armed = min(28 + max(60, 22), 50) = 50 (outer min binds),
    deadline = 50 - 22 = 28. All hand-computed.
    """
    import asyncio

    from gateway import run as run_mod
    from tests.gateway.restart_test_helpers import make_restart_runner

    monkeypatch.setattr(run_mod, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    runner._restart_drain_timeout = 50.0
    runner._cron_drain_timeout = 30.0
    runner._launchd_exit_timeout_s = 60.0
    runner._stop_requested_by_signal = True
    runner._last_shutdown_teardown_s = 22.0
    adapter.disconnect = AsyncMock()

    # In-flight cron work, so the cron leash is actually exercised.
    monkeypatch.setattr(
        run_mod.GatewayRunner, "_active_cron_job_count", lambda _self: 1
    )

    drain_kwargs: list[dict] = []
    real_drain_resolver = run_mod.resolve_elapsed_adjusted_drain

    def _drain_spy(*a, **kw):
        drain_kwargs.append(dict(kw))
        return real_drain_resolver(*a, **kw)

    cron_kwargs: list[dict] = []
    real_cron_resolver = run_mod.resolve_cron_drain_budget

    def _cron_spy(*a, **kw):
        cron_kwargs.append(dict(kw))
        return real_cron_resolver(*a, **kw)

    monkeypatch.setattr(run_mod, "resolve_elapsed_adjusted_drain", _drain_spy)
    monkeypatch.setattr(run_mod, "resolve_cron_drain_budget", _cron_spy)
    monkeypatch.setattr(
        run_mod,
        "arm_shutdown_watchdog",
        lambda delay, **kw: kw.get("done_event") or threading.Event(),
    )

    async def _fake_drain(_self, timeout, cron_timeout=None):
        return ({}, False)

    monkeypatch.setattr(
        run_mod.GatewayRunner, "_drain_active_agents", _fake_drain
    )
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    with patch("gateway.status.remove_pid_file"), \
         patch("gateway.status.write_runtime_status"):
        asyncio.run(runner.stop())

    assert len(drain_kwargs) == 1, f"expected one drain fit, got {drain_kwargs}"
    assert len(cron_kwargs) == 1, f"expected one cron leash, got {cron_kwargs}"

    armed = drain_kwargs[0]["armed_deadline_s"]
    assert armed is not None, (
        "the drain fit received armed_deadline_s=None — it is re-deriving "
        "the deadline instead of consuming the value the watchdog was "
        "armed with, which is the #838 defect"
    )
    # Hand-computed: the outer min() binds at the production clamp.
    assert armed == pytest.approx(50.0), (
        f"armed deadline {armed} is not the hand-computed 50.0 "
        f"(min(28 + max(60, 22), 60 - 10))"
    )
    assert armed == pytest.approx(runner._armed_shutdown_deadline_s)

    deadline = cron_kwargs[0]["deadline_s"]
    assert deadline is not None, (
        "the cron leash received deadline_s=None — it is re-deriving from "
        "the raw SIGKILL wall, which is finding 2 of the #838 review"
    )
    # armed 50 minus the measured 22s teardown reserve.
    assert deadline == pytest.approx(28.0), (
        f"cron deadline {deadline} is not armed(50) - reserve(22) = 28"
    )
    # Both consumers saw ONE deadline, derived from the same armed value.
    assert deadline == pytest.approx(armed - 22.0)


def test_drain_consumes_the_REARMED_deadline_at_the_inner_leash_geometry(
    monkeypatch, tmp_path
):
    """The consumed deadline must differ from what re-derivation would give.

    The sibling call-site test above runs at the production clamp 60, where
    the OUTER ``min()`` of the arming expression binds. There, consuming
    ``_armed_shutdown_deadline_s`` and re-deriving it from ``exit_timeout -
    hard_exit_reserve_s`` produce the SAME number (28), so that test passes
    just as happily with ``armed_deadline_s=None`` hard-wired at the call
    site — the exact re-derivation that is finding 1 of the #838 review.
    A gate that only measures the geometry where the two agree cannot
    detect the defect it is named for; this is the #838 root pattern
    reproduced one level up, in the test suite.

    This pins the geometry where they diverge: clamp 300 (system-domain
    launchd is not gui-clamped), configured 180, measured teardown 70, plus
    a real pre-drain cost so the re-arm actually extends the deadline.

    Hand-computed, independent of every resolver::

        capped drain    = 180                       (fits under the wall)
        armed at t=0    = min(180 + max(60, 70), 290)          = 250
        re-armed        = min(0.9 + 180 + 70, 290)             = 250.9
        deadline CONSUMED  = 250.9 - 70                        = 180.9
        deadline REDERIVED = 250   - 70                        = 180

    Re-derivation silently discards the pre-drain cost the re-arm just
    bought back and hands it to the teardown reserve instead.
    """
    import asyncio

    from gateway import run as run_mod
    from tests.gateway.restart_test_helpers import make_restart_runner

    monkeypatch.setattr(run_mod, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    runner._restart_drain_timeout = 180.0
    runner._cron_drain_timeout = 30.0
    runner._launchd_exit_timeout_s = 300.0
    runner._stop_requested_by_signal = True
    runner._last_shutdown_teardown_s = 70.0
    adapter.disconnect = AsyncMock()

    # A real pre-drain cost, past the 0.5s re-arm hysteresis.
    PRE_DRAIN_S = 0.9

    async def _slow_notify():
        await asyncio.sleep(PRE_DRAIN_S)

    monkeypatch.setattr(runner, "_notify_active_sessions_of_shutdown", _slow_notify)

    # In-flight cron work, so the cron leash is exercised too.
    monkeypatch.setattr(
        run_mod.GatewayRunner, "_active_cron_job_count", lambda _self: 1
    )

    drain_kwargs: list[dict] = []
    real_drain_resolver = run_mod.resolve_elapsed_adjusted_drain

    def _drain_spy(*a, **kw):
        drain_kwargs.append(dict(kw))
        return real_drain_resolver(*a, **kw)

    cron_kwargs: list[dict] = []
    real_cron_resolver = run_mod.resolve_cron_drain_budget

    def _cron_spy(*a, **kw):
        cron_kwargs.append(dict(kw))
        return real_cron_resolver(*a, **kw)

    monkeypatch.setattr(run_mod, "resolve_elapsed_adjusted_drain", _drain_spy)
    monkeypatch.setattr(run_mod, "resolve_cron_drain_budget", _cron_spy)
    monkeypatch.setattr(
        run_mod,
        "arm_shutdown_watchdog",
        lambda delay, **kw: kw.get("done_event") or threading.Event(),
    )

    async def _fake_drain(_self, timeout, cron_timeout=None):
        return ({}, False)

    monkeypatch.setattr(run_mod.GatewayRunner, "_drain_active_agents", _fake_drain)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    with patch("gateway.status.remove_pid_file"), \
         patch("gateway.status.write_runtime_status"):
        asyncio.run(runner.stop())

    assert len(drain_kwargs) == 1, f"expected one drain fit, got {drain_kwargs}"
    assert len(cron_kwargs) == 1, f"expected one cron leash, got {cron_kwargs}"

    armed = drain_kwargs[0]["armed_deadline_s"]
    assert armed is not None, (
        "the drain fit received armed_deadline_s=None — it is re-deriving "
        "the deadline instead of consuming the value the watchdog was "
        "armed with (finding 1 of the #838 review)"
    )
    # The RE-ARMED value, i.e. the t=0 250 extended by the measured cost.
    assert armed == pytest.approx(250.0 + PRE_DRAIN_S, abs=0.5), (
        f"armed deadline {armed} is not the re-armed 250.9 — the drain is "
        f"consuming a stale or re-derived deadline, not the one os._exit "
        f"actually fires at"
    )
    # 🔴 THE DISCRIMINATING ASSERTION: re-derivation from the hard-exit wall
    # gives 250 here, not 250.9. Hard-wiring armed_deadline_s=None at the
    # call site passes the clamp-60 sibling test and fails this one.
    assert armed > 250.0 + 0.25, (
        f"armed deadline {armed} equals the t=0 arming (250) — the pre-drain "
        f"elapsed the re-arm bought back was discarded, which is exactly "
        f"what a re-derived deadline yields at this geometry"
    )

    deadline = cron_kwargs[0]["deadline_s"]
    assert deadline is not None, (
        "the cron leash received deadline_s=None — it is re-deriving from "
        "the raw SIGKILL wall (finding 2 of the #838 review)"
    )
    # armed minus the measured 70s teardown reserve, hand-computed.
    assert deadline == pytest.approx(180.0 + PRE_DRAIN_S, abs=0.5), (
        f"cron deadline {deadline} is not armed({armed}) - reserve(70)"
    )
    assert deadline > 180.0 + 0.25, (
        f"cron deadline {deadline} collapsed to the re-derived 180 — the "
        f"elapsed adjustment was silently undone at the cron call site"
    )
    # ONE deadline: both consumers, same armed value, same reserve.
    assert deadline == pytest.approx(armed - 70.0, abs=0.01)

    # And the deadline still sits a full teardown reserve inside the wall.
    assert armed <= 300.0 - 10.0, (
        f"armed deadline {armed} reaches past exit_timeout - hard_exit_reserve"
    )


def test_hard_exit_backstop_ignores_blocked_daemon_threads(monkeypatch, tmp_path):
    from gateway import shutdown_watchdog

    release = threading.Event()
    blockers = [
        threading.Thread(target=release.wait, daemon=True, name=f"blocked-{i}")
        for i in range(25)
    ]
    for thread in blockers:
        thread.start()

    exited = threading.Event()
    codes: list[int] = []

    def fake_exit(code):
        codes.append(code)
        exited.set()

    monkeypatch.setattr(shutdown_watchdog.os, "_exit", fake_exit)
    arm_shutdown_watchdog(
        0.05,
        exit_code=1,
        dump_path=tmp_path / "watchdog.log",
    )
    try:
        assert exited.wait(1.0), "hard exit was blocked by unrelated daemon threads"
        assert codes == [1]
    finally:
        release.set()
        for thread in blockers:
            thread.join(timeout=1.0)


def test_stop_consumes_the_signal_driven_flag_for_its_drain(monkeypatch, tmp_path):
    """The stop path must honour the flag the signal handler sets.

    Both halves of the cap have to exist: the signal handler sets
    ``_stop_requested_by_signal``, and the stop path must read it to decide
    whether launchd's clamp applies. This drives the real ``stop()`` twice
    with the flag off and on and reads the drain budget actually handed to
    ``_drain_active_agents`` — behaviour, not source text, so a refactor
    that moves or renames the wiring still passes while one that DROPS the
    consumer goes red.
    """
    import asyncio

    from gateway import run as run_mod
    from tests.gateway.restart_test_helpers import make_restart_runner

    monkeypatch.setattr(run_mod, "_hermes_home", tmp_path)

    def _run(signal_driven: bool) -> float:
        runner, adapter = make_restart_runner()
        runner._restart_drain_timeout = 50.0
        runner._cron_drain_timeout = 0.01
        runner._launchd_exit_timeout_s = 60.0
        runner._stop_requested_by_signal = signal_driven
        runner._last_shutdown_teardown_s = 22.0
        adapter.disconnect = AsyncMock()

        budgets: list[float] = []

        async def _fake_drain(_self, timeout, cron_timeout=None):
            budgets.append(timeout)
            return ({}, False)

        monkeypatch.setattr(
            run_mod.GatewayRunner, "_drain_active_agents", _fake_drain
        )
        with patch("gateway.status.remove_pid_file"), \
             patch("gateway.status.write_runtime_status"):
            asyncio.run(runner.stop())
        assert budgets, "stop() never drained"
        return budgets[0]

    # Not signal-driven: launchd's clamp does not apply, the configured
    # drain survives whole (modulo the pre-drain elapsed adjustment).
    unsupervised = _run(False)
    assert unsupervised > 28.0, (
        f"drain {unsupervised} was capped on a non-signal stop; the stop "
        f"path is applying the launchd clamp unconditionally"
    )

    # Signal-driven: capped to 60 - 10 (hard exit) - 22 (measured teardown).
    supervised = _run(True)
    assert supervised <= 28.0, (
        f"drain {supervised} exceeds the launchd cap of 28.0; the stop path "
        f"is not consuming _stop_requested_by_signal"
    )


# --- Round 3: the four P1s FleetReview found on 384be7fd ---------------------


def test_stale_unclamped_armed_deadline_cannot_fit_the_drain_past_the_wall():
    """F1: a caller-supplied armed deadline is bounded by the hard-exit wall.

    ``_armed_shutdown_deadline_s`` is published by the top-of-``stop()``
    arming, which passes ``signal_driven=self._stop_requested_by_signal``.
    An in-band restart (``stop(restart=True)``) runs with that False, so
    ``resolve_launchd_shutdown_watchdog_delay`` short-circuits and the
    published value is the RAW inner leash — no wall clamp. If a supervisor
    SIGTERM lands while that stop is draining, the handler flips the flag
    True and the drain/cron reads see ``signal_driven=True`` together with
    that stale unclamped value. The extend-only re-arm cannot rescue it
    either: the correctly-clamped fresh value is EARLIER, so it fails the
    extend-only guard and the stale one stays in force.

    Expected values hand-computed, independent of the function under test::

        stale published (in-band, clamp 60) = 180 + max(60, 15)   = 240
        hard-exit wall                      = 60 - 10             =  50
        deadline, clamped   = 50  - 15                            =  35
        deadline, unclamped = 240 - 15                            = 225   <- BUG

    225 against an uncatchable SIGKILL at 60 fits the drain ~190s past the
    wall: killed mid-drain, no persist, no teardown, no `.clean_shutdown`.
    """
    from gateway.restart import resolve_stop_drain_deadline_s

    STALE_UNCLAMPED = 240.0  # what the in-band arming publishes at clamp 60

    deadline = resolve_stop_drain_deadline_s(
        180.0,
        60.0,
        signal_driven=True,
        armed_deadline_s=STALE_UNCLAMPED,
    )
    assert deadline == pytest.approx(35.0), (
        f"deadline {deadline} was taken from the stale unclamped armed "
        f"value ({STALE_UNCLAMPED}); expected the wall-clamped 35 "
        f"(hard exit 50 - reserve 15). A deadline past the wall fits the "
        f"drain past launchd's uncatchable SIGKILL"
    )
    # Explicitly NOT the unclamped answer, so this cannot pass by accident.
    assert deadline != pytest.approx(225.0)
    # And never past the wall for ANY caller-supplied value.
    for absurd in (240.0, 1_000.0, 86_400.0):
        assert (
            resolve_stop_drain_deadline_s(
                180.0, 60.0, signal_driven=True, armed_deadline_s=absurd
            )
            <= 50.0 - 15.0
        ), f"armed_deadline_s={absurd} escaped the hard-exit wall"


def test_wall_clamp_is_inert_when_the_armed_deadline_is_already_inside():
    """F1 control: the clamp must not shorten any legitimate deadline.

    Hand-computed at both geometries the rest of this file pins:

        clamp  60 / drain  28 / teardown 22: armed  50.0 -> deadline  28.0
        clamp 300 / drain 180 / teardown 70: armed 250.9 -> deadline 180.9
    """
    from gateway.restart import resolve_stop_drain_deadline_s

    assert resolve_stop_drain_deadline_s(
        28.0, 60.0, signal_driven=True, last_teardown_s=22.0, armed_deadline_s=50.0
    ) == pytest.approx(28.0)
    # The re-armed inner-leash value must survive the clamp untouched.
    assert resolve_stop_drain_deadline_s(
        180.0,
        300.0,
        signal_driven=True,
        last_teardown_s=70.0,
        armed_deadline_s=250.9,
    ) == pytest.approx(180.9)


def test_rearm_is_skipped_off_the_launchd_path_where_nothing_bounds_it(
    monkeypatch, tmp_path
):
    """F4: no live ExitTimeOut => no wall => no cap, and no benefit.

    ``resolve_launchd_shutdown_watchdog_delay`` short-circuits without a
    launchd budget, so the re-arm's "still bounded by the SIGKILL wall"
    invariant is simply false on systemd / Docker-s6 / --external-supervisor
    / foreground: the new deadline is an uncapped
    ``elapsed + drain + max(grace, reserve)`` and every SIGTERM that spends
    >0.5s pre-drain pushes ``os._exit`` later, bounded by nothing.

    There is nothing to buy back there either —
    ``resolve_stop_drain_deadline_s`` returns None without a budget, so the
    drain is never elapsed-charged and finding 4's defect cannot arise.
    Cost without correctness, so the re-arm must not fire at all.
    """
    import asyncio

    from gateway import run as run_mod
    from tests.gateway.restart_test_helpers import make_restart_runner

    monkeypatch.setattr(run_mod, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    runner._restart_drain_timeout = 180.0
    runner._cron_drain_timeout = 0.01
    runner._launchd_exit_timeout_s = None  # <- systemd / docker / foreground
    runner._stop_requested_by_signal = True
    adapter.disconnect = AsyncMock()

    async def _slow_notify():
        await asyncio.sleep(0.9)  # real pre-drain cost, past the hysteresis

    monkeypatch.setattr(runner, "_notify_active_sessions_of_shutdown", _slow_notify)

    armed: list[float] = []

    def _capture(delay, *, done_event=None, snapshot_fn=None, exit_code=None):
        armed.append(delay)
        return done_event if done_event is not None else threading.Event()

    monkeypatch.setattr(run_mod, "arm_shutdown_watchdog", _capture)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    with patch("gateway.status.remove_pid_file"), \
         patch("gateway.status.write_runtime_status"):
        asyncio.run(runner.stop())

    assert len(armed) == 1, (
        f"expected ONLY the top-of-stop() arming off the launchd path, got "
        f"{armed} — a re-arm fired where no wall bounds it and where the "
        f"drain is never elapsed-charged, so it can only push os._exit out"
    )
    # Hand-computed: drain 180 + max(grace 60, reserve 15) = 240, uncapped.
    assert armed[0] == pytest.approx(240.0)


def test_rearm_failure_leaves_the_original_watchdog_armed_and_stop_running(
    monkeypatch, tmp_path
):
    """F2: a failing re-arm must not abort stop() or disarm the backstop.

    If ``arm_shutdown_watchdog`` raises, propagating unwinds
    ``_stop_impl_body`` at the drain fit — before drain, persist and
    teardown — and the outer ``finally`` then sets every registered event,
    retiring the ORIGINAL watchdog too. Result: in-flight state unpersisted
    AND nothing left to ``os._exit`` before launchd's SIGKILL.

    Drives the real ``stop()`` with the marker cleared and the SECOND arming
    call raising, then asserts stop() ran to completion (the adapter was
    disconnected, which happens in post-drain teardown) and the published
    deadline was NOT advanced to a backstop that does not exist.
    """
    import asyncio

    from gateway import run as run_mod
    from tests.gateway.restart_test_helpers import make_restart_runner

    monkeypatch.setattr(run_mod, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    runner._restart_drain_timeout = 180.0
    runner._cron_drain_timeout = 0.01
    runner._launchd_exit_timeout_s = 300.0
    runner._stop_requested_by_signal = True
    runner._last_shutdown_teardown_s = 70.0
    adapter.disconnect = AsyncMock()

    async def _slow_notify():
        await asyncio.sleep(0.9)

    monkeypatch.setattr(runner, "_notify_active_sessions_of_shutdown", _slow_notify)

    calls: list[float] = []
    first_event: list[threading.Event] = []

    def _capture(delay, *, done_event=None, snapshot_fn=None, exit_code=None):
        calls.append(delay)
        if len(calls) == 1:
            ev = done_event if done_event is not None else threading.Event()
            first_event.append(ev)
            return ev
        raise RuntimeError("can't start new thread")  # the re-arm fails

    monkeypatch.setattr(run_mod, "arm_shutdown_watchdog", _capture)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    with patch("gateway.status.remove_pid_file"), \
         patch("gateway.status.write_runtime_status"):
        asyncio.run(runner.stop())  # must NOT raise

    assert len(calls) == 2, f"expected the arming plus a failing re-arm, got {calls}"
    # stop() reached post-drain teardown rather than unwinding at the fit.
    adapter.disconnect.assert_awaited()
    # The published deadline still names the backstop that actually exists.
    assert runner._armed_shutdown_deadline_s == pytest.approx(250.0), (
        f"published deadline {runner._armed_shutdown_deadline_s} was advanced "
        f"to a watchdog that failed to arm; the live backstop is still the "
        f"t=0 one at 250"
    )
