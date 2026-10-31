"""The cron script runner: ticks the ``script`` lane (``no_agent`` jobs) outside the gateway.

Spec: Obsidian ``AI/Hermes Agent/Cron Script Runner Out Of Gateway — Spec (2026-10-27).md`` v1.0,
§5.4 (the runner process) and §5.5 (tick watchdog, one strike, code skew, drain). Entry:
``hermes cron runner``; re-arm after a watchdog trip: ``hermes cron runner rearm``.

The runner reuses ``InProcessCronScheduler`` with ``lane="script"``, no adapters (standalone
delivery), ``heartbeat=False`` (``ticker_heartbeat``/``ticker_last_success`` stay the gateway's
signal) and its own per-home heartbeat ``<home>/cron/script_runner_heartbeat``. It ticks a home only
when:

* every live gateway on the host booted on code that contains S1 (host precondition, D7);
* no watchdog trip marker exists (one strike, D12);
* ``cron.script_runner.enabled`` is a YAML ``true`` in the process home's config;
* that home's own gateway is absent, or live and publishing ``cron_script_lane_delegation == 1``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from cron import lanes

logger = logging.getLogger(__name__)

TRIP_MARKER_NAME = "script_runner_tripped"
EXIT_CONFIG = 78
EXIT_WATCHDOG = 70
_SHUTDOWN_WAIT_S = 20.0
# Bounded skew drain: after SIGKILL, how long to wait for each group to be reaped before its
# re-fire is withheld (t_c15baa4b).
_SKEW_CONFIRM_S = 2.0
_WARN_EVERY_S = 3600.0
_REFUSED_ENV = ("HERMES_MACHINE_ID", "_HERMES_CRON_EXTERNAL_WORKER")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def trip_marker_path() -> Path:
    """``<process home>/cron/script_runner_tripped`` (the process home, never a tick scope)."""
    from hermes_constants import get_process_hermes_home

    return get_process_hermes_home() / "cron" / TRIP_MARKER_NAME


def _home_label(home: Path) -> str:
    home = Path(home)
    return home.name if home.parent.name == "profiles" else "default"


def _detect_code_skew():
    try:
        from gateway.code_skew import detect_code_skew

        return detect_code_skew()
    except Exception:
        return None


def _inflight_count() -> int:
    from cron.scheduler import get_running_job_ids

    return len(get_running_job_ids())


def _inflight_refire_spent() -> List[tuple]:
    """``(job_id, elapsed_s)`` per in-flight run whose job could NOT be re-fired if killed now: the
    same test ``request_restart_requeue`` applies (a cron/interval job under ``RESTART_REQUEUE_MAX``).
    A run that is itself the re-fire, a job at the cap, a job on its final finite repetition, a
    one-shot or an unreadable job counts as spent, so the bounded skew drain waits for it instead of
    losing the run (Prism 05343ab9e749, cd1ea5071578)."""
    import cron.scheduler as sched
    from cron.jobs import get_job, restart_refire_refusal
    from cron.scheduler_provider import _profile_cron_scope

    with sched._running_lock:
        keys = sorted(sched._running_job_ids | sched._running_fire_owners.keys())
        since = {k: sched._running_since.get(k) for k in keys}
    now = time.time()
    spent = []
    for key in keys:
        home_key, job_id = key
        try:
            with _profile_cron_scope(sched._inflight_home_path(home_key)):
                job = get_job(job_id)
            # The in-flight run is not in repeat.completed yet: mark_job_run counts it.
            available = restart_refire_refusal(job, run_counted=False) is None
        except Exception:
            logger.debug("re-fire lookup failed for %s", job_id, exc_info=True)
            available = False
        if not available:
            started = since.get(key)
            spent.append((job_id, round(now - started) if started else None))
    return spent


def _boot_code_sha() -> Optional[str]:
    try:
        from gateway.code_skew import current_code_sha

        return current_code_sha()
    except Exception:
        return None


def _gateway_record(home: Path, liveness) -> Optional[dict]:
    if liveness.source == "multiplexer":
        return liveness.runtime if isinstance(liveness.runtime, dict) else None
    try:
        data = json.loads((Path(home) / "gateway_state.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def runner_gate_reason(name: str, home) -> Optional[str]:
    """None when the runner may tick ``home``, else the standby reason. Checks the knob, the trip
    marker, then the home's own gateway: absent → tick; live → tick only when its record names the
    liveness holder, is fresh and carries ``cron_script_lane_delegation == 1``. Any exception →
    standby (``probe_error``)."""
    if not lanes.knob_enabled():
        return "knob_disabled"
    if trip_marker_path().exists():
        return "watchdog_tripped"
    try:
        import gateway.status as status

        liveness = status.resolve_gateway_liveness(
            profile_dir=Path(home), use_cache=False,
            pid_probe=lambda p: status.get_running_pid(p, cleanup_stale=False))
        if not liveness.running:
            return None
        record = _gateway_record(Path(home), liveness)
        if (isinstance(record, dict) and liveness.pid is not None
                and int(record.get("pid") or -1) == int(liveness.pid)
                and not status.runtime_status_is_stale(record)
                and record.get("cron_script_lane_delegation") == 1):
            return None
        return "home_gateway_not_delegating"
    except Exception:
        logger.debug("runner gate probe failed for %s", name, exc_info=True)
        return "probe_error"


def runner_may_tick(name: str, home) -> bool:
    """The §5.4 per-home gate (after the host precondition)."""
    return runner_gate_reason(name, home) is None


class ScriptRunner:
    """Per-process runner state: the gate, the heartbeat writer, the tick watchdog and drain."""

    def __init__(self, *, profile_homes: Optional[Callable[[], list]] = None,
                 interval: Optional[float] = None, exit_fn: Callable[[int], Any] = os._exit,
                 root: Optional[Path] = None):
        if profile_homes is None:
            from cron.scheduler_ownership import cron_tick_profile_homes

            profile_homes = lambda: cron_tick_profile_homes(None)  # noqa: E731
        self._profile_homes = profile_homes
        self.interval = float(interval if interval is not None else lanes.RUNNER_CYCLE_S)
        self._exit_fn = exit_fn
        self._root = root
        self._stop: Optional[threading.Event] = None
        self.exit_code = 0
        self.host_failing: List[lanes.GatewayEntry] = []
        self.tripped = False
        self.draining = False
        self.draining_since: Optional[str] = None
        self._draining_mono = float("-inf")
        self.sigterm = False
        self._skew_bounded = False
        self._reasons: Dict[str, Optional[str]] = {}
        self._last_ok: Dict[str, str] = {}
        self._tick_started: Dict[str, str] = {}
        self._last_homes: List[tuple] = []
        self._warned: Dict[str, tuple] = {}
        self._recovered: set = set()
        # Set by a self re-arm; consumed by the first ADMITTED tick, which posts the receipt.
        self._rearmed: Optional[dict] = None
        self._timer: Optional[threading.Timer] = None
        self._deadline: Optional[threading.Timer] = None
        self._tick_thread: Optional[threading.Thread] = None
        self._pid = os.getpid()
        self._pid_start = self._own_start_time()
        self._code_sha = _boot_code_sha()

    @staticmethod
    def _own_start_time():
        try:
            from gateway.status import get_process_start_time

            return get_process_start_time(os.getpid())
        except Exception:
            return None

    # --- wiring ----------------------------------------------------------------------------

    def attach_stop(self, stop: threading.Event) -> None:
        self._stop = stop

    def start_kwargs(self, stop: threading.Event) -> dict:
        """Keyword arguments for ``InProcessCronScheduler.start`` (spec §5.4 entry)."""
        self.attach_stop(stop)
        return {
            "adapters": None, "loop": None, "interval": self.interval,
            "profile_homes": self.enumerate_homes, "profile_gate": self.profile_gate,
            "can_dispatch": self.can_dispatch, "lane": "script", "heartbeat": False,
            "startup_recovery": False, "on_home_cycle": self.on_home_cycle,
            "tick_hooks": (self.on_tick_start, self.on_tick_end),
        }

    def _warn_hourly(self, key: str, signature, message: str, *args) -> None:
        last = self._warned.get(key)
        now = time.monotonic()
        if last is not None and last[0] == signature and now - last[1] < _WARN_EVERY_S:
            return
        self._warned[key] = (signature, now)
        logger.warning(message, *args)

    # --- per-cycle checks (run once per cycle, before any home's gate) ----------------------

    def enumerate_homes(self) -> list:
        """The provider's ``profile_homes`` callable: run the cycle-level checks (code skew and
        drain, knob-off re-arm, trip marker, host precondition) and return
        the homes to consider."""
        homes = list(self._profile_homes())
        from cron.scheduler_provider import _existing_profile_homes, _profile_entry

        self._last_homes = [_profile_entry(e) for e in _existing_profile_homes(homes)]
        self._check_skew()
        if not lanes.knob_enabled():
            marker = trip_marker_path()
            if marker.exists():
                with contextlib.suppress(OSError):
                    marker.unlink()
                    logger.warning("script runner re-armed: cron.script_runner.enabled is false; "
                                   "trip marker removed")
        self.tripped = trip_marker_path().exists()
        if self.tripped:
            self._maybe_self_rearm()
        if self.tripped:
            self._warn_hourly("tripped", True, "script runner standby: watchdog tripped (%s); "
                              "self re-arm pending, or run 'hermes cron runner rearm'",
                              trip_marker_path())
        self._check_host()
        return homes

    def _maybe_self_rearm(self) -> None:
        """Delete the trip marker once it is ``rearm_delay_s(trips in 24 h)`` old (t_302ab1e8).
        The receipt waits for the first ADMITTED tick (``on_home_cycle``)."""
        from cron import script_runner_rearm as rearm_policy

        marker = trip_marker_path()
        try:
            age = time.time() - marker.stat().st_mtime
        except OSError:
            return
        trips = len(rearm_policy.recent_trips())
        delay = rearm_policy.rearm_delay_s(trips)
        if age < delay:
            return
        probe_s = self._ledger_probe_s()
        budget = lanes.RUNNER_TICK_WATCHDOG_S / 2
        if probe_s is None or probe_s > budget:
            self._warn_hourly("rearm_probe", None, "script runner self re-arm held: ledger probe "
                              "%s (budget %.0f s); lane stays parked",
                              "failed" if probe_s is None else f"took {probe_s:.1f} s", budget)
            return
        try:
            content = marker.read_text(encoding="utf-8-sig")
            marker.unlink()
        except OSError:
            logger.warning("script runner self re-arm: marker removal failed", exc_info=True)
            return
        try:
            info = json.loads(content)
        except ValueError:
            info = None
        if not isinstance(info, dict):
            info = {}
        self.tripped = False
        self._rearmed = {"trip_at": info.get("at"), "phase": info.get("phase"),
                         "age_s": round(age), "trips_24h": trips}
        logger.warning("script runner re-armed itself: trip at %s (%s) is %d s old, %d trip(s) in "
                       "24 h, delay %d s", info.get("at"), info.get("phase"), age, trips, delay)

    @staticmethod
    def _ledger_probe_s() -> Optional[float]:
        """Seconds for one ledger ``_transaction`` round-trip (connect + ``SELECT 1``), or None on
        error. Run on a daemon thread, joined for at most the watchdog budget, so a still-wedged
        ledger keeps the lane parked instead of re-tripping and killing in-flight jobs again."""
        from cron import executions

        out: List[Optional[float]] = [None]

        def probe() -> None:
            started = time.monotonic()
            try:
                with executions._transaction() as conn:
                    conn.execute("SELECT 1").fetchone()
                out[0] = time.monotonic() - started
            except Exception:
                logger.warning("script runner self re-arm: ledger probe failed", exc_info=True)

        t = threading.Thread(target=probe, name="script-runner-rearm-probe", daemon=True)
        started = time.monotonic()
        t.start()
        t.join(float(lanes.RUNNER_TICK_WATCHDOG_S))
        if t.is_alive():
            return time.monotonic() - started
        return out[0]

    def _check_skew(self) -> None:
        if not self.draining and _detect_code_skew() is not None:
            self.draining = True
            self.draining_since = _now_iso()
            self._draining_mono = time.monotonic()
            logger.warning("script runner draining: code skew; exiting 0 once in-flight scripts end "
                           "(at most %s s, then terminate + re-queue)", lanes.RUNNER_SKEW_DRAIN_MAX_S)
        if not self.draining:
            return
        if _inflight_count() == 0:
            self.exit_code = 0
            if self._stop is not None:
                self._stop.set()
        elif time.monotonic() - self._draining_mono >= float(lanes.RUNNER_SKEW_DRAIN_MAX_S):
            # Bounded drain (t_c15baa4b), only while every in-flight job can still be re-fired:
            # killing a run that is itself the re-fire (or a job at RESTART_REQUEUE_MAX) would lose
            # it until its next schedule, so then the drain waits as before (Prism 05343ab9e749).
            spent = _inflight_refire_spent()
            if spent:
                names = ", ".join(f"{job_id} {elapsed}s" for job_id, elapsed in spent)
                self._warn_hourly("skew_wait", tuple(j for j, _ in spent),
                                  "script runner code-skew drain waiting: re-fire spent, %s", names)
                return
            logger.warning("script runner code-skew drain bounded: re-fire available; %d script(s) "
                           "in flight past %s s, terminating them (one re-fire each) and exiting 0",
                           _inflight_count(), lanes.RUNNER_SKEW_DRAIN_MAX_S)
            self._skew_bounded = True
            self.request_shutdown()

    def _check_host(self) -> None:
        try:
            entries = lanes.host_gateways(self._root)
        except Exception:
            logger.warning("script runner: host gateway enumeration failed", exc_info=True)
            self.host_failing = [lanes.GatewayEntry(Path("?"), -1, None, None, True)]
            return
        self.host_failing = [e for e in entries if not lanes.code_contains_s1(e.code_sha)]
        if self.host_failing:
            names = sorted(f"{_home_label(e.home)} pid {e.pid} on {(e.code_sha or 'none')[:12]}"
                           for e in self.host_failing)
            self._warn_hourly("host", tuple(names),
                              "script runner standby: host precondition: %s", "; ".join(names))

    def _recover(self, home) -> None:
        """Startup recovery for one home, once, the first cycle the runner gates it in: never a
        home the runner does not own (knob off, tripped, host standby, a non-delegating gateway)."""
        from cron.scheduler_provider import InProcessCronScheduler, _profile_cron_scope

        key = str(home)
        if key in self._recovered:
            return
        self._recovered.add(key)
        try:
            with _profile_cron_scope(home):
                InProcessCronScheduler().recover_interrupted()
        except Exception:
            logger.error("script runner startup recovery failed for %s", home, exc_info=True)

    def profile_gate(self, name: str, home) -> bool:
        if self.draining or self.sigterm:
            reason = "draining"
        elif self.tripped:
            reason = "watchdog_tripped"
        elif self.host_failing:
            reason = "host_precondition"
        else:
            reason = runner_gate_reason(name, home)
        self._reasons[str(home)] = reason
        if reason is None:
            self._recover(home)
        return reason is None

    def can_dispatch(self) -> bool:
        return not (self.draining or self.sigterm or self.tripped)

    # --- heartbeat --------------------------------------------------------------------------

    def _write(self, home, state: str, reason: Optional[str] = None, outcome=None) -> None:
        key = str(home)
        lanes.write_runner_heartbeat(home, {
            "at": _now_iso(), "pid": self._pid, "pid_start": self._pid_start, "state": state,
            "standby_reason": reason if state == "standby" else None,
            "draining_since": self.draining_since if state == "draining" else None,
            "tick_started_at": self._tick_started.get(key),
            "last_ok_at": self._last_ok.get(key), "last_outcome": outcome,
            "code_sha": self._code_sha,
        })

    def on_home_cycle(self, name: str, home, gated_in: bool, outcome) -> None:
        """Once per existing home per cycle. ``last_ok_at`` advances only on a non-raising tick
        that reported ``ADMITTED``; every other outcome carries it forward."""
        key = str(home)
        if isinstance(outcome, lanes.TickOutcome):
            label = outcome.value
            if gated_in and outcome is lanes.TickOutcome.ADMITTED:
                self._last_ok[key] = _now_iso()
                if self._rearmed is not None:
                    self._post_rearm_receipt(home)
        elif isinstance(outcome, BaseException):
            label = type(outcome).__name__
        else:
            label = None
        if self.draining or self.sigterm:
            self._write(home, "draining", outcome=label)
        elif gated_in:
            self._write(home, "active", outcome=label)
        else:
            self._write(home, "standby", self._reasons.get(key) or "gated", outcome=label)

    def _post_rearm_receipt(self, home) -> None:
        """One #logs line for a self re-arm, on the first ADMITTED tick after it."""
        from cron import script_runner_rearm as rearm_policy

        info, self._rearmed = self._rearmed or {}, None
        rearm_policy.post_logs(
            f"🔁 cron script runner re-armed itself after a watchdog trip at {info.get('trip_at')} "
            f"({info.get('phase') or 'phase unknown'}); first tick on {_home_label(home)} under "
            f"budget. Trips in 24 h: {info.get('trips_24h')}. History: "
            f"{rearm_policy.trips_path()}")
        rearm_policy.clear_alerts()  # closes the 2nd-trip episode; ``quiet`` when none is open

    # --- tick watchdog (§5.5) --------------------------------------------------------------

    def on_tick_start(self, home) -> None:
        started = _now_iso()
        self._tick_thread = threading.current_thread()
        self._tick_started[str(home)] = started
        self._write(home, "active")
        timer = threading.Timer(float(lanes.RUNNER_TICK_WATCHDOG_S), self._watchdog_fire,
                                args=(home, started))
        timer.daemon = True
        timer.name = "cron-runner-watchdog"
        self._timer = timer
        timer.start()

    def on_tick_end(self, home) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
        self._tick_started.pop(str(home), None)

    def cancel_deadline(self) -> None:
        if self._deadline is not None:
            self._deadline.cancel()

    def _watchdog_fire(self, home, tick_started_at) -> None:
        grace = float(lanes.RUNNER_WATCHDOG_KILL_GRACE_S)
        hard_exit_at = time.monotonic() + grace + 2
        # 1. Hard deadline: the exit happens even if a step below blocks.
        self._deadline = threading.Timer(grace + 2, self._exit_fn, args=(EXIT_WATCHDOG,))
        self._deadline.daemon = True
        self._deadline.start()
        try:
            # The cheap marker lands before any stack walk, so a slow dump never costs the trip.
            self.tripped = True
            self._write_trip_marker(home, tick_started_at)
            diag = self._stack_diagnosis()
            logger.critical("script runner tick exceeded %s s on %s (tick thread in %s); tripping "
                            "(one strike) and exiting %d", lanes.RUNNER_TICK_WATCHDOG_S, home,
                            diag.get("phase"), EXIT_WATCHDOG)
            self._write_trip_marker(home, tick_started_at, diag)
            for _name, h in self._last_homes or [(None, home)]:
                with contextlib.suppress(Exception):
                    self._write(h, "standby", "watchdog_tripped")
            # Trip record + notice run beside the recovery, never ahead of it: a stalled history
            # write or sender start must not let the hard deadline fire before the requeue
            # snapshot and the tree kill (Prism eb08fba97ed9).
            notify = threading.Thread(target=self._record_and_notify_trip,
                                      args=(home, tick_started_at, diag), daemon=True,
                                      name="cron-runner-trip-notify")
            notify.start()
            # A script killed below is the runner's own doing, not a job failure: with the
            # scheduler marked shutting down, a worker that records the -15 before the exit takes
            # the restart-kill path (no failure page, one re-fire request), as on a gateway
            # restart drain (t_302ab1e8).
            with contextlib.suppress(Exception):
                import cron.scheduler as sched

                sched.signal_shutdown("cron script runner watchdog trip")
            # Before the kill: snapshot what was in flight while the pool still lists it.
            with contextlib.suppress(Exception):
                self._requeue_inflight()
            self._tree_kill(grace)
            self._await_inflight(min(time.monotonic() + 1.5, hard_exit_at - 0.5))
            notify.join(max(0.0, hard_exit_at - 0.3 - time.monotonic()))
            for handler in logging.getLogger().handlers:
                with contextlib.suppress(Exception):
                    handler.flush()
        finally:
            self._exit_fn(EXIT_WATCHDOG)

    def _record_and_notify_trip(self, home, tick_started_at, diag: dict) -> None:
        """Trip history line; #alerts page on the 2nd trip in 24 h (the 1st is self-healing and
        posts its #logs receipt when the re-armed runner ticks). Never raises."""
        try:
            from cron import script_runner_rearm as rearm_policy

            entry = {"epoch": time.time(), "at": _now_iso(), "home": str(home), "pid": self._pid,
                     "code_sha": self._code_sha, "tick_started_at": tick_started_at,
                     "phase": diag.get("phase"), "ledger": diag.get("ledger"),
                     "lock_waits": diag.get("lock_waits")}
            rearm_policy.record_trip(entry)
            trips = rearm_policy.recent_trips()
            if len(trips) >= 2:
                rearm_policy.page_alerts(
                    f"🔴 cron script runner watchdog: {len(trips)} trips in 24 h; latest "
                    f"{rearm_policy.trip_summary(entry)}. In-flight scripts were killed and "
                    f"re-queued; the gateway runs the script lane until the runner re-arms itself "
                    f"in {round(rearm_policy.rearm_delay_s(len(trips)) / 60)} min. History: "
                    f"{rearm_policy.trips_path()}")
        except Exception:
            logger.error("script runner: trip record/notify failed", exc_info=True)

    @staticmethod
    def _requeue_inflight() -> None:
        """Record every job in flight at the trip for one re-fire (t_302ab1e8). Not written to
        jobs.json here: the trip usually means the tick is stuck inside a ``_jobs_lock`` section
        waiting on the ledger, so the restarted runner applies the file
        (``script_runner_rearm.apply_pending_requeues``)."""
        import cron.scheduler as sched
        from cron import script_runner_rearm as rearm_policy

        # Stamped BEFORE the snapshot (Prism 29502ffb3d71), and each row carries its run's start
        # (Prism dbe5fc2fa076): a worker that already marked ``ok`` is still listed until its
        # ``finally`` releases it, so only a stamp no later than the run's start is safe.
        trip_epoch = time.time()
        got = sched._running_lock.acquire(timeout=0.2)
        try:
            keys = list(sched._running_job_ids)
            since = dict(sched._running_since)
            if not got:  # unlocked reads (Prism aa167090b0a2): no start = released = finished
                keys = [key for key in keys if key in since]
        except RuntimeError:
            keys, since = [], {}
        finally:
            if got:
                sched._running_lock.release()
        rearm_policy.record_pending_requeues(
            [(str(sched._inflight_home_path(key[0])), key[1], since.get(key)) for key in keys],
            trip_epoch=trip_epoch, lock_timeout=1.0)

    @staticmethod
    def _await_inflight(deadline: float) -> None:
        """Give killed scripts' workers until ``deadline`` to record their run before the exit."""
        with contextlib.suppress(Exception):
            while _inflight_count() and time.monotonic() < deadline:
                time.sleep(0.05)

    def _stack_diagnosis(self) -> dict:
        """Every thread's stack plus the tick thread's innermost ``cron.*`` frame (``phase``), so a
        trip names the call that blocked (t_f2e71f40: the 2026-10-28 trip left no clue). Also the
        recent ``_jobs_lock`` waits. Never raises."""
        diag: Dict[str, Any] = {"phase": None, "tick_thread": None, "threads": {}}
        try:
            names = {t.ident: t.name for t in threading.enumerate()}
            tick = self._tick_thread
            diag["tick_thread"] = tick.name if tick is not None else None
            for ident, frame in sys._current_frames().items():
                name = names.get(ident, f"thread-{ident}")
                if name in diag["threads"]:
                    name = f"{name}#{ident}"
                # A plain frame walk: traceback's extractors stat/read source via linecache
                # (checkcache stats even with lookup_lines=False), which hangs on a stalled mount.
                frames = []
                f = frame
                while f is not None:
                    frames.append(f"{f.f_code.co_filename}:{f.f_lineno} in {f.f_code.co_name}")
                    f = f.f_back
                diag["threads"][name] = frames[::-1]
                if tick is not None and ident == tick.ident:
                    f = frame
                    while f is not None:
                        module = f.f_globals.get("__name__", "")
                        if module.startswith("cron.") and module != __name__:
                            diag["phase"] = f"{module}:{f.f_code.co_name}:{f.f_lineno}"
                            break
                        f = f.f_back
            diag["lock_waits"] = [[label, round(s, 3)] for label, s in lanes.lock_waits()[-20:]]
            # Which thread holds the ledger lock and what every ledger transaction is doing
            # (t_302ab1e8). Only when already imported: a trip must not import anything.
            executions = sys.modules.get("cron.executions")
            if executions is not None:
                diag["ledger"] = executions.ledger_activity()
        except Exception as exc:  # noqa: BLE001 - diagnostics must not block the trip
            diag["error"] = f"{type(exc).__name__}: {exc}"
        return diag

    def _write_trip_marker(self, home, tick_started_at, diag: Optional[dict] = None) -> None:
        from utils import atomic_write_text

        try:
            atomic_write_text(trip_marker_path(), json.dumps({
                "at": _now_iso(), "home": str(home), "pid": self._pid,
                "code_sha": self._code_sha, "tick_started_at": tick_started_at,
                **(diag or {}),
            }, sort_keys=True) + "\n", tmp_prefix=".script_runner_tripped_", mode=0o600)
        except Exception:
            logger.error("script runner: trip marker write failed; the restart will be armed",
                         exc_info=True)

    @staticmethod
    def _snapshot_script_procs() -> list:
        import cron.scheduler as sched

        got = sched._script_procs_lock.acquire(blocking=False)
        try:
            if got:
                return list(sched._active_script_procs.values())
            logger.warning("script runner watchdog: procs lock busy; snapshotting unlocked")
            try:
                return list(sched._active_script_procs.values())
            except RuntimeError:
                return list(sched._active_script_procs.values())
        finally:
            if got:
                sched._script_procs_lock.release()

    def _tree_kill(self, grace: float) -> None:
        """SIGTERM every registered script's process group, wait ≤ ``grace``, SIGKILL the rest."""
        # Scripts start with start_new_session=True, so each group id is its leader's pid. The group
        # is resolved WITHOUT poll() first: a leader that exited while a background child keeps its
        # pipes is an unreaped zombie, its pid (= the pgid) cannot be reused, and the survivors
        # must still be signalled. Only a leader already reaped (returncode set) is skipped: its
        # pid may now name an unrelated process.
        groups = []
        for proc in self._snapshot_script_procs():
            try:
                if proc.returncode is not None:
                    continue
                pgid = proc.pid
                os.killpg(pgid, signal.SIGTERM)  # windows-footgun: ok — POSIX-only runner (macOS LaunchAgent, spec §2)
                groups.append((proc, pgid))
            except Exception:
                logger.debug("watchdog SIGTERM failed", exc_info=True)

        def alive(item) -> bool:
            _proc, pgid = item
            try:
                os.killpg(pgid, 0)  # windows-footgun: ok — POSIX-only runner (macOS LaunchAgent, spec §2)
            except ProcessLookupError:
                return False
            except Exception:
                return True
            return True

        deadline = time.monotonic() + grace
        while groups and time.monotonic() < deadline:
            groups = [g for g in groups if alive(g)]
            if groups:
                time.sleep(0.05)
        for _proc, pgid in groups:
            with contextlib.suppress(Exception):
                os.killpg(pgid, signal.SIGKILL)  # windows-footgun: ok — POSIX-only runner (macOS LaunchAgent, spec §2)

    def _skew_kill(self) -> None:
        """Bounded skew drain (Prism 9dde2d9f29f3, dbd507fb38d7, 96d1a12310f7): snapshot each
        script's whole tree BEFORE signalling, TERM it, wait ≤ ``RUNNER_SKEW_KILL_GRACE_S``, KILL the
        survivors through the timeout/cancel tree-kill, then confirm (a zombie-only tree is gone).
        Held under ``_script_procs_lock`` so no run can set or consume its re-fire flag meanwhile; a
        tree still alive afterwards has its re-fire withheld (keyed per profile, Prism f4526d2bb9fa),
        because the re-fire would run beside the live copy."""
        import cron.scheduler as sched
        from cron import scheduler_script as ss

        with sched._script_procs_lock:
            sched.signal_shutdown("cron script runner code-skew drain bound")
            trees = [(key, proc, ss._script_tree_snapshot(proc))
                     for key, proc in sched._active_script_procs.items() if proc.returncode is None]
            # returncode stays None until the worker reaps its script, so an exited run reads as in
            # flight. Keep only trees alive on a fresh check (Prism 934ec5f4046f): a zombie-only
            # tree finished on its own and keeps its own result, with no interruption flag.
            trees = [(key, proc, ss._script_tree_refresh(proc, snap)) for key, proc, snap in trees]
            # An unreadable same-user process (Prism 952be3262777) only withholds a re-fire for a run
            # cut off here; it never makes a run that already finished on its own count as cut off.
            trees = [t for t in trees if ss._script_tree_alive(
                t[1], None if t[2] is None else [m for m in t[2]
                                                 if not isinstance(m, ss._UnreadableMember)])]
            # Every run cut off here is a failure flagged for its re-fire, whatever it exits with
            # (Prism b86087faa72f); consumed by _run_job_script once the run returns.
            sched._skew_interrupted_job_ids.update(
                {sched._script_proc_job_ids.get(key) for key, _p, _s in trees} - {None})
            for _key, proc, snap in trees:
                ss._signal_script_tree(proc, snap, signal.SIGTERM)
            alive = trees
            deadline = time.monotonic() + float(lanes.RUNNER_SKEW_KILL_GRACE_S)
            while True:
                alive = [t for t in alive if ss._script_tree_alive(t[1], t[2])]
                if not alive or time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
            # Re-snapshot before KILL and before the confirm: a descendant forked during the TERM
            # grace is in neither the first snapshot nor the reaped leader's tree (Prism 361a15209668).
            trees = [(key, proc, ss._script_tree_refresh(proc, snap)) for key, proc, snap in trees]
            alive = [t for t in trees if ss._script_tree_alive(t[1], t[2])]
            for _key, proc, snap in alive:
                ss._signal_script_tree(proc, snap, signal.SIGKILL)
            alive = [(key, proc, ss._script_tree_refresh(proc, snap)) for key, proc, snap in alive]
            for _key, proc, snap in alive:
                ss._signal_script_tree(proc, snap, signal.SIGKILL)
            deadline = time.monotonic() + _SKEW_CONFIRM_S
            while True:
                alive = [t for t in alive if ss._script_tree_alive(t[1], t[2])]
                if not alive or time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            withheld = {sched._script_proc_job_ids.get(key) for key, _p, _s in alive} - {None}
            sched._refire_withheld_job_ids.update(withheld)
            sched._restart_killed_job_ids.difference_update(withheld)
        if alive:
            logger.error("script runner code-skew drain: %d script tree(s) survived SIGKILL (%s); "
                         "re-fire withheld for %s", len(alive),
                         ", ".join(str(proc.pid) for _k, proc, _s in alive),
                         ", ".join(sorted(f"{j}@{h}" for h, j in withheld)) or "-")

    # --- SIGTERM shutdown (§5.4) -----------------------------------------------------------

    def request_shutdown(self) -> None:
        self.sigterm = True
        if self._stop is not None:
            self._stop.set()

    def shutdown(self) -> int:
        """Heartbeat ``draining`` → ``signal_shutdown`` → ``terminate_running_scripts`` → wait ≤ 20 s
        for in-flight runs to record (and request their one re-fire) → 0."""
        import cron.scheduler as sched

        self.draining_since = self.draining_since or _now_iso()
        for _name, home in self._last_homes:
            with contextlib.suppress(Exception):
                self._write(home, "draining")
        if self._skew_bounded:
            self._skew_kill()
        else:
            sched.signal_shutdown("cron script runner SIGTERM")
            sched.terminate_running_scripts("cron script runner SIGTERM")
        deadline = time.monotonic() + _SHUTDOWN_WAIT_S
        while _inflight_count() and time.monotonic() < deadline:
            time.sleep(0.1)
        return 0


