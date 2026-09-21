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
    ceiling = resolve_max_actionable_teardown_reserve_s(
        budget, hard_exit_reserve_s=hard_exit_reserve_s
    )
    measured = _seconds(last_teardown_s)
    if ceiling is not None and measured >= ceiling:
        measured = 0.0
    reserve = max(_seconds(cleanup_reserve_s), measured)
    cap = max(hard_exit - reserve, 0.0)
    return min(drain, cap)


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
    grace_s: float | None = None,
) -> float | None:
    """Largest teardown sample worth reserving for under a live budget.

    The teardown reserve is carved out of the drain, so it is bounded by
    the post-drain window the watchdog can actually leave. Two separate
    bounds apply and the smaller one wins:

    * ``exit_timeout - hard_exit_reserve_s`` — the whole span between the
      start of the stop and the hard exit. A sample at or above it cannot
      be honoured (the drain is already zero there), so reserving for it
      buys nothing and costs every in-flight session its drain.
    * the watchdog grace — the armed deadline is
      ``min(drain + grace, hard_exit)``, so when the inner leash binds the
      post-drain window is ``grace``, no matter how far the drain shrinks.
      A larger sample would promise a teardown ``os._exit`` cuts short:
      measured at clamp 300 / configured 180 / sample 70, the old
      span-only ceiling (290) accepted the sample, giving drain 180, armed
      240 and a 60s window for a 70s reserve.

    A sample AT the ceiling is rejected along with one above it: at the
    ceiling the drain is exactly zero, which is the drain-starvation this
    bound exists to prevent, not a usable reserve.

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
    from gateway.shutdown_watchdog import DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S

    grace = DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S if grace_s is None else grace_s
    hard_exit = resolve_launchd_shutdown_watchdog_delay(
        budget,
        budget,
        signal_driven=True,
        hard_exit_reserve_s=hard_exit_reserve_s,
    )
    try:
        grace_bound = max(float(grace), 0.0)
    except (TypeError, ValueError):
        return hard_exit
    return min(hard_exit, grace_bound)


def resolve_armed_shutdown_watchdog_delay(
    drain_timeout: float,
    launchd_exit_timeout_s: float | None,
    *,
    signal_driven: bool,
    grace_s: float | None = None,
    hard_exit_reserve_s: float = LAUNCHD_HARD_EXIT_RESERVE_S,
) -> float:
    """Wall-clock deadline the shutdown watchdog is actually armed with.

    The single source of truth for the two-step arming the stop path
    performs: an inner leash of ``drain + grace`` (see
    :func:`gateway.shutdown_watchdog.resolve_shutdown_watchdog_delay`),
    then :func:`resolve_launchd_shutdown_watchdog_delay` to pull it back
    inside launchd's budget.

    Extracted so the reserve invariant can be measured against the
    expression ``gateway.run`` really arms, rather than a copy of it
    re-derived in a test. The grace is large (60s by default), so under
    launchd the inner leash always loses the ``min()`` and the armed
    deadline is ``exit_timeout - hard_exit_reserve_s`` — which is exactly
    what :func:`resolve_launchd_capped_drain` sizes the teardown window
    against.
    """
    from gateway.shutdown_watchdog import (
        DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S,
        resolve_shutdown_watchdog_delay,
    )

    grace = DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S if grace_s is None else grace_s
    return resolve_launchd_shutdown_watchdog_delay(
        resolve_shutdown_watchdog_delay(drain_timeout, grace_s=grace),
        launchd_exit_timeout_s,
        signal_driven=signal_driven,
        hard_exit_reserve_s=hard_exit_reserve_s,
    )


def resolve_elapsed_adjusted_drain(
    drain_timeout: float,
    launchd_exit_timeout_s: float | None,
    *,
    signal_driven: bool,
    elapsed_s: float,
) -> float:
    """Shrink a launchd-timed drain by the pre-drain phases already spent.

    The hard-exit watchdog is armed at the TOP of the stop with an
    ABSOLUTE deadline (``exit_timeout - hard_exit_reserve_s``), but the
    drain is a RELATIVE budget that only starts after the pre-drain
    phases: secondary-profile reconnect cancellation (up to the adapter
    disconnect timeout), the per-session shutdown notifications (a
    sequential ``await adapter.send`` each, no per-send timeout), the
    boot-resume cancellation and the pre-drain ``resume_pending`` marking
    (SQLite writes plus notify sends). The stop path logs those phase
    elapsed times precisely because they are not free.

    So with ``elapsed_s`` spent before the drain starts, a drain sized
    against the absolute deadline actually ends at ``elapsed + drain``
    and the post-drain teardown gets ``reserve - elapsed``, not
    ``reserve``. At the production clamp a 15s pre-drain phase consumes
    the entire 15s default reserve and puts ``os._exit`` inside the
    SQLite checkpoint — the corruption class the reserve exists to
    prevent.

    Subtracting the elapsed time keeps the reserve honest in wall-clock
    terms. Only applies to launchd-timed signal stops: every other path
    has no absolute supervisor deadline to preserve headroom against, so
    its configured drain stands untouched. The cron branch already does
    the equivalent via ``resolve_cron_drain_budget(elapsed=...)``.
    """

    def _seconds(value: object) -> float:
        try:
            return max(float(value), 0.0)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    drain = _seconds(drain_timeout)
    if not signal_driven or launchd_exit_timeout_s is None:
        return drain
    try:
        budget = float(launchd_exit_timeout_s)
    except (TypeError, ValueError):
        return drain
    if budget <= 0.0:
        return drain
    return max(drain - _seconds(elapsed_s), 0.0)


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
