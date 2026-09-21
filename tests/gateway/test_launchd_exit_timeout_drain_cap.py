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
    armed = resolve_armed_shutdown_watchdog_delay(drain, clamp, signal_driven=True)
    promised_reserve = max(
        LAUNCHD_STOP_CLEANUP_RESERVE_S, effective_teardown or 0.0
    )
    assert armed - drain >= promised_reserve, (
        f"clamp={clamp} drain={drain} watchdog={armed}: only "
        f"{armed - drain}s before hard exit for a {promised_reserve}s teardown"
    )
    # The reserve is carved out of the drain, never borrowed past the wall.
    assert drain + promised_reserve <= clamp


def test_teardown_ceiling_is_bounded_by_the_watchdog_grace():
    """A sample above the grace can never be honoured, so reject it.

    The armed deadline is ``min(drain + grace, hard_exit)``. When the inner
    leash binds, the post-drain window is ``grace`` no matter how far the
    drain shrinks — ``remaining()`` is maximized at drain 0 and still only
    equals the grace. Bounding the ceiling by the span to the hard exit
    alone therefore advertised an unhonourable reserve on any clamp above
    ~70 (system-domain launchd is not gui-clamped to 60).
    """
    from gateway.restart import (
        LAUNCHD_HARD_EXIT_RESERVE_S,
        resolve_armed_shutdown_watchdog_delay,
        resolve_max_actionable_teardown_reserve_s,
    )
    from gateway.shutdown_watchdog import (
        DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S as GRACE,
    )

    # Low clamp: the hard-exit span is the binding bound.
    assert resolve_max_actionable_teardown_reserve_s(60.0) == (
        60.0 - LAUNCHD_HARD_EXIT_RESERVE_S
    )
    # High clamp: the grace is the binding bound, NOT clamp - 10 (= 290).
    assert resolve_max_actionable_teardown_reserve_s(300.0) == GRACE

    # The ceiling never advertises more than the window that can exist.
    for clamp in (30.0, 60.0, 120.0, 300.0, 600.0):
        ceiling = resolve_max_actionable_teardown_reserve_s(clamp)
        assert ceiling is not None
        drain = resolve_launchd_capped_drain(
            600.0, clamp, last_teardown_s=ceiling - 0.5
        )
        armed = resolve_armed_shutdown_watchdog_delay(
            drain, clamp, signal_driven=True
        )
        assert armed - drain >= ceiling - 0.5, (
            f"clamp={clamp}: ceiling {ceiling} promises more than the "
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
    assert (
        resolve_elapsed_adjusted_drain(
            drain, 60.0, signal_driven=True, elapsed_s=elapsed
        )
        == expected
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
    """
    import asyncio

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

    calls: list[dict] = []
    SENTINEL = 16.0

    def _spy(drain_timeout, launchd_exit_timeout_s, *, signal_driven, elapsed_s):
        calls.append(
            {
                "drain": drain_timeout,
                "clamp": launchd_exit_timeout_s,
                "signal_driven": signal_driven,
                "elapsed": elapsed_s,
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
    assert isinstance(call["elapsed"], float) and call["elapsed"] >= 0.0

    # ...and the drain it actually ran is the adjusted value, not the raw
    # budget. This is what goes red if the wiring is removed.
    assert drain_budgets == [SENTINEL], (
        f"drain budget {drain_budgets} is not the elapsed-adjusted value; "
        f"the teardown reserve pays for the pre-drain phases instead"
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


def test_signal_handler_marks_stop_as_signal_driven():
    """SIGTERM/SIGINT handler flags the runner before scheduling stop().

    The handler is a closure inside the gateway main; this is a structural
    contract check that both halves of the cap exist — the producer in the
    signal handler and the consumer in the stop path — so neither can be
    dropped in a refactor without this test going red.
    """
    import inspect

    from gateway import run as run_mod

    src = inspect.getsource(run_mod)
    assert src.count("runner._stop_requested_by_signal = True") == 1
    assert "effective_stop_drain_timeout(self)" in inspect.getsource(
        run_mod.GatewayRunner.stop
    )