def rearm() -> int:
    """``hermes cron runner rearm``: delete the trip marker and print its content."""
    marker = trip_marker_path()
    try:
        content = marker.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        print("cron script runner: not tripped (no marker)")
        return 0
    marker.unlink(missing_ok=True)
    print(f"cron script runner re-armed; removed {marker}:\n{content.rstrip()}")
    return 0


def _bootstrap_env() -> None:
    """The gateway's child-visible start-path env (spec §5.6): ``.env``, the config bridge, the
    platform env bridges, ``HERMES_EXEC_ASK``/``TERMINAL_CWD`` and ``HERMES_QUIET``. ``_HERMES_GATEWAY``
    goes into each script child's env only (``process_bootstrap.apply_child_env``)."""
    from hermes_cli.process_bootstrap import bootstrap_cron_parent_env
    from hermes_constants import get_process_hermes_home

    bootstrap_cron_parent_env(get_process_hermes_home() / "config.yaml", role="runner")


def _setup_logging() -> None:
    from hermes_constants import get_process_hermes_home
    from hermes_logging import setup_logging

    setup_logging(hermes_home=get_process_hermes_home(), mode="cron")
    # launchd captures stderr into ~/.hermes/logs/cron-script-runner.log.
    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(logging.WARNING)
    stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(stream)


