"""Out-of-loop shutdown and event-loop liveness backstops (#66892, #69089).

When the asyncio loop freezes mid-drain, every asyncio-based recovery path is
structurally unable to fire: the drain deadline, status rewrites, and forensics
all need the same loop that is stuck. launchd/systemd KeepAlive only restarts a
*dead* process, so a wedged-but-alive gateway sits as a zombie until manual
SIGKILL.

This module provides:

1. A plain OS-thread shutdown watchdog armed at ``stop()``. If shutdown has not
   completed within ``restart_drain_timeout + grace``, it dumps all-thread
   stacks via ``faulthandler`` plus a metadata snapshot, then ``os._exit`` so
   the service manager can revive the process.
2. An event-loop heartbeat file at ``<HERMES_HOME>/state/gateway.heartbeat`` so
   external supervision can distinguish "process alive" from "loop frozen"
   (``gateway_state.json`` alone can't — it only rewrites on transitions/turns).
3. A lifetime thread watchdog that can still diagnose and hard-exit when the
   event loop is too frozen to run its own heartbeat or timeout callbacks.
4. A self-rescheduling floor timer that keeps the loop selector's timeout
   finite, giving existing async recovery tasks a chance to resume.
"""

from __future__ import annotations

import asyncio
import faulthandler
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from gateway.restart import GATEWAY_SERVICE_RESTART_EXIT_CODE
from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

# Extra leash beyond ``agent.restart_drain_timeout`` so a slow-but-progressing
# drain is not cut short. Matches the issue #66892 suggested hardening.
DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S = 60.0
DEFAULT_HEARTBEAT_INTERVAL_S = 30.0
DEFAULT_LOOP_FLOOR_TIMER_INTERVAL_S = 5.0
DEFAULT_LOOP_WATCHDOG_INTERVAL_S = 30.0
DEFAULT_LOOP_WATCHDOG_TIMEOUT_S = 10.0
# 3 sustained misses (~90-120s of loop block) escalate. The false-positive
# class that motivated raising this (the watchdog's own on-loop heartbeat
# fsync stalling the loop it monitors) is fixed at the root by the off-loop
# heartbeat write + two-witness probe (#90502), so the default stays tight
# for genuine wedges. Deployments with legitimately slow loops can tune via
# gateway.loop_watchdog_* in config.yaml.
DEFAULT_LOOP_WATCHDOG_MAX_STRIKES = 3
# Host-starvation classification for a missed-probe escalation.
#
# Exit 75 is the right answer for a WEDGED loop (deadlock / synchronous block):
# the process cannot recover itself and a supervised restart does. It is the
# wrong answer for a loop that is merely STARVED of CPU, because the replacement
# process contends for the same starved host — measured 2026-09-20, when runaway
# CPU burners drove load to 538 on 32 cores, the watchdog exited 75 twice, and
# launchd relaunched into a 12-minute boot with all four adapters timing out
# simultaneously. The self-kill converted a slow-but-alive gateway into a dead
# one. Both failures present identically at the probe, so the classification has
# to be explicit.
#
# ``load1 > max(factor * ncpu, floor)`` is the starvation predicate. The
# absolute floor keeps a small host (2-4 cores) from being declared starved at a
# load that is merely busy, and the factor is tunable via
# ``gateway.liveness_starvation_load_factor``.
DEFAULT_LIVENESS_STARVATION_LOAD_FACTOR = 2.0
LIVENESS_STARVATION_LOAD_FLOOR = 8.0
# A starved hold is bounded: after this much CONTINUOUS starvation with no
# successful probe, exit 75 anyway. Past this point the distinction stops
# mattering — whatever it is, it is not resolving on its own.
DEFAULT_LIVENESS_STARVATION_MAX_HOLD_S = 900.0
_HEARTBEAT_RELATIVE = ("state", "gateway.heartbeat")
_WATCHDOG_DUMP_RELATIVE = ("logs", "gateway-shutdown-watchdog.log")


