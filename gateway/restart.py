"""Shared gateway restart constants and supervisor detection helpers."""

import os
import re
import subprocess
from collections.abc import Callable, Mapping

from hermes_cli.config import DEFAULT_CONFIG

# EX_TEMPFAIL from sysexits.h — used to ask the service manager to restart
# the gateway after a graceful drain/reload path completes.
GATEWAY_SERVICE_RESTART_EXIT_CODE = 75

# EX_CONFIG from sysexits.h — fatal configuration error (e.g. token
# collision, no messaging platforms).  The s6 finish script translates
# this into exit 125 (permanent failure) so the supervisor stops
# restarting the gateway.  See #51228.
GATEWAY_FATAL_CONFIG_EXIT_CODE = 78

# Set by ``hermes gateway run --external-supervisor``. Unlike systemd's
# INVOCATION_ID and launchd's XPC_SERVICE_NAME, this survives wrappers that
# intentionally replace the child environment (for example ``sudo env -i``).
EXTERNAL_GATEWAY_SUPERVISOR_ENV = "HERMES_GATEWAY_EXTERNAL_SUPERVISOR"

DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT = float(
    DEFAULT_CONFIG["agent"]["restart_drain_timeout"]
)

# In-band restart (``/restart``, SIGUSR1, self-restart from a child CLI)
# waits for active turns to finish *before* ``stop()`` begins. Distinct
# from ``restart_drain_timeout``, which is the force-interrupt budget
# once ``stop()`` is running (and must stay short under systemd
# TimeoutStopSec). See #77184.
DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT = float(
    DEFAULT_CONFIG["agent"]["restart_after_turn_timeout"]
)

# Cron-only floor under the ``stop()`` drain. ``restart_drain_timeout``
# defaults to 0 because interrupting a *chat* turn is cheap and recoverable:
# the user is told the gateway is restarting and the session is pre-marked
# resume_pending. An interrupted *cron* run has neither property — nobody is
# waiting on it, it lands in jobs.json as a permanent failure, and a recurring
# job just waits for its next schedule — so a zero-second drain silently
# destroys work. See #82161.
DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT = float(
    DEFAULT_CONFIG["agent"]["cron_drain_timeout"]
)

# Seconds of the shutdown watchdog leash held back for the work that still has
# to happen after the drain returns: interrupt agents, kill tool subprocesses,
# mark in-flight jobs interrupted, disconnect adapters. Waiting for cron past
# that point trades a job that is killed *and recorded* for one that is
# SIGKILLed mid-write and stays wedged at ``last_status=running`` forever.
CRON_DRAIN_CLEANUP_RESERVE_S = 10.0

# systemd TimeoutStopSec headroom after the stop-path drain budget, and the
# floor used when that budget is still the default immediate (0s) chat drain.
# Keep these in lockstep with generate_systemd_unit() / #94759.
SYSTEMD_STOP_HEADROOM_S = 30.0
SYSTEMD_TIMEOUT_STOP_SEC_FLOOR = 60.0

# launchd is the one supervisor whose stop budget the gateway cannot size:
# ``ExitTimeOut`` lives in the plist, and the per-user (gui) domain CLAMPS
# it — measured on macOS 26.6.1: plist 215 → live 60, 90 → 60, 60 → 60,
# 30 → 30. The gateway can only *read* the live value (``launchctl print
# gui/<uid>/<label>`` → ``exit timeout = N``) and fit its SIGTERM-driven
# stop inside it. Draining past it is not "a longer drain" — launchd
# SIGKILLs at N seconds, mid-SQLite-write on a busy restart, which is the
# unclean-exit half of the state.db corruption class (incident 2026-09-19:
# ``restart_drain_timeout: 180`` vs live 60 → SIGKILL at +60s on every
# busy restart).
LAUNCHD_GUI_EXIT_TIMEOUT_CLAMP_S = 60
LAUNCHD_STOP_CLEANUP_RESERVE_S = 15.0
LAUNCHD_HARD_EXIT_RESERVE_S = 10.0