def main(argv: Optional[list] = None) -> int:
    """``hermes cron runner``. Exit 78: config refusal; 70: watchdog trip; 0: SIGTERM or skew."""
    from cron.scheduler_provider import InProcessCronScheduler, resolve_cron_scheduler

    _setup_logging()
    _bootstrap_env()
    for key in _REFUSED_ENV:
        if key in os.environ:
            logger.error("cron script runner refusing to start: %s is set (I6: no outbox, no "
                         "shared claim identity)", key)
            return EXIT_CONFIG
    if not isinstance(resolve_cron_scheduler(), InProcessCronScheduler):
        logger.error("cron script runner refusing to start: cron.provider is not the built-in "
                     "in-process scheduler")
        return EXIT_CONFIG
    with contextlib.suppress(Exception):
        from gateway.code_skew import record_boot_fingerprint

        record_boot_fingerprint()
    runner = ScriptRunner()
    with contextlib.suppress(Exception):
        # Jobs a watchdog trip killed get their one re-fire now, from a process whose locks are
        # fresh (the tripped one could not take ``_jobs_lock``; t_302ab1e8).
        from cron import script_runner_rearm

        script_runner_rearm.apply_pending_requeues()
    stop = threading.Event()
    kwargs = runner.start_kwargs(stop)
    signal.signal(signal.SIGTERM, lambda *_a: runner.request_shutdown())
    homes = list(runner._profile_homes())
    with contextlib.suppress(Exception):
        from hermes_logging import enable_profile_log_routing

        enable_profile_log_routing([home for _name, home in homes])
    logger.warning(
        "cron script runner started: pid %d, lane script, %d home(s) %s, knob=%s, "
        "precondition S1=%s, tripped=%s", os.getpid(), len(homes),
        [name for name, _home in homes], lanes.knob_enabled(), (lanes.S1_MERGE_SHA or "")[:12],
        trip_marker_path().exists())
    thread = threading.Thread(target=InProcessCronScheduler().start, args=(stop,), kwargs=kwargs,
                              name="cron-script-runner", daemon=True)
    thread.start()
    while not stop.wait(0.5):
        if not thread.is_alive():
            logger.error("cron script runner ticker thread exited; exiting 1")
            return 1
    if runner.sigterm:
        return runner.shutdown()
    thread.join(timeout=_SHUTDOWN_WAIT_S)
    return runner.exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