class _LoopFloorTimerHandle:
    """Cancelable owner for the currently scheduled selector floor timer."""

    def __init__(self, loop: asyncio.AbstractEventLoop, interval: float):
        self._loop = loop
        self._interval = interval
        self._cancelled = False
        self._timer: Optional[asyncio.TimerHandle] = None
        self._schedule()

    def _schedule(self) -> None:
        self._timer = self._loop.call_later(self._interval, self._tick)

    def _tick(self) -> None:
        if not self._cancelled:
            self._schedule()

    def cancel(self) -> None:
        self._cancelled = True
        if self._timer is not None:
            self._timer.cancel()


_REPO_ROOT = str(Path(__file__).resolve().parent.parent)


def describe_blocked_loop_thread(thread_ident: Optional[int]) -> tuple[str, str]:
    """Return ``(site, stack)`` for the thread running the event loop.

    ``site`` is ``path:line func`` of the innermost frame under the repo root
    (``unknown`` if none); ``stack`` is the formatted stack (last 30 frames).
    Pure in-process frame walk (``sys._current_frames``) — no signals, no
    external profiler. Never raises.
    """
    try:
        import traceback

        frame = sys._current_frames().get(thread_ident) if thread_ident else None
        if frame is None:
            return "unknown", "(loop thread frame unavailable)"
        summary = traceback.extract_stack(frame)
        site = "unknown"
        for fs in summary:
            fn = fs.filename or ""
            if fn.startswith(_REPO_ROOT) and "/site-packages/" not in fn and "/.venv/" not in fn:
                rel = os.path.relpath(fn, _REPO_ROOT)
                site = f"{rel}:{fs.lineno} {fs.name}"
        stack = "".join(traceback.format_list(summary[-30:]))
        return site, stack
    except Exception:
        return "unknown", "(loop thread stack capture failed)"


def _log_blocked_loop_site(thread_ident: Optional[int], blocked_s: float) -> None:
    """Emit the structured ``PHASE=event_loop_blocked ... site=`` line + stack.

    ``site=`` is last on the line so ``unclean_restart_notice._BLOCKED_SITE_RE``
    picks it up on the next boot. Never raises.
    """
    try:
        site, stack = describe_blocked_loop_thread(thread_ident)
        logger.error(
            "PHASE=event_loop_blocked source=liveness_watchdog seconds=%d site=%s",
            int(blocked_s),
            site,
        )
        logger.error(
            "PHASE=event_loop_blocked source=liveness_watchdog loop-thread stack:\n%s",
            stack,
        )
    except Exception:
        pass


class _LoopLivenessWatchdogHandle:
    """Small lifecycle handle for the daemon liveness thread."""

    def __init__(self, stop_event: threading.Event, thread: threading.Thread):
        self._stop_event = stop_event
        self._thread = thread

    def stop(self) -> None:
        self._stop_event.set()

    def join(self, timeout: Optional[float] = None) -> None:
        self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self._thread.is_alive()


def _host_load_suffix() -> str:
    """Return ``load1=<x> ncpu=<n>`` for the missed-probe CRITICAL.

    A wedged event loop and a starved one produce the SAME watchdog symptom,
    and the log line alone could not tell them apart — the 2026-09-20 incident
    (a kanban worker's runaway busy-loops at load 538 on 32 cores) read as a
    gateway deadlock for 55 minutes. Attaching the host's 1-minute load average
    and CPU count makes the next occurrence self-diagnosing.

    Best-effort by construction: this runs on a path that is about to hard-exit,
    so a platform without ``os.getloadavg`` (Windows) reports ``unknown`` rather
    than raising and costing us the diagnostic dump.
    """
    try:
        load1 = f"{os.getloadavg()[0]:.2f}"
    except (OSError, AttributeError, IndexError):
        load1 = "unknown"
    try:
        ncpu = str(os.cpu_count() or "unknown")
    except Exception:
        ncpu = "unknown"
    return f"load1={load1} ncpu={ncpu}"


def _sample_host_load() -> tuple[Optional[float], Optional[int]]:
    """Return ``(load1, ncpu)``, either element ``None`` when unavailable.

    Best-effort by construction: this runs on a path that may be about to
    hard-exit, so a platform without ``os.getloadavg`` (Windows) reports
    ``None`` rather than raising and costing us the diagnostic dump. An
    unavailable load average classifies as WEDGED — absence of evidence for
    starvation must not create a hold.
    """
    try:
        load1: Optional[float] = float(os.getloadavg()[0])
    except (OSError, AttributeError, IndexError, TypeError, ValueError):
        load1 = None
    try:
        ncpu: Optional[int] = os.cpu_count()
    except Exception:
        ncpu = None
    return load1, ncpu