# Fallback reserve for a live ``ExitTimeOut`` too small to afford the fixed
# ``LAUNCHD_HARD_EXIT_RESERVE_S``. Subtracting the fixed reserve from such a
# budget yields a near-zero or zero-second watchdog, i.e. hard-exit the
# instant shutdown starts — no drain, no persistence, strictly worse than
# the SIGKILL it is avoiding. A short budget keeps the same shape (most of
# it for work, a slice held back for launchd) at proportional scale.
#
# The reserve is ``min(fixed, budget * FRACTION)``, which makes the armed
# window ``max(budget - fixed, budget * (1 - FRACTION))`` — continuous and
# monotonically non-decreasing in the budget, so a larger ExitTimeOut can
# never arm a smaller window. The two branches cross at
# ``LAUNCHD_HARD_EXIT_RESERVE_S / FRACTION``; 1/3 puts that crossover at 30,
# the smallest clamp launchd actually hands out, so every production clamp
# (30, 60) keeps the full fixed reserve and only genuinely tiny budgets
# scale.
LAUNCHD_SHORT_BUDGET_RESERVE_FRACTION = 1.0 / 3.0

_LAUNCHD_EXIT_TIMEOUT_RE = re.compile(r"^\s*exit timeout\s*=\s*(\d+)\s*$", re.MULTILINE)


def parse_launchd_exit_timeout(print_output: object) -> float | None:
    """Extract ``exit timeout = N`` from ``launchctl print`` output."""
    match = _LAUNCHD_EXIT_TIMEOUT_RE.search(str(print_output or ""))
    if match is None:
        return None
    return float(match.group(1))


def launchd_service_label(environ: Mapping[str, str] | None = None) -> str | None:
    """Return this process's launchd job label, or ``None`` when not launchd-owned."""
    env = os.environ if environ is None else environ
    label = str(env.get("XPC_SERVICE_NAME", "") or "").strip()
    if not label or label == "0":
        return None
    return label