class _LivenessMissDecision:
    """What the watchdog should do about one escalated missed-probe run."""

    __slots__ = ("action", "strikes", "phase", "starved_since")

    def __init__(
        self,
        *,
        action: str,
        strikes: int,
        phase: Optional[str],
        starved_since: Optional[float],
    ):
        self.action = action  # "exit" | "hold"
        self.strikes = strikes
        self.phase = phase
        self.starved_since = starved_since


def evaluate_liveness_miss(
    *,
    strikes: int,
    strikes_limit: int,
    load1: Optional[float],
    ncpu: Optional[int],
    load_factor: float,
    starved_since: Optional[float],
    now: float,
    max_hold_s: float,
) -> _LivenessMissDecision:
    """Classify an escalated missed-probe run as starved (hold) or wedged (exit).

    Pure function so the decision is testable without generating real load —
    generating load on the host is the incident this guards against.
    """
    try:
        factor = float(load_factor)
        if not (factor > 0) or factor != factor or factor == float("inf"):
            factor = DEFAULT_LIVENESS_STARVATION_LOAD_FACTOR
    except (TypeError, ValueError):
        factor = DEFAULT_LIVENESS_STARVATION_LOAD_FACTOR

    starved = False
    if load1 is not None and load1 == load1:  # not NaN
        cores = ncpu if isinstance(ncpu, int) and ncpu > 0 else 1
        threshold = max(factor * cores, LIVENESS_STARVATION_LOAD_FLOOR)
        starved = load1 > threshold

    if not starved:
        return _LivenessMissDecision(
            action="exit", strikes=strikes, phase=None, starved_since=None
        )

    began = starved_since if starved_since is not None else now
    try:
        ceiling = float(max_hold_s)
    except (TypeError, ValueError):
        ceiling = DEFAULT_LIVENESS_STARVATION_MAX_HOLD_S
    if now - began >= ceiling:
        return _LivenessMissDecision(
            action="exit",
            strikes=strikes,
            phase="liveness_starved_giveup",
            starved_since=began,
        )

    # Hold: back the counter off to one below the limit so the next HEALTHY
    # probe clears it outright and the next MISSED one re-evaluates rather than
    # the hold silently swallowing every future miss.
    return _LivenessMissDecision(
        action="hold",
        strikes=max(strikes_limit - 1, 0),
        phase="liveness_starved",
        starved_since=began,
    )


def _format_starvation_line(
    phase: str,
    *,
    load1: Optional[float],
    ncpu: Optional[int],
    strikes: int,
    held_s: float,
) -> str:
    load_txt = f"{load1:.2f}" if load1 is not None else "unknown"
    ncpu_txt = str(ncpu) if ncpu else "unknown"
    return (
        f"PHASE={phase} load1={load_txt} ncpu={ncpu_txt} strikes={strikes} "
        f"held_s={held_s:.1f}"
    )


def _arm_loop_floor_timer(
    loop: asyncio.AbstractEventLoop,
    interval: float = DEFAULT_LOOP_FLOOR_TIMER_INTERVAL_S,
) -> _LoopFloorTimerHandle:
    """Keep at least one timer pending so selector waits remain bounded."""
    try:
        resolved_interval = float(interval)
        if resolved_interval <= 0:
            raise ValueError
    except (TypeError, ValueError):
        resolved_interval = DEFAULT_LOOP_FLOOR_TIMER_INTERVAL_S
    return _LoopFloorTimerHandle(loop, resolved_interval)


def start_loop_liveness_watchdog(
    loop: asyncio.AbstractEventLoop,
    *,
    probe_interval: float = DEFAULT_LOOP_WATCHDOG_INTERVAL_S,
    probe_timeout: float = DEFAULT_LOOP_WATCHDOG_TIMEOUT_S,
    max_strikes: int = DEFAULT_LOOP_WATCHDOG_MAX_STRIKES,
    starvation_load_factor: float = DEFAULT_LIVENESS_STARVATION_LOAD_FACTOR,
    starvation_max_hold_s: float = DEFAULT_LIVENESS_STARVATION_MAX_HOLD_S,
    exit_code: int = GATEWAY_SERVICE_RESTART_EXIT_CODE,
) -> Optional[_LoopLivenessWatchdogHandle]:
    """Start an out-of-loop watchdog that hard-exits after missed probes.

    The guard is on by default; operators opt out with
    ``gateway.loop_watchdog: false`` in config.yaml (enforced by the caller,
    ``GatewayRunner._start_loop_liveness_guards`` — this module stays
    config-agnostic so bare-loop tests can drive it directly).
    """
    interval = probe_interval
    timeout = probe_timeout
    strikes_limit = max_strikes
    stop_event = threading.Event()
    # The thread whose stack names the blocking site when probes go unanswered.
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    loop_thread_ident = (
        threading.get_ident() if running is loop else threading.main_thread().ident
    )

    def _wait_for_probe(probe_event: threading.Event) -> Optional[bool]:
        deadline = time.monotonic() + timeout
        while True:
            if stop_event.is_set():
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return probe_event.is_set()
            if probe_event.wait(timeout=min(remaining, 0.05)):
                return True

    def _watchdog() -> None:
        strikes = 0
        starved_since: Optional[float] = None
        while not stop_event.wait(timeout=interval):
            probe_event = threading.Event()
            try:
                loop.call_soon_threadsafe(probe_event.set)
            except RuntimeError:
                # A normally closed loop cannot be probed and no longer needs
                # a process-liveness backstop.
                return
            except Exception:
                logger.debug(
                    "Failed to schedule gateway loop liveness probe", exc_info=True
                )
                return

            responded = _wait_for_probe(probe_event)
            if responded is None:
                return
            if responded:
                strikes = 0
                starved_since = None
                continue

            if stop_event.is_set():
                return
            strikes += 1
            if strikes < strikes_limit:
                continue

            if stop_event.is_set():
                return

            # Classify BEFORE exiting. A starved host is not a wedge, and a
            # supervised restart cannot fix it — see the constants above.
            load1, ncpu = _sample_host_load()
            decision = evaluate_liveness_miss(
                strikes=strikes,
                strikes_limit=strikes_limit,
                load1=load1,
                ncpu=ncpu,
                load_factor=starvation_load_factor,
                starved_since=starved_since,
                now=time.monotonic(),
                max_hold_s=starvation_max_hold_s,
            )
            starved_since = decision.starved_since
            if decision.action == "hold":
                # The structured line IS the page: this module has no alert
                # channel of its own, and inventing one on a path that must
                # stay allocation-light and never raise would be worse than
                # the log the fleet's log watchers already read.
                try:
                    logger.critical(
                        "Gateway event loop missed %d consecutive liveness "
                        "probes, but the HOST is starved, not the loop wedged; "
                        "HOLDING instead of exiting %d (a restart would contend "
                        "for the same CPU). %s",
                        strikes,
                        exit_code,
                        _format_starvation_line(
                            "liveness_starved",
                            load1=load1,
                            ncpu=ncpu,
                            strikes=strikes,
                            held_s=max(
                                time.monotonic() - (starved_since or time.monotonic()),
                                0.0,
                            ),
                        ),
                    )
                except Exception:
                    pass
                strikes = decision.strikes
                continue

            if decision.phase == "liveness_starved_giveup":
                try:
                    logger.critical(
                        "Gateway event loop starvation hold exceeded its "
                        "ceiling with no successful probe; exiting with code "
                        "%d anyway. %s",
                        exit_code,
                        _format_starvation_line(
                            "liveness_starved_giveup",
                            load1=load1,
                            ncpu=ncpu,
                            strikes=strikes,
                            held_s=max(
                                time.monotonic() - (starved_since or time.monotonic()),
                                0.0,
                            ),
                        ),
                    )
                except Exception:
                    pass
            else:
                try:
                    logger.critical(
                        "Gateway event loop missed %d consecutive liveness probes; "
                        "host %s; dumping all thread stacks and exiting with code "
                        "%d so the service supervisor can restart it.",
                        strikes,
                        _host_load_suffix(),
                        exit_code,
                    )
                except Exception:
                    pass
            _log_blocked_loop_site(loop_thread_ident, strikes * (interval + timeout))
            try:
                faulthandler.dump_traceback(all_threads=True)
            except Exception:
                logger.debug("Loop liveness faulthandler dump failed", exc_info=True)
            if stop_event.is_set():
                return
            # Record the watchdog exit in the lifecycle sentinel so the next
            # boot reports "watchdog hard-exit" instead of misclassifying
            # this as an unclean SIGKILL/OOM death (NS-608).
            try:
                from gateway.lifecycle_ledger import mark_exited
                mark_exited(exit_code, reason="loop_liveness_watchdog")
            except Exception:
                pass
            os._exit(exit_code)
            return

    thread = threading.Thread(
        target=_watchdog,
        daemon=True,
        name="gateway-loop-liveness-watchdog",
    )
    try:
        thread.start()
    except Exception:
        logger.debug("Failed to start gateway loop liveness watchdog", exc_info=True)
        return None
    return _LoopLivenessWatchdogHandle(stop_event, thread)