def read_launchd_exit_timeout_s(
    label: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    uid: int | None = None,
    run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
) -> float | None:
    """Live ``ExitTimeOut`` (seconds) launchd enforces for this gateway's job.

    Returns ``None`` — meaning "no launchd budget applies" — when the process
    is not launchd-owned (no ``XPC_SERVICE_NAME``), ``launchctl`` is missing
    or fails, or the print output carries no ``exit timeout`` line. Callers
    must treat ``None`` as fail-open: the configured drain stands unchanged.
    """
    label = label or launchd_service_label(environ)
    if not label:
        return None
    if uid is None:
        # launchd is macOS-only; Windows has no os.getuid, so resolve it via getattr.
        _getuid = getattr(os, "getuid", None)
        if _getuid is None:
            return None
        uid = _getuid()
    try:
        resolved_uid = int(uid)
    except (TypeError, ValueError):
        return None
    domain = "system" if resolved_uid == 0 else f"gui/{resolved_uid}"
    try:
        proc = run(
            ["launchctl", "print", f"{domain}/{label}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    return parse_launchd_exit_timeout(getattr(proc, "stdout", ""))


def resolve_launchd_capped_drain(
    drain_timeout: float,
    launchd_exit_timeout_s: float | None,
    *,
    cleanup_reserve_s: float = LAUNCHD_STOP_CLEANUP_RESERVE_S,
    last_teardown_s: float | None = None,
    hard_exit_reserve_s: float = LAUNCHD_HARD_EXIT_RESERVE_S,
) -> float:
    """Clamp a SIGTERM-driven stop drain to what launchd will actually allow.

    ``launchd_exit_timeout_s`` is the live ``exit timeout`` for this job (see
    :func:`read_launchd_exit_timeout_s`); ``None`` means no launchd budget
    applies and the configured drain is returned untouched. Never *extends*
    the drain — an operator who configured a short one keeps it.

    The two reserves are ADDITIVE, not overlapping. The process does not run
    until launchd's SIGKILL at ``exit_timeout``; the shutdown watchdog
    hard-exits ``hard_exit_reserve_s`` earlier (see
    :func:`resolve_launchd_shutdown_watchdog_delay`). Sizing the teardown
    window against the SIGKILL wall therefore over-promises by exactly that
    reserve — at clamp 60 a 45s drain left only 5s before os._exit for a
    teardown allocated 15s, and a recorded 22s teardown got 12s. The drain
    may use at most ``exit_timeout - hard_exit_reserve_s -
    max(cleanup_reserve_s, last_teardown_s)`` so the teardown completes
    before the hard exit that actually fires.
    """

    def _seconds(value: object) -> float:
        try:
            return max(float(value), 0.0)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    drain = _seconds(drain_timeout)
    if launchd_exit_timeout_s is None:
        return drain
    try:
        budget = float(launchd_exit_timeout_s)
    except (TypeError, ValueError):
        return drain
    if budget <= 0.0:
        return drain
    hard_exit = resolve_launchd_shutdown_watchdog_delay(
        budget,
        budget,
        signal_driven=True,
        hard_exit_reserve_s=hard_exit_reserve_s,
    )
    # Bound the measured sample HERE, not only at the ledger read, so no
    # caller can starve the drain by handing in an oversized reserve. The
    # reader filters what it loads from disk, but `last_teardown_s` also
    # arrives through `effective_stop_drain_timeout` from a runner
    # attribute, and a sample at or above the actionable ceiling zeroes the
    # drain — dropping every in-flight session with no drain at all while
    # still not making the teardown fit. Falling back to the fixed cleanup
    # reserve is the honest answer: the teardown cannot be reserved for.
    reserve = resolve_stop_teardown_reserve_s(
        budget,
        last_teardown_s=last_teardown_s,
        cleanup_reserve_s=cleanup_reserve_s,
        hard_exit_reserve_s=hard_exit_reserve_s,
    )
    cap = max(hard_exit - reserve, 0.0)
    return min(drain, cap)


def resolve_stop_teardown_reserve_s(
    launchd_exit_timeout_s: float | None,
    *,
    last_teardown_s: float | None = None,
    cleanup_reserve_s: float = LAUNCHD_STOP_CLEANUP_RESERVE_S,
    hard_exit_reserve_s: float = LAUNCHD_HARD_EXIT_RESERVE_S,
) -> float:
    """Seconds of the stop budget held back for the post-drain teardown.

    THE ONE RESERVE for this seam: ``max(cleanup_reserve_s, last_teardown_s)``
    with the measured sample filtered through
    :func:`resolve_max_actionable_teardown_reserve_s`. Every site that needs
    the reserve — the drain cap, the arming leash, the drain deadline, the
    cron leash — calls this rather than re-deriving the ``max()`` and the
    ceiling filter, because a site that re-derives one of the two halves
    drifts from the deadline the process is actually running under (that is
    exactly the #838 defect class).
    """

    def _seconds(value: object) -> float:
        try:
            return max(float(value), 0.0)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    measured = _seconds(last_teardown_s)
    ceiling = resolve_max_actionable_teardown_reserve_s(
        launchd_exit_timeout_s, hard_exit_reserve_s=hard_exit_reserve_s
    )
    if ceiling is not None and measured >= ceiling:
        measured = 0.0
    return max(_seconds(cleanup_reserve_s), measured)


def resolve_launchd_shutdown_watchdog_delay(
    watchdog_delay_s: float,
    launchd_exit_timeout_s: float | None,
    *,
    signal_driven: bool,
    hard_exit_reserve_s: float = LAUNCHD_HARD_EXIT_RESERVE_S,
) -> float:
    """Return the hard-exit deadline for a launchd-timed shutdown.

    Signal-driven shutdown must stop itself before launchd's uncatchable
    SIGKILL. The final reserve leaves launchd headroom even when persistence
    or adapter teardown wedges. Other shutdown paths keep their normal
    watchdog leash.

    When the live budget is at or below the fixed reserve (a short but valid
    ``ExitTimeOut`` such as 8s), subtracting it outright would return zero —
    hard-exiting the instant shutdown starts, skipping all drain *and*
    persistence rather than using the time that genuinely exists. Short
    budgets therefore fall back to a proportional reserve, keeping a real
    persistence window on the near side of SIGKILL.
    """
    try:
        watchdog = max(float(watchdog_delay_s), 0.0)
    except (TypeError, ValueError):
        watchdog = 0.0
    if not signal_driven or launchd_exit_timeout_s is None:
        return watchdog
    try:
        launchd_budget = float(launchd_exit_timeout_s)
        reserve = max(float(hard_exit_reserve_s), 0.0)
    except (TypeError, ValueError):
        return watchdog
    if launchd_budget <= 0.0:
        return watchdog
    # The reserve must never consume more of the budget than the
    # proportional fallback would, or a LARGER ExitTimeOut arms a SMALLER
    # window. Gating on ``reserve >= launchd_budget`` left a cliff just
    # above the reserve (budget 10 -> 7.5s armed, but 11 -> 1.0s, 12 ->
    # 2.0s); FleetReview's proposed ``budget - reserve < budget * FRACTION``
    # gate only MOVES that cliff (measured with FRACTION=0.25: budget 13.33
    # -> 9.9975s armed, 13.34 -> 3.34s). A 1s watchdog calls os._exit one
    # second into the stop — before the drain, before agents are
    # interrupted, before the SQLite checkpoint — which is the silent-loss
    # outcome this fallback exists to prevent, and strictly worse than
    # letting launchd escalate at the full budget.
    #
    # Taking the SMALLER of the two reserves makes the armed window
    # ``max(budget - fixed, budget * (1 - FRACTION))``: continuous, and
    # monotonically non-decreasing in the budget (verified over 0.02..80.00
    # in 0.01 steps). Every production clamp is above the crossover, so this
    # is inert there — clamp 30 -> 20.0, clamp 60 -> 50.0, unchanged.
    reserve = min(reserve, launchd_budget * LAUNCHD_SHORT_BUDGET_RESERVE_FRACTION)
    return min(watchdog, max(launchd_budget - reserve, 0.0))


def resolve_max_actionable_teardown_reserve_s(
    launchd_exit_timeout_s: float | None,
    *,
    hard_exit_reserve_s: float = LAUNCHD_HARD_EXIT_RESERVE_S,
) -> float | None:
    """Largest teardown sample worth reserving for under a live budget.

    The teardown reserve is carved out of the drain, so it is bounded by
    the span between the start of the stop and the hard exit:
    ``exit_timeout - hard_exit_reserve_s``. A recorded sample at or above
    that ceiling cannot be honoured — the drain is already zero there — so
    treating it as the reserve buys nothing and costs every in-flight
    session its drain.

    This is the ONLY bound applied to the sample. The watchdog grace is
    deliberately NOT a ceiling here: when the grace is the tighter of the
    two, the right response is to widen the leash to the reserve (see
    :func:`resolve_armed_shutdown_watchdog_delay`), not to discard the
    measurement. Discarding it collapses the promise to the fixed 15s
    cleanup reserve, which is strictly worse than the measurement it
    replaces — at clamp 120 / configured 180 / measured 65 a grace-bounded
    ceiling rejected the sample, giving drain 95 and a 15s window for a
    teardown known to take 65s.

    Returns ``None`` when no launchd budget applies, meaning "no ceiling":
    an unsupervised stop is not racing a SIGKILL.
    """
    if launchd_exit_timeout_s is None:
        return None
    try:
        budget = float(launchd_exit_timeout_s)
    except (TypeError, ValueError):
        return None
    if not (budget > 0.0):
        return None
    return resolve_launchd_shutdown_watchdog_delay(
        budget,
        budget,
        signal_driven=True,
        hard_exit_reserve_s=hard_exit_reserve_s,
    )


def resolve_armed_shutdown_watchdog_delay(
    drain_timeout: float,
    launchd_exit_timeout_s: float | None,
    *,
    signal_driven: bool,
    last_teardown_s: float | None = None,
    cleanup_reserve_s: float = LAUNCHD_STOP_CLEANUP_RESERVE_S,
    grace_s: float | None = None,
    hard_exit_reserve_s: float = LAUNCHD_HARD_EXIT_RESERVE_S,
) -> float:
    """Wall-clock deadline the shutdown watchdog is actually armed with.

    The single source of truth for the two-step arming the stop path
    performs: an inner leash of ``drain + leash`` (see
    :func:`gateway.shutdown_watchdog.resolve_shutdown_watchdog_delay`),
    then :func:`resolve_launchd_shutdown_watchdog_delay` to pull it back
    inside launchd's budget.

    Extracted so the reserve invariant can be measured against the
    expression ``gateway.run`` really arms, rather than a copy of it
    re-derived in a test. The grace is large (60s by default), so at the
    production clamp (60, gui-domain-clamped) the inner leash loses the
    ``min()`` and the armed deadline is
    ``exit_timeout - hard_exit_reserve_s`` — exactly what
    :func:`resolve_launchd_capped_drain` sizes the teardown window against.

    The leash is ``max(grace, reserve)``, not the bare grace. On a clamp
    high enough for the inner term to bind (system-domain launchd is not
    gui-clamped to 60), a measured teardown ABOVE the grace would otherwise
    be promised a window the watchdog cuts short: at clamp 300 /
    configured 180 / measured 70 the grace-only leash armed at 240 for a
    drain of 180 — a 60s window for a 70s reserve. Widening the leash to
    the reserve honours the measurement instead of discarding it, and it
    can never push the deadline past the hard exit because the outer
    ``min()`` still binds.
    """
    from gateway.shutdown_watchdog import (
        DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S,
        resolve_shutdown_watchdog_delay,
    )

    def _seconds(value: object) -> float:
        try:
            return max(float(value), 0.0)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    grace = DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S if grace_s is None else grace_s
    reserve = resolve_stop_teardown_reserve_s(
        launchd_exit_timeout_s,
        last_teardown_s=last_teardown_s,
        cleanup_reserve_s=cleanup_reserve_s,
        hard_exit_reserve_s=hard_exit_reserve_s,
    )
    leash = max(_seconds(grace), reserve)
    return resolve_launchd_shutdown_watchdog_delay(
        resolve_shutdown_watchdog_delay(drain_timeout, grace_s=leash),
        launchd_exit_timeout_s,
        signal_driven=signal_driven,
        hard_exit_reserve_s=hard_exit_reserve_s,
    )


def resolve_stop_drain_deadline_s(
    drain_timeout: float,
    launchd_exit_timeout_s: float | None,
    *,
    signal_driven: bool,
    cleanup_reserve_s: float = LAUNCHD_STOP_CLEANUP_RESERVE_S,
    last_teardown_s: float | None = None,
    hard_exit_reserve_s: float = LAUNCHD_HARD_EXIT_RESERVE_S,
    armed_deadline_s: float | None = None,
) -> float | None:
    """Wall-clock instant (from the start of ``stop()``) the drain must END by.

    THE ONE DEADLINE for this seam. ``None`` means "no absolute deadline
    applies" — not launchd-timed, or not signal-driven — and every caller
    must treat that as fail-open (the configured budget stands untouched).

    The deadline is the ARMED watchdog (the deadline
    :func:`resolve_armed_shutdown_watchdog_delay` hands ``arm_shutdown_
    watchdog``, i.e. the instant ``os._exit`` actually fires) minus the
    post-drain teardown reserve. It is deliberately NOT ``exit_timeout -
    hard_exit_reserve_s - reserve``: those two are equal only when the
    OUTER ``min()`` of the arming expression binds, which is the case at
    the gui-clamped 60 and nowhere else. On a clamp high enough for the
    inner ``drain + max(grace, reserve)`` leash to bind — system-domain
    launchd is not gui-clamped — the armed deadline is EARLIER, and a drain
    fitted to the hard-exit wall runs into the teardown window the watchdog
    does not grant.

    Worked at the motivating geometry (clamp 300, configured 180, measured
    teardown 70, grace 60)::

        effective drain = resolve_launchd_capped_drain(180, 300, 70) = 180
        armed           = min(180 + max(60, 70), 290)                = 250
        deadline        = 250 - 70                                   = 180

    versus the hard-exit derivation's ``290 - 70`` = 220 — 40 seconds of
    drain that would be charged straight to the teardown reserve. At the
    production clamp (60) the outer ``min()`` binds and this returns the
    same 35 the hard-exit derivation did, so it is inert there.

    ``drain_timeout`` is the EFFECTIVE (already launchd-capped) drain, i.e.
    what :func:`effective_stop_drain_timeout` returned, because that is
    what the watchdog was armed from.

    ``armed_deadline_s`` lets the stop path pass the value it ACTUALLY
    armed ``arm_shutdown_watchdog`` with instead of having this function
    recompute it. The recomputation is correct only while the runner state
    the arming read (``_last_shutdown_teardown_s``, the live
    ``ExitTimeOut``) is unchanged; passing the captured value makes the
    deadline provably the armed one rather than a second derivation that
    has to be argued equal. ``gateway.run`` passes it.
    """
    if not signal_driven or launchd_exit_timeout_s is None:
        return None
    try:
        budget = float(launchd_exit_timeout_s)
    except (TypeError, ValueError):
        return None
    if budget <= 0.0:
        return None
    armed: float | None = None
    if armed_deadline_s is not None:
        try:
            armed = max(float(armed_deadline_s), 0.0)
        except (TypeError, ValueError):
            armed = None
    if armed is None:
        armed = resolve_armed_shutdown_watchdog_delay(
            drain_timeout,
            budget,
            signal_driven=True,
            last_teardown_s=last_teardown_s,
            cleanup_reserve_s=cleanup_reserve_s,
            hard_exit_reserve_s=hard_exit_reserve_s,
        )
    reserve = resolve_stop_teardown_reserve_s(
        budget,
        last_teardown_s=last_teardown_s,
        cleanup_reserve_s=cleanup_reserve_s,
        hard_exit_reserve_s=hard_exit_reserve_s,
    )
    return max(armed - reserve, 0.0)


def resolve_elapsed_adjusted_drain(
    drain_timeout: float,
    launchd_exit_timeout_s: float | None,
    *,
    signal_driven: bool,
    elapsed_s: float,
    cleanup_reserve_s: float = LAUNCHD_STOP_CLEANUP_RESERVE_S,
    last_teardown_s: float | None = None,
    hard_exit_reserve_s: float = LAUNCHD_HARD_EXIT_RESERVE_S,
    armed_deadline_s: float | None = None,
) -> float:
    """Fit a launchd-timed drain inside the deadline the pre-drain phases left.

    Consumes THE ONE DEADLINE (:func:`resolve_stop_drain_deadline_s`); it
    derives no arithmetic of its own. That deadline is the ARMED watchdog
    minus the teardown reserve — the armed value is what actually calls
    ``os._exit``, and fitting against ``exit_timeout -
    hard_exit_reserve_s`` instead was the #838 defect: on any clamp where
    the inner leash binds the drain overran into the teardown window (clamp
    300 / configured 180 / measured 70: deadline 220 against an armed 250,
    so every second of pre-drain elapsed was charged to a 70s reserve).

    ``stop()`` runs against an ABSOLUTE wall measured from its own start.
    The drain does not start at zero — it starts at ``elapsed_s``, after
    the pre-drain phases: secondary-profile reconnect cancellation (up to
    the adapter disconnect timeout), the per-session shutdown
    notifications (a sequential ``await adapter.send`` each, no per-send
    timeout), the boot-resume cancellation and the pre-drain
    ``resume_pending`` marking (SQLite writes plus notify sends). What
    remains for it is therefore ``deadline - elapsed_s``, and the drain
    is whichever of that and the configured budget is SMALLER::

        drain = min(configured_drain, deadline - elapsed_s)

    Worked examples at the production clamp (60), ``hard_exit_reserve_s``
    10, teardown reserve 15 -> ``deadline`` 35 (the outer ``min()`` binds
    there, so the armed and hard-exit derivations agree):

    * configured 45, elapsed 12 -> ``min(45, 35 - 12)`` = **23**. The
      deadline binds; the elapsed genuinely costs the drain.
    * configured 20, elapsed 12 -> ``min(20, 35 - 12)`` = **20**, NOT
      ``20 - 12`` = 8. The operator's 20s still fits (12 + 20 = 32 <= 35),
      so it stands in full.

    That second row is the bug this replaced. Subtracting the elapsed
    from the drain unconditionally treats the launchd budget as a mere
    boolean gate and charges the elapsed twice whenever the configured
    drain already fits in the remaining window — dropping in-flight
    sessions early while the supervisor window sits idle (measured over
    43,920 geometries: 41.5% over-subtracted, worst case 60s of drain
    discarded at clamp 300). Taking the ``min`` keeps the teardown
    reserve exactly as honest while never spending headroom that exists.

    Only applies to launchd-timed signal stops: every other path has no
    absolute supervisor deadline to preserve headroom against, so its
    configured drain stands untouched. The cron branch consumes the same
    deadline via :func:`resolve_cron_drain_budget(watchdog_delay=...)`.
    """

    def _seconds(value: object) -> float:
        try:
            return max(float(value), 0.0)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    drain = _seconds(drain_timeout)
    deadline = resolve_stop_drain_deadline_s(
        drain,
        launchd_exit_timeout_s,
        signal_driven=signal_driven,
        cleanup_reserve_s=cleanup_reserve_s,
        last_teardown_s=last_teardown_s,
        hard_exit_reserve_s=hard_exit_reserve_s,
        armed_deadline_s=armed_deadline_s,
    )
    if deadline is None:
        return drain
    # Fail open on an unreadable elapsed: the whole point of the deadline is
    # to spend a KNOWN pre-drain cost. With no usable reading there is
    # nothing to charge, and silently fitting the drain to the bare deadline
    # would shorten it on a measurement fault rather than a real cost.
    try:
        elapsed = max(float(elapsed_s), 0.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return drain
    return max(min(drain, deadline - elapsed), 0.0)


def effective_stop_drain_timeout(runner: object) -> float:
    """Drain budget for the stop in progress on ``runner``.

    Signal-driven stops under launchd are timed by launchd's live
    ``ExitTimeOut`` (``runner._launchd_exit_timeout_s``, set at boot);
    everything else — in-band SIGUSR1 restart after the turn, ``--replace``
    takeover, tests — keeps the configured drain. Duck-typed and
    getattr-guarded on purpose: shutdown-path tests drive ``_stop_impl``
    from bare doubles that are not ``GatewayRunner`` instances.
    """
    drain = getattr(runner, "_restart_drain_timeout", DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT)
    if not getattr(runner, "_stop_requested_by_signal", False):
        return drain
    return resolve_launchd_capped_drain(
        drain,
        getattr(runner, "_launchd_exit_timeout_s", None),
        last_teardown_s=getattr(runner, "_last_shutdown_teardown_s", None),
    )


def is_gateway_supervisor_process(
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether this gateway process is owned by a supervisor."""
    env = os.environ if environ is None else environ
    if env.get("INVOCATION_ID"):
        return True
    if env.get("HERMES_S6_SUPERVISED_CHILD"):
        return True
    xpc_service = env.get("XPC_SERVICE_NAME", "")
    if xpc_service and xpc_service != "0":
        return True
    return str(env.get(EXTERNAL_GATEWAY_SUPERVISOR_ENV, "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def is_container_restart_context() -> bool:
    """Return whether the gateway is running inside a container for restart
    routing purposes (Docker/Podman ⇒ the detached setsid path dies with the
    cgroup; exit-75 service restart is the only viable path).

    Extracted from the inline probe in the /restart handler so tests can mock
    container detection hermetically — a real ``/.dockerenv`` on a
    containerized CI runner otherwise flips the routing under the test.
    """
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def parse_restart_drain_timeout(raw: object) -> float:
    """Parse a configured drain timeout, falling back to the shared default."""
    try:
        value = float(raw) if str(raw or "").strip() else DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    except (TypeError, ValueError):
        return DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    return max(0.0, value)


def parse_restart_after_turn_timeout(raw: object) -> float:
    """Parse the after-turn wait cap for in-band restart, falling back to default.

    ``0`` is a deliberate disable (legacy immediate drain) and must not fall
    through to the default — unlike empty/missing input.
    """
    if raw is None:
        return DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT
    if isinstance(raw, str) and not raw.strip():
        return DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT
    return max(0.0, value)


def parse_cron_drain_timeout(raw: object) -> float:
    """Parse the cron-only drain floor, falling back to the shared default.

    ``0`` is a deliberate opt-out — cron work is then interrupted on the same
    budget as chat work, the pre-#82161 behaviour — and must not fall through
    to the default, unlike empty/missing input.
    """
    if raw is None:
        return DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT
    if isinstance(raw, str) and not raw.strip():
        return DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT
    return max(0.0, value)


def resolve_cron_drain_budget(
    drain_timeout: float,
    cron_drain_timeout: float,
    *,
    watchdog_delay: float,
    elapsed: float = 0.0,
    cleanup_reserve_s: float = CRON_DRAIN_CLEANUP_RESERVE_S,
) -> float:
    """Seconds the shutdown drain may spend waiting on in-flight cron work.

    The configured floor is clamped to what this process can actually honour.
    The shutdown watchdog hard-exits at ``watchdog_delay`` and the service
    manager's ``TimeoutStopSec`` is sized from the full stop budget (drain
    vs cron floor + cleanup reserve, plus headroom — see
    ``resolve_systemd_timeout_stop_sec``), so waiting past that leash
    (minus ``cleanup_reserve_s`` for the teardown that follows the drain)
    would swap a cleanly-interrupted job for a SIGKILL that leaves it
    wedged mid-run — strictly worse than the bug being fixed.

    Never returns less than ``drain_timeout``: the cron floor only ever
    extends the wait, so an operator who deliberately configured a long
    ``restart_drain_timeout`` keeps it.
    """

    def _seconds(value: object, fallback: float = 0.0) -> float:
        try:
            return max(float(value), 0.0)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return fallback

    drain = _seconds(drain_timeout)
    floor = _seconds(cron_drain_timeout)
    if floor <= 0.0:
        return drain
    ceiling = (
        _seconds(watchdog_delay)
        - _seconds(elapsed)
        - _seconds(cleanup_reserve_s, CRON_DRAIN_CLEANUP_RESERVE_S)
    )
    return max(drain, min(floor, ceiling))


def resolve_systemd_timeout_stop_sec(
    drain_timeout: float,
    cron_drain_timeout: float = DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT,
    *,
    cleanup_reserve_s: float = CRON_DRAIN_CLEANUP_RESERVE_S,
    headroom_s: float = SYSTEMD_STOP_HEADROOM_S,
    floor_s: float = SYSTEMD_TIMEOUT_STOP_SEC_FLOOR,
) -> int:
    """Seconds systemd ``TimeoutStopSec`` must cover the full stop budget.

    ``restart_drain_timeout`` is only the chat-turn interrupt budget (default
    0). The stop path may wait longer for in-flight cron work —
    ``cron_drain_timeout`` plus ``cleanup_reserve_s`` — before it even starts
    interrupting. Sizing the unit from drain alone lets systemd SIGKILL an
    in-budget drain (#94759).

    A zero ``cron_drain_timeout`` is a deliberate opt-out and does not extend
    the budget. Non-numeric inputs degrade to 0 rather than raising.
    """

    def _seconds(value: object) -> float:
        try:
            return max(float(value), 0.0)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    drain = _seconds(drain_timeout)
    cron = _seconds(cron_drain_timeout)
    reserve = _seconds(cleanup_reserve_s)
    headroom = _seconds(headroom_s)
    floor = _seconds(floor_s)
    cron_budget = (cron + reserve) if cron > 0.0 else 0.0
    stop_budget = max(drain, cron_budget)
    return int(max(floor, stop_budget + headroom))


# ``--replace`` takeover: how long the NEW gateway waits for the OLD one to
# finish its graceful stop before escalating SIGTERM → SIGKILL. Must cover
# the same stop budget systemd's TimeoutStopSec covers, or a busy restart
# SIGKILLs the old process mid-drain / mid-SQLite-write (state.db
# corruption, incident 2026-09-16). The 10s historical floor is kept as the
# minimum so an idle gateway still cycles fast.
REPLACE_TAKEOVER_GRACE_FLOOR_S = 10.0
REPLACE_TAKEOVER_GRACE_HEADROOM_S = 30.0


def resolve_replace_takeover_grace_s(
    drain_timeout: float,
    cron_drain_timeout: float = DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT,
    *,
    cleanup_reserve_s: float = CRON_DRAIN_CLEANUP_RESERVE_S,
    headroom_s: float = REPLACE_TAKEOVER_GRACE_HEADROOM_S,
    floor_s: float = REPLACE_TAKEOVER_GRACE_FLOOR_S,
) -> float:
    """Seconds ``--replace`` waits for the old gateway before SIGKILL.

    Same budget model as :func:`resolve_systemd_timeout_stop_sec` (drain vs
    cron+reserve, plus headroom, floored) so the two supervisors agree on
    when a stop is "stuck" versus merely draining. Returns a float so the
    caller can poll on a monotonic deadline.
    """
    return float(
        resolve_systemd_timeout_stop_sec(
            drain_timeout,
            cron_drain_timeout,
            cleanup_reserve_s=cleanup_reserve_s,
            headroom_s=headroom_s,
            floor_s=floor_s,
        )
    )


def resolve_restart_exit_wait_budget(
    drain_timeout: float,
    after_turn_timeout: float,
    *,
    headroom: float = 15.0,
) -> float:
    """Seconds a CLI should wait for the gateway PID to exit after SIGUSR1.

    In-band restart may defer ``stop()`` until active turns finish
    (``after_turn_timeout``) and then spend up to ``drain_timeout`` inside
    ``stop()``. Callers that fall back to a hard kill on wait expiry must
    cover both phases or they reintroduce #77184.
    """
    try:
        drain = max(float(drain_timeout), 0.0)
    except (TypeError, ValueError):
        drain = 0.0
    try:
        after_turn = max(float(after_turn_timeout), 0.0)
    except (TypeError, ValueError):
        after_turn = 0.0
    try:
        margin = max(float(headroom), 0.0)
    except (TypeError, ValueError):
        margin = 0.0
    return drain + after_turn + margin