def _process_hermes_home() -> Path:
    """HERMES_HOME for process-level identity files (ignore profile overrides)."""
    val = os.environ.get("HERMES_HOME", "").strip()
    if val:
        return Path(val)
    return get_hermes_home()


def get_loop_heartbeat_path(home: Optional[Path] = None) -> Path:
    """Return ``<HERMES_HOME>/state/gateway.heartbeat``."""
    base = home if home is not None else _process_hermes_home()
    return base.joinpath(*_HEARTBEAT_RELATIVE)


def get_loop_tick_socket_path(
    home: Optional[Path] = None, pid: Optional[int] = None
) -> Path:
    """Return the loop-scheduling witness socket for ``pid``.

    ``<HERMES_HOME>/state/gateway.loop-tick.<pid>.sock`` — PID-suffixed so a
    leftover node from a previous process can never be mistaken for this
    gateway's witness. Served by the gateway loop itself (see
    ``_tick_socket_handler``): an answer is direct proof that the loop is
    dispatching, which is exactly the property the heartbeat file lost when
    its write moved off-loop (#90502).
    """
    base = home if home is not None else _process_hermes_home()
    return base.joinpath(
        "state", f"gateway.loop-tick.{int(pid if pid is not None else os.getpid())}.sock"
    )


def get_shutdown_watchdog_dump_path(home: Optional[Path] = None) -> Path:
    """Return the faulthandler / metadata dump path for a fired watchdog."""
    base = home if home is not None else _process_hermes_home()
    return base.joinpath(*_WATCHDOG_DUMP_RELATIVE)


def write_loop_heartbeat(
    *,
    pid: Optional[int] = None,
    start_time: Optional[float] = None,
    home: Optional[Path] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Path:
    """Atomically rewrite the loop-liveness heartbeat file.

    ``start_time`` is the gateway process start (``time.time()`` epoch seconds)
    so supervisors can detect PID reuse. Best-effort — never raises.
    """
    path = get_loop_heartbeat_path(home)
    payload: Dict[str, Any] = {
        "pid": int(pid if pid is not None else os.getpid()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "monotonic": time.monotonic(),
    }
    if start_time is not None:
        payload["start_time"] = float(start_time)
    # Embed a cheap memory sample (own RSS + MemAvailable + swap) so the
    # heartbeat doubles as a rolling pre-death telemetry snapshot: after an
    # unclean death (SIGKILL/OOM/VM loss) the last heartbeat is the closest
    # surviving record of memory pressure — see gateway.lifecycle_ledger
    # (NS-608).  Best-effort; <1ms of /proc reads on Linux, {} elsewhere.
    try:
        from gateway.lifecycle_ledger import sample_memory

        mem = sample_memory()
        if mem:
            payload["mem"] = mem
    except Exception:
        pass
    if extra:
        payload.update(extra)
    try:
        atomic_json_write(path, payload, indent=None)
    except Exception:
        logger.debug("Failed to write gateway loop heartbeat", exc_info=True)
    return path


def resolve_shutdown_watchdog_delay(
    drain_timeout: float,
    *,
    grace_s: float = DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S,
) -> float:
    """Return the wall-clock leash for the shutdown watchdog thread."""
    try:
        drain = max(float(drain_timeout), 0.0)
    except (TypeError, ValueError):
        drain = 0.0
    try:
        grace = max(float(grace_s), 0.0)
    except (TypeError, ValueError):
        grace = DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S
    return drain + grace


def _write_watchdog_dump(
    dump_path: Path,
    *,
    delay_s: float,
    snapshot: Optional[Dict[str, Any]],
) -> None:
    """Best-effort faulthandler + metadata dump before hard-exit."""
    try:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    header = {
        "event": "shutdown_watchdog_fired",
        "pid": os.getpid(),
        "delay_s": delay_s,
        "fired_at": datetime.now(timezone.utc).isoformat(),
        "snapshot": snapshot or {},
    }
    try:
        with open(dump_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(header, default=str) + "\n")
            fh.write("--- faulthandler dump (all threads) ---\n")
            fh.flush()
            try:
                faulthandler.dump_traceback(file=fh, all_threads=True)
            except Exception:
                fh.write("(faulthandler.dump_traceback failed)\n")
            fh.write("--- end dump ---\n")
            fh.flush()
    except Exception:
        pass

    # Also dump to stderr so journald/launchd capture it even if the file
    # write failed (wedged disk was one of the #66892 hypotheses).
    try:
        sys.stderr.write(
            f"Gateway shutdown watchdog fired after {delay_s:.0f}s "
            f"(pid={os.getpid()}); dumping all thread stacks.\n"
        )
        sys.stderr.flush()
        faulthandler.dump_traceback(all_threads=True)
    except Exception:
        pass


def arm_shutdown_watchdog(
    delay_s: float,
    *,
    done_event: Optional[threading.Event] = None,
    snapshot_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    exit_code: int = 1,
    dump_path: Optional[Path] = None,
    name: str = "gateway-shutdown-watchdog",
) -> Optional[threading.Event]:
    """Arm a daemon-thread hard-exit backstop for a wedged shutdown path.

    If ``done_event`` is set before ``delay_s`` elapses, the thread exits
    quietly (normal / progressing shutdown completed). Otherwise it dumps
    diagnostics and calls ``os._exit(exit_code)``.

    Never raises. Returns the ``done_event`` (creating one when omitted) so
    the caller can disarm on successful completion.

    🔴 Returns ``None`` when no backstop was armed, i.e. the thread start
    itself failed. ``threading.Thread.start`` raises ``RuntimeError: can't
    start new thread`` under thread/FD exhaustion or memory pressure —
    exactly the wedged-shutdown condition the watchdog exists for — and
    returning the ``done`` event regardless made that indistinguishable from
    success. A caller that REPLACES a live watchdog (see
    ``gateway.run._rearm_shutdown_watchdog``) then retires the running
    backstop in favour of a thread that does not exist, leaving the shutdown
    with no hard-exit at all: no dump, no ``mark_exited`` ledger entry, and
    no ordered PID-file / runtime-lock release before launchd's SIGKILL.
    Returning ``None`` is the only signal a never-raises API can give, so
    every replace-style caller MUST check it. Callers that merely ADD a
    backstop can keep ignoring the return: they already hold the event they
    passed in, and a ``None`` there means only that the optional backstop is
    absent, which is the pre-existing behaviour.

    The deliberate ``delay_s <= 0`` disable still returns the event: nothing
    was armed, but nothing was asked for either, so it is not a failure.
    """
    done = done_event if done_event is not None else threading.Event()
    try:
        delay = max(float(delay_s), 0.0)
    except (TypeError, ValueError):
        delay = DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S

    if delay <= 0:
        return done

    def _watchdog() -> None:
        # Wait with interruptible chunks so a late disarm doesn't need the
        # full remaining sleep to observe done_event.
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if done.wait(timeout=min(remaining, 1.0)):
                return
        if done.is_set():
            return

        snapshot: Optional[Dict[str, Any]] = None
        if snapshot_fn is not None:
            try:
                snapshot = snapshot_fn()
            except Exception as exc:
                snapshot = {"snapshot_error": repr(exc)}

        target = dump_path if dump_path is not None else get_shutdown_watchdog_dump_path()
        _write_watchdog_dump(target, delay_s=delay, snapshot=snapshot)

        try:
            logger.critical(
                "Shutdown watchdog fired after %.0fs — forcing process exit "
                "(asyncio drain path appears wedged; see %s)",
                delay,
                target,
            )
        except Exception:
            pass

        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        # Mirror _exit_after_graceful_shutdown: release PID file + runtime
        # lock BEFORE the log drain (locks must never be stranded), then
        # drain the async log queue so the logger.critical above actually
        # reaches the file before os._exit bypasses atexit. (#66892)
        try:
            from gateway.status import remove_pid_file, release_gateway_runtime_lock
            remove_pid_file()
            release_gateway_runtime_lock()
        except Exception:
            pass
        try:
            from hermes_logging import drain_log_queue
            drain_log_queue(timeout=1.0)
        except Exception:
            pass
        # Record the watchdog exit so the next boot's unclean-death detector
        # reports "shutdown watchdog fired" instead of SIGKILL/OOM (NS-608).
        try:
            from gateway.lifecycle_ledger import mark_exited
            mark_exited(exit_code, reason="shutdown_watchdog")
        except Exception:
            pass
        os._exit(exit_code)

    try:
        threading.Thread(target=_watchdog, daemon=True, name=name).start()
    except Exception:
        # Signal the failure to the caller. `logger.debug` alone left a
        # replace-style caller (the stop-path re-arm) unable to distinguish
        # "armed" from "no thread exists", so it retired the live backstop in
        # favour of nothing. This API never raises, so None IS the signal.
        logger.warning("Failed to arm shutdown watchdog", exc_info=True)
        return None
    return done


async def _tick_socket_handler(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Answer a liveness ping with one byte.

    Runs on the gateway loop: the reply is produced only while the loop is
    actually dispatching, so a successful read is a witness of loop
    schedulability that no executor thread and no filesystem stall can
    refresh. A UNIX-socket write is a socket-buffer copy — no fsync, no
    disk I/O — so the witness keeps working on the exact filesystem that
    stalls the heartbeat write. Best-effort; never raises.
    """
    try:
        writer.write(b"1")
        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def loop_heartbeat_forever(
    *,
    interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    start_time: Optional[float] = None,
    home: Optional[Path] = None,
    should_continue: Optional[Callable[[], bool]] = None,
) -> None:
    """Rewrite the loop heartbeat file on a cadence until cancelled / gated off.

    Runs as an asyncio task on the gateway loop — if the loop freezes, this task
    stops and the file mtime/updated_at goes stale for external monitors. That
    property is load-bearing and is preserved below: the write is still
    *initiated* by the loop, so a frozen loop still lets the file age.

    The write itself is handed to a thread, because it is not free. It ends in
    ``atomic_json_write`` -> ``os.fsync``, and on a filesystem that stalls, that
    fsync blocks whatever thread runs it. Doing it inline meant the loop-liveness
    watchdog's own heartbeat could block the loop it exists to monitor: the probe
    times out at ``DEFAULT_LOOP_WATCHDOG_TIMEOUT_S`` (10s) and gives up after
    ``DEFAULT_LOOP_WATCHDOG_MAX_STRIKES`` (3), a ~90-120s budget, while a WSL2
    VHDX under io pressure was measured stalling a trivial stat-and-fsync probe
    at p99 31s and max 112s. So the watchdog killed the loop for being
    unresponsive at the moment it was blocked inside the watchdog's own write.

    Awaited, not fire-and-forget: an unawaited task would keep the file fresh
    while the loop was wedged, which is exactly the signal the docstring above
    promises. And a single in-flight write at a time, so a 112s stall cannot pile
    up one queued thread per interval behind it.

    Because the write is now off-loop, file freshness is no longer *proof* of
    loop schedulability: a stalled write or a saturated executor can age the file
    while the loop runs, and a write that lands after the loop froze can keep it
    fresh. The file therefore stops being sufficient authority on its own. This
    task also arms a loop-scheduling witness — a UNIX socket answered by the
    loop itself (``_tick_socket_handler``) — and records whether it is armed in
    the heartbeat payload (``loop_tick_socket``). External probes must require
    the witness to agree with file staleness before classifying a loop as
    wedged; see ``hermes_cli.gateway.probe_gateway_loop_liveness`` for the
    two-witness contract.
    """
    try:
        interval = max(float(interval_s), 1.0)
    except (TypeError, ValueError):
        interval = DEFAULT_HEARTBEAT_INTERVAL_S

    # Arm the loop-scheduling witness. Best-effort: a failed bind (permissions,
    # path length) must not abort the gateway or the file heartbeat — it only
    # disables the witness, and the payload flag tells probes that staleness is
    # no longer sufficient authority to escalate.
    #
    # Windows: asyncio.start_unix_server raises (no AF_UNIX event-loop
    # support), so the witness is PERMANENTLY absent there — the payload
    # records loop_tick_socket=False and every stale-file probe classifies
    # UNKNOWN, never WEDGED. That is deliberate fail-safe: a wedged native
    # Windows gateway keeps the graceful-drain backstop instead of an
    # escalation verdict built on a witness that cannot exist. (WSL2 — the
    # #90502 incident environment — is Linux and arms the socket normally.)
    tick_server = None
    tick_socket_path = None
    try:
        tick_socket_path = get_loop_tick_socket_path(home)
        tick_socket_path.parent.mkdir(parents=True, exist_ok=True)
        # Re-bind over a leftover node from a dead process (os._exit(75) /
        # SIGKILL skip the finally-unlink; PID reuse re-lands on this
        # PID-suffixed path) is handled by asyncio itself:
        # create_unix_server os.remove()s an existing socket node before
        # binding — guarded by test_producer_rebinds_over_stale_socket_node.
        # What asyncio does NOT do is clean up SIBLING nodes from other
        # dead PIDs, so sweep those to keep state/ from accumulating
        # gateway.loop-tick.*.sock nodes across crash-restart cycles.
        # POSIX-only: os.kill(pid, 0) is a liveness probe here, but on
        # Windows os.kill calls TerminateProcess for non-CTRL signals —
        # and AF_UNIX server nodes are never created there anyway.
        if os.name == "posix":
            try:
                for _stale in tick_socket_path.parent.glob(
                    "gateway.loop-tick.*.sock"
                ):
                    if _stale == tick_socket_path:
                        continue
                    try:
                        _stale_pid = int(_stale.name.split(".")[-2])
                    except (ValueError, IndexError):
                        _stale.unlink(missing_ok=True)
                        continue
                    try:
                        os.kill(_stale_pid, 0)  # windows-footgun: ok — inside os.name == "posix" gate
                    except OSError:
                        _stale.unlink(missing_ok=True)
            except Exception:
                logger.debug(
                    "stale loop-tick socket sweep failed", exc_info=True
                )
        tick_server = await asyncio.start_unix_server(
            _tick_socket_handler, path=str(tick_socket_path)
        )
    except Exception:
        tick_server = None
        logger.warning(
            "Loop tick socket unavailable — liveness probes will have no "
            "loop-scheduling witness and will not escalate on a stale heartbeat",
            exc_info=True,
        )

    async def _write_off_loop() -> None:
        # write_loop_heartbeat never raises, so a failure here is an executor
        # problem (shutdown, saturation) and must not kill the heartbeat task.
        try:
            await asyncio.to_thread(
                write_loop_heartbeat,
                start_time=start_time,
                home=home,
                extra={"loop_tick_socket": tick_server is not None},
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Loop heartbeat write failed off-loop", exc_info=True)

    try:
        # Immediate first write so monitors see a fresh file as soon as the
        # gateway is running, not after the first interval.
        await _write_off_loop()
        while True:
            if should_continue is not None and not should_continue():
                return
            await asyncio.sleep(interval)
            if should_continue is not None and not should_continue():
                return
            await _write_off_loop()
    finally:
        if tick_server is not None:
            tick_server.close()
            try:
                await tick_server.wait_closed()
            except Exception:
                pass
            if tick_socket_path is not None:
                try:
                    tick_socket_path.unlink(missing_ok=True)
                except Exception:
                    pass
