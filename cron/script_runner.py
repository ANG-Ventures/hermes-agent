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
import subprocess
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
# Land hold before the bounded skew drain kills (t_3c08d829): the fleet's home helper, relative to
# the fleet root. It publishes the restart-pending record (the home-reconcile tick starts no new
# land), waits while a live land marker exists (bounded by SGR_LAND_WAIT_SECS) and always exits 0.
_LAND_WAIT_HELPER = ("skills-shared", "hermes-harness", "safe-gateway-restart", "scripts",
                     "land_wait.py")
# Past the helper's own bound (SGR_LAND_WAIT_SECS) the runner stops waiting for it and kills it.
_LAND_WAIT_SLACK_S = 30.0
# The exit-path release of the restart-pending record is one file unlink; never hold the exit long.
_LAND_RELEASE_TIMEOUT_S = 10.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def trip_marker_path() -> Path:
    """``<process home>/cron/script_runner_tripped`` (the process home, never a tick scope)."""
    from hermes_constants import get_process_hermes_home

    return get_process_hermes_home() / "cron" / TRIP_MARKER_NAME


def _home_label(home: Path) -> str:
    home = Path(home)
    return home.name if home.parent.name == "profiles" else "default"


EXIT_RECORD_NAME = "script_runner_exit.json"


def _exit_record_path() -> Path:
    return trip_marker_path().parent / EXIT_RECORD_NAME


def note_start() -> None:
    """Log an exit-reason line for a previous runner that left none (SIGKILL, launchd exit timeout,
    hard crash), then record this start (t_2ddabbc4: every start in the log has a cause). Never
    raises."""
    try:
        path = _exit_record_path()
        try:
            prev = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            prev = None
        if isinstance(prev, dict) and prev.get("exit") is None and prev.get("pid") != os.getpid():
            logger.warning("cron script runner exit: reason=unrecorded code=? pid %s (started %s; "
                           "no exit line: SIGKILL, launchd exit timeout or a hard crash)",
                           prev.get("pid"), prev.get("started_at"))
        from utils import atomic_write_text

        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({"pid": os.getpid(), "started_at": _now_iso(),
                                            "exit": None}) + "\n",
                          tmp_prefix=".script_runner_exit_", mode=0o600)
    except Exception:
        logger.debug("script runner: exit record start failed", exc_info=True)


def note_exit(reason: str, code) -> None:
    """One ``cron script runner exit: reason=<r> code=<n>`` line on every exit path, and the exit
    record that tells the next start this one was logged. Never raises."""
    try:
        logger.warning("cron script runner exit: reason=%s code=%s pid %d", reason, code,
                       os.getpid())
        for handler in logging.getLogger().handlers:
            with contextlib.suppress(Exception):
                handler.flush()
        from utils import atomic_write_text

        path = _exit_record_path()
        try:
            record = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            record = None
        if not isinstance(record, dict) or record.get("pid") != os.getpid():
            record = {"pid": os.getpid(), "started_at": None}
        record["exit"] = {"at": _now_iso(), "reason": reason, "code": code}
        atomic_write_text(path, json.dumps(record) + "\n", tmp_prefix=".script_runner_exit_",
                          mode=0o600)
    except Exception:
        logger.debug("script runner: exit record failed", exc_info=True)


def _detect_code_skew():
    try:
        from gateway.code_skew import detect_code_skew

        return detect_code_skew()
    except Exception:
        return None


def _skew_shas() -> tuple:
    """``(project root, boot sha, disk sha)``; a sha is None when unresolved."""
    from gateway import code_skew

    boot = code_skew._boot_fingerprint
    boot_sha = boot.rsplit(":", 1)[-1] if boot else None
    if boot_sha == "unresolved":
        boot_sha = None
    return code_skew._PROJECT_ROOT, boot_sha, code_skew.current_code_sha()


def _loaded_repo_files(root: Path) -> set:
    """Repo-relative posix paths of every module file in ``sys.modules`` under ``root``."""
    root = Path(root).resolve()
    out = set()
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if not path:
            continue
        try:
            out.add(Path(path).resolve().relative_to(root).as_posix())
        except (ValueError, OSError):
            continue
    return out


def _skew_loaded_changes(base: Optional[str] = None) -> Optional[List[str]]:
    """Loaded module files changed between ``base`` (the last accepted sha; default the boot sha)
    and the disk sha (t_2ddabbc4): ``[]`` when the diff touches none of them, None when the diff
    cannot be read (the caller drains)."""
    try:
        root, boot, disk = _skew_shas()
        base = base or boot
        if not base or not disk or base == disk:
            return None
        # --no-renames: a renamed loaded module must show its old path.
        p = subprocess.run(["git", "-C", str(root), "diff", "--name-only", "--no-renames",
                            f"{base}..{disk}"], stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=10,
                           check=False)
        if p.returncode != 0:
            return None
        changed = {line.strip() for line in p.stdout.splitlines() if line.strip()}
        return sorted(changed & _loaded_repo_files(root))
    except Exception:
        return None


def _real_members(snapshot: Optional[list]) -> Optional[list]:
    """``snapshot`` without scan markers (``_SCAN_MARKERS``): the members that prove a running copy
    and may be signalled. None stays None (the process-group fallback)."""
    from cron import scheduler_script as ss

    return None if snapshot is None else [m for m in snapshot if not isinstance(m, ss._SCAN_MARKERS)]


def _inflight_count() -> int:
    from cron.scheduler import get_running_job_ids

    return len(get_running_job_ids())


def _inflight_refire_spent() -> List[tuple]:
    """``(job_id, elapsed_s)`` per in-flight run that holds the bounded skew drain."""
    return _inflight_refire_status()[0]


def _inflight_refire_status() -> tuple:
    """``(spent, failed_keys)``. ``spent``: ``(job_id, elapsed_s)`` per in-flight run whose job
    could NOT be re-fired if killed now (the test ``request_restart_requeue`` applies) and whose last
    run was ``ok`` or never recorded. A run that is itself the re-fire, a job at the cap, a job on
    its final finite repetition, a one-shot or an unreadable job counts, so the bounded skew drain
    waits for it instead of losing the run (Prism 05343ab9e749, cd1ea5071578).
    ``failed_keys``: in-flight keys of re-fire-spent jobs whose last run failed. They do not hold
    the drain (t_af461294): killing one more run of an already-red streak loses nothing, and their
    wait held every deploy for up to the script timeout. The bounded kill withholds their re-fire."""
    import cron.scheduler as sched
    from cron.jobs import get_job, restart_refire_refusal
    from cron.scheduler_provider import _profile_cron_scope

    with sched._running_lock:
        keys = sorted(sched._running_job_ids | sched._running_fire_owners.keys())
        since = {k: sched._running_since.get(k) for k in keys}
    now = time.time()
    spent, failed = [], set()
    for key in keys:
        home_key, job_id = key
        job = None
        try:
            with _profile_cron_scope(sched._inflight_home_path(home_key)):
                job = get_job(job_id)
            # The in-flight run is not in repeat.completed yet: mark_job_run counts it.
            available = restart_refire_refusal(job, run_counted=False) is None
        except Exception:
            logger.debug("re-fire lookup failed for %s", job_id, exc_info=True)
            available = False
        if available:
            continue
        if job is not None and job.get("last_status") not in ("ok", None):
            failed.add(key)
            continue
        started = since.get(key)
        spent.append((job_id, round(now - started) if started else None))
    return spent, failed


def _inflight_finish_requested() -> List[tuple]:
    """``(job_id, elapsed_s, max_s)`` per in-flight run whose row sets ``drain_finish_s`` and has
    run for less than that (t_fa0b5cff): the bounded skew drain waits for it instead of cutting it
    at ``RUNNER_SKEW_DRAIN_MAX_S``. A run past its own maximum is cut like any other."""
    import cron.scheduler as sched
    from cron.jobs import drain_finish_seconds, get_job
    from cron.scheduler_provider import _profile_cron_scope

    with sched._running_lock:
        keys = sorted(sched._running_job_ids | sched._running_fire_owners.keys())
        since = {k: sched._running_since.get(k) for k in keys}
    now = time.time()
    out = []
    for key in keys:
        home_key, job_id = key
        try:
            with _profile_cron_scope(sched._inflight_home_path(home_key)):
                max_s = drain_finish_seconds(get_job(job_id))
        except Exception:
            logger.debug("drain-finish lookup failed for %s", job_id, exc_info=True)
            continue
        started = since.get(key)
        if max_s is None or started is None:
            continue
        elapsed = now - started
        if elapsed < max_s:
            out.append((job_id, round(elapsed), max_s))
    return out


def _land_wait_helper() -> Path:
    """``<fleet root>/skills-shared/.../land_wait.py``; the fleet root is the process home with a
    ``profiles/<p>`` suffix stripped."""
    from hermes_constants import get_process_hermes_home

    home = get_process_hermes_home()
    if home.parent.name == "profiles":
        home = home.parent.parent
    return home.joinpath(*_LAND_WAIT_HELPER)


def _land_wait_budget_s() -> float:
    try:
        return float(os.environ.get("SGR_LAND_WAIT_SECS", "1200")) + _LAND_WAIT_SLACK_S
    except ValueError:
        return 1200.0 + _LAND_WAIT_SLACK_S


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
        # The last disk sha accepted without a drain (None = the boot sha). Later skews diff
        # against it, so a commit that reverts code imported after the accept still drains.
        self._skew_base_sha: Optional[str] = None
        self.exit_reason: Optional[str] = None
        self._skew_withhold: set = set()
        self._bound_polled = False
        # Land hold (t_3c08d829): None = not started; a Popen = helper running; True = settled.
        self._land_hold: Any = None
        self._land_hold_deadline = 0.0
        self._land_hold_spawned = False  # a helper was started, so a record may exist
        self._land_hold_started = float("-inf")  # monotonic start of the helper, a timestamp only
        self._land_hold_lock = threading.Lock()
        self._reasons: Dict[str, Optional[str]] = {}
        self._last_ok: Dict[str, str] = {}
        self._tick_started: Dict[str, str] = {}
        self._last_homes: List[tuple] = []
        self._warned: Dict[str, tuple] = {}
        self._recovered: set = set()
        # Set by a self re-arm; consumed by the first ADMITTED tick, which posts the receipt.
        self._rearmed: Optional[dict] = None
        # The first ADMITTED tick of this process closes a still-open trip episode (t_b6454aff).
        self._episode_cleared = False
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
        if not self.draining:
            # A gated re-fire waits for the drain-killed tree to go (t_fa0b5cff); checked every
            # cycle so it fires within one cycle of that, not at the next restart.
            with contextlib.suppress(Exception):
                from cron import script_runner_rearm as rearm_policy

                rearm_policy.apply_gated_refires()
        if not lanes.knob_enabled():
            marker = trip_marker_path()
            if marker.exists():
                with contextlib.suppress(OSError):
                    marker.unlink()
                    logger.warning("script runner re-armed: cron.script_runner.enabled is false; "
                                   "trip marker removed")
        self.tripped = trip_marker_path().exists()
        if self.tripped:
            # A trip seen by this process opens a new episode: the next ADMITTED tick closes it
            # again even if the marker goes away off the self-rearm path (Prism ce116ff61d38).
            self._episode_cleared = False
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

    def _skew_pending(self) -> bool:
        """Disk differs from the code this process runs: the boot sha, or the last sha accepted
        without a drain (also when the checkout returns to the boot sha)."""
        if self._skew_base_sha is None:
            return _detect_code_skew() is not None
        try:
            disk = _skew_shas()[2]
        except Exception:
            disk = None
        if disk is None:
            return _detect_code_skew() is not None
        return disk != self._skew_base_sha

    def _skew_needs_drain(self) -> bool:
        """A code skew drains only when the base..disk diff touches a module this process loaded,
        or the diff cannot be read (t_2ddabbc4: 84 of 152 drains changed no loaded module). The
        base is the last accepted sha, so a kept skew is diffed once and a later revert of code
        imported from it still drains."""
        try:
            disk = _skew_shas()[2]
        except Exception:
            disk = None
        changed = _skew_loaded_changes(self._skew_base_sha)
        if changed is None:
            logger.warning("script runner code skew: base..disk diff unreadable; draining")
            return True
        if changed:
            logger.warning("script runner code skew touches %d loaded module(s): %s", len(changed),
                           ", ".join(changed[:10]) + (" ..." if len(changed) > 10 else ""))
            return True
        self._skew_base_sha = disk
        logger.warning("script runner code skew: disk %s changes no loaded module; not draining",
                       (disk or "?")[:12])
        return False

    def _check_skew(self) -> None:
        if not self.draining and self._skew_pending() and self._skew_needs_drain():
            self.draining = True
            self.draining_since = _now_iso()
            self._draining_mono = time.monotonic()
            logger.warning("script runner draining: code skew; exiting 0 once in-flight scripts end "
                           "(at most %s s, then terminate + re-queue)", lanes.RUNNER_SKEW_DRAIN_MAX_S)
        if not self.draining:
            return
        if _inflight_count() == 0:
            self.exit_code = 0
            self.exit_reason = self.exit_reason or "code skew drain (no script in flight)"
            if self._stop is not None:
                self._stop.set()
        else:
            self._check_skew_bound()

    def poll_skew_bound(self) -> None:
        """Called from ``main``'s 0.5 s stop poll: evaluate the drain bound once, the moment it is
        crossed. The ticker cycle (``RUNNER_CYCLE_S`` = 60) alone can land a few ms short of it and
        hold the drain another whole cycle (t_af461294 drill: 180 s, not 120 s)."""
        if self.draining and not self.sigterm and self._land_hold not in (None, True):
            # A land hold is running: proceed within 0.5 s of the helper's exit (t_3c08d829).
            if self._land_hold.poll() is not None or time.monotonic() >= self._land_hold_deadline:
                self._check_skew_bound()
            return
        if (not self.draining or self._bound_polled or self.sigterm
                or time.monotonic() - self._draining_mono < float(lanes.RUNNER_SKEW_DRAIN_MAX_S)):
            return
        self._bound_polled = True
        if _inflight_count():
            self._check_skew_bound()

    def _check_skew_bound(self) -> None:
        if time.monotonic() - self._draining_mono >= float(lanes.RUNNER_SKEW_DRAIN_MAX_S):
            # Bounded drain (t_c15baa4b), only while every in-flight job can still be re-fired:
            # killing a run that is itself the re-fire (or a job at RESTART_REQUEUE_MAX) would lose
            # it until its next schedule, so then the drain waits as before (Prism 05343ab9e749).
            spent, failed = _inflight_refire_status()
            if spent:
                names = ", ".join(f"{job_id} {elapsed}s" for job_id, elapsed in spent)
                self._warn_hourly("skew_wait", tuple(j for j, _ in spent),
                                  "script runner code-skew drain waiting: re-fire spent, %s", names)
                return
            finishing = _inflight_finish_requested()
            if finishing:
                names = ", ".join(f"{job_id} {elapsed}s of {max_s}s"
                                  for job_id, elapsed, max_s in finishing)
                self._warn_hourly("skew_finish", tuple(j for j, _e, _m in finishing),
                                  "script runner code-skew drain waiting: finish requested, %s",
                                  names)
                return
            if not self._land_hold_settled():
                return
            logger.warning("script runner code-skew drain bounded: re-fire available; %d script(s) "
                           "in flight past %s s, terminating them (one re-fire each) and exiting 0; "
                           "re-fire withheld for re-fire-spent failing job(s): %s",
                           _inflight_count(), lanes.RUNNER_SKEW_DRAIN_MAX_S,
                           ", ".join(sorted(j for _h, j in failed)) or "-")
            self._skew_withhold = failed
            self._skew_bounded = True
            self.request_shutdown("code skew drain bounded (in-flight scripts terminated)")

    def _land_hold_settled(self) -> bool:
        """Hold the bounded kill while a home-reconcile land is writing the live tree (t_3c08d829).

        A kill inside that write leaves the tick's land-killed latch, and every later tick refuses
        until an operator clears it. Starts ``land_wait.py default --owner <runner pid>`` once
        (non-blocking: the ticker and main's 0.5 s poll come back here) and returns True once it
        has exited. A missing helper, a start failure, a non-zero exit or a helper past
        ``SGR_LAND_WAIT_SECS`` + slack never blocks the drain: one log line, then True. The
        restart-pending record it leaves is owned by this pid; ``main`` removes it on exit
        (``release_land_hold``). ``SGR_LAND_WAIT=0`` makes the helper return at once."""
        with self._land_hold_lock:
            hold = self._land_hold
            if hold is True:
                return True
            if hold is None:
                helper = _land_wait_helper()
                if not helper.is_file():
                    logger.warning("script runner code-skew drain: land hold skipped: no helper at "
                                   "%s", helper)
                    self._land_hold = True
                    return True
                try:
                    self._land_hold = subprocess.Popen(
                        [sys.executable, str(helper), "default", "--owner", str(os.getpid())],
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, start_new_session=True)
                except Exception as exc:  # noqa: BLE001 - the hold must never block the drain
                    logger.warning("script runner code-skew drain: land hold skipped: helper "
                                   "start failed (%s: %s)", type(exc).__name__, exc)
                    self._land_hold = True
                    return True
                self._land_hold_spawned = True
                self._land_hold_started = time.monotonic()
                self._land_hold_deadline = self._land_hold_started + _land_wait_budget_s()
                return False
            rc = hold.poll()
            if rc is None and time.monotonic() < self._land_hold_deadline:
                return False
            if rc is None:
                with contextlib.suppress(Exception):
                    os.killpg(hold.pid, signal.SIGKILL)  # windows-footgun: ok — POSIX-only runner (macOS LaunchAgent, spec §2)
                with contextlib.suppress(Exception):
                    hold.wait(5)
            out = ""
            with contextlib.suppress(Exception):
                out = (hold.stdout.read() or b"").decode("utf-8", "replace").strip()
                hold.stdout.close()
            waited = round(time.monotonic() - self._land_hold_started)
            if rc is None:
                logger.warning("script runner code-skew drain: land hold helper still running "
                               "after %d s; killed it, proceeding%s", waited,
                               f" ({out})" if out else "")
            elif rc != 0:
                logger.warning("script runner code-skew drain: land hold helper exited %d after "
                               "%d s; proceeding%s", rc, waited, f" ({out})" if out else "")
            elif out:
                logger.warning("script runner code-skew drain: land hold %d s: %s", waited,
                               " | ".join(out.splitlines()))
            self._land_hold = True
            return True

    def end_land_hold(self) -> None:
        """Kill a still-running land-hold helper on exit (the drain ended without a kill), and reap
        it so it cannot publish its record after ``release_land_hold`` removed it."""
        hold = self._land_hold
        if hold not in (None, True) and hold.poll() is None:
            with contextlib.suppress(Exception):
                os.killpg(hold.pid, signal.SIGKILL)  # windows-footgun: ok — POSIX-only runner (macOS LaunchAgent, spec §2)
            with contextlib.suppress(Exception):
                hold.wait(5)

    def release_land_hold(self) -> None:
        """Remove this runner's restart-pending record: ``land_wait.py default --release --owner
        <pid>``. ``main`` calls it as the LAST exit step, after in-flight scripts were terminated
        (until then the record is what stops a new land). Best effort: a missing helper, a failure
        or a timeout logs one line and never blocks or fails the exit.

        The record cannot be left to lapse with the runner: the tick reads a record whose owner pid
        is alive as live, and on the Studio the pid space wraps in minutes (427 pids/s measured
        2026-11-11), so a reused pid would hold every land until the record is 1800 s old.
        Residual: a runner SIGKILLed (launchd exit timeout) before this step leaves the record for
        up to 1800 s."""
        if not self._land_hold_spawned:
            return  # the helper never started: no record was published
        helper = _land_wait_helper()
        try:
            p = subprocess.run(
                [sys.executable, str(helper), "default", "--release", "--owner", str(os.getpid())],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=_LAND_RELEASE_TIMEOUT_S,
                check=False)
        except Exception as exc:  # noqa: BLE001 - the release must never block or fail the exit
            logger.warning("script runner: land hold release failed (%s: %s)",
                           type(exc).__name__, exc)
            return
        out = ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "replace").strip()
        if p.returncode != 0:
            logger.warning("script runner: land hold release exited %d%s", p.returncode,
                           f" ({out})" if out else "")
        elif out:
            logger.warning("script runner: land hold release: %s", " | ".join(out.splitlines()))

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
                InProcessCronScheduler().recover_interrupted(replay_pass=False)
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
                elif not self._episode_cleared:
                    # A re-arm off the self-rearm path (`hermes cron runner rearm`, a hand
                    # unlink, the knob toggle) also ends the trip; close any open episode once.
                    # Gated on ANY recorded trip, not the 24 h window: a runner parked longer
                    # than the window still has an open episode (Prism ce116ff61d38).
                    from cron import script_runner_rearm as rearm_policy

                    if rearm_policy.trips_path().exists():
                        rearm_policy.clear_alerts()
                self._episode_cleared = True
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
            # The history line is one O_APPEND write on THIS thread: on the notify thread it lost
            # the race with the exit on 3 of 8 live trips (t_db4c9214).
            entry = self._trip_entry(home, tick_started_at, diag)
            with contextlib.suppress(Exception):
                from cron import script_runner_rearm as rearm_policy

                rearm_policy.record_trip(entry)
            self._write_trip_marker(home, tick_started_at, diag)
            for _name, h in self._last_homes or [(None, home)]:
                with contextlib.suppress(Exception):
                    self._write(h, "standby", "watchdog_tripped")
            # The notice runs beside the recovery, never ahead of it: a stalled sender start must
            # not let the hard deadline fire before the requeue snapshot and the tree kill
            # (Prism eb08fba97ed9).
            notify = threading.Thread(target=self._notify_trip, args=(entry,), daemon=True,
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
            note_exit("watchdog trip", EXIT_WATCHDOG)
            for handler in logging.getLogger().handlers:
                with contextlib.suppress(Exception):
                    handler.flush()
        finally:
            self._exit_fn(EXIT_WATCHDOG)

    def _trip_entry(self, home, tick_started_at, diag: dict) -> dict:
        return {"epoch": time.time(), "at": _now_iso(), "home": str(home), "pid": self._pid,
                "code_sha": self._code_sha, "tick_started_at": tick_started_at,
                "phase": diag.get("phase"), "ledger": diag.get("ledger"),
                "lock_waits": diag.get("lock_waits")}

    def _notify_trip(self, entry: dict) -> None:
        """#alerts page on the 2nd trip in 24 h (the 1st is self-healing and posts its #logs
        receipt when the re-armed runner ticks); ``entry`` is already in the history. Never
        raises."""
        try:
            from cron import script_runner_rearm as rearm_policy

            rearm_policy.trim_trips()
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

        with sched._running_lock:  # run starts, before any worker can release its key
            since = dict(sched._running_since)
        with sched._script_procs_lock:
            sched.signal_shutdown("cron script runner code-skew drain bound")
            # Re-fire-spent jobs whose last run failed were not waited for (t_af461294): their cut
            # run is recorded as a failure with no re-fire.
            sched._refire_withheld_job_ids.update(self._skew_withhold)
            sched._refire_spent_withheld_job_ids.update(self._skew_withhold)
            trees = [(key, proc, ss._script_tree_snapshot(proc))
                     for key, proc in sched._active_script_procs.items() if proc.returncode is None]
            # returncode stays None until the worker reaps its script, so an exited run reads as in
            # flight. Keep only trees alive on a fresh check (Prism 934ec5f4046f): a zombie-only
            # tree finished on its own and keeps its own result, with no interruption flag.
            trees = [(key, proc, ss._script_tree_refresh(proc, snap)) for key, proc, snap in trees]
            # A scan marker (an unreadable same-user process, Prism 952be3262777, or a failed token
            # scan) only withholds a re-fire; it never makes a run that already finished on its own
            # count as cut off (Prism dae50bba8939). A tree alive ONLY through markers is not flagged
            # or signalled, but it is still checked at the end and its re-fire withheld.
            cut, unsure = [], []
            for t in trees:
                if ss._script_tree_alive(t[1], _real_members(t[2])):
                    cut.append(t)
                elif ss._script_tree_alive(t[1], t[2]):
                    unsure.append(t)
            trees = cut
            # Every run cut off here is a failure flagged for its re-fire, whatever it exits with
            # (Prism b86087faa72f); consumed by _run_job_script once the run returns.
            sched._skew_interrupted_job_ids.update(
                {sched._script_proc_job_ids.get(key) for key, _p, _s in trees} - {None})
            alive = self._skew_terminate(trees)
            # An "unsure" tree a later refresh confirms (a real member alive, not only a scan
            # marker) is a running copy: it goes through the same TERM/KILL (Prism 662cc8e0a1ae).
            # One still alive only through markers is never signalled; its re-fire is withheld.
            unsure = [(key, proc, ss._script_tree_refresh(proc, snap)) for key, proc, snap in unsure]
            confirmed = [t for t in unsure if ss._script_tree_alive(t[1], _real_members(t[2]))]
            unsure = [t for t in unsure if not any(t is c for c in confirmed)]
            if confirmed:
                sched._skew_interrupted_job_ids.update(
                    {sched._script_proc_job_ids.get(key) for key, _p, _s in confirmed} - {None})
                alive += self._skew_terminate(confirmed)
            alive += [t for t in unsure if ss._script_tree_alive(t[1], t[2])]
            # The confirmed pass can spend a whole TERM/KILL grace; a first-pass survivor that exited
            # meanwhile must not withhold its re-fire (Prism ecfb7394bfc6). Recheck on a fresh scan.
            alive = [(key, proc, ss._script_tree_refresh(proc, snap)) for key, proc, snap in alive]
            alive = [t for t in alive if ss._script_tree_alive(t[1], t[2])]
            withheld = {sched._script_proc_job_ids.get(key) for key, _p, _s in alive} - {None}
            sched._refire_withheld_job_ids.update(withheld)
            sched._restart_killed_job_ids.difference_update(withheld)
            # Withheld here only DEFERS the re-fire (t_fa0b5cff): it used to drop it for good, so
            # sub-vps-backup (2026-11-11 02:55, "12 script tree(s) survived SIGKILL") never re-ran.
            # Record the surviving members; the next runner re-fires once they are gone or the
            # run's own script timeout has passed. A spent failing job stays withheld.
            gates = [(sched._script_proc_job_ids.get(key), self._gate_members(proc, snap))
                     for key, proc, snap in alive
                     if sched._script_proc_job_ids.get(key) in withheld
                     and sched._script_proc_job_ids.get(key) not in self._skew_withhold]
        if gates:
            self._record_gated_refires(gates, since)
        if alive:
            logger.error("script runner code-skew drain: %d script tree(s) survived SIGKILL (%s); "
                         "re-fire withheld for %s", len(alive),
                         ", ".join(str(proc.pid) for _k, proc, _s in alive),
                         ", ".join(sorted(f"{j}@{h}" for h, j in withheld)) or "-")

    @staticmethod
    def _gate_members(proc, snap) -> dict:
        """``{"members": [[pid, create_time]...], "unsure": [...]}`` for a survivor tree's
        still-running members. ``unsure`` are ``_UnreadableMember`` hits: a same-user orphan with
        an unreadable environ that MAY be ours (on macOS every launchd job has ppid 1, so a long
        run collects unrelated ones; the likely source of 02:55's "12 survived"). They hold the
        re-fire only for ``RUNNER_GATED_UNSURE_S``; a real member holds it to the run's deadline.
        ``unproven``: a failed scan, or nothing re-identifiable in a tree that was alive; the
        re-fire then waits for the run's deadline (``until``), never less."""
        import psutil
        from cron import scheduler_script as ss

        out: Dict[str, Any] = {"members": [], "unsure": [], "unproven": False}
        for member in snap or []:
            if isinstance(member, ss._UnprovenMember):
                out["unproven"] = True  # a failed scan: an escapee cannot be ruled out
                continue
            unsure = isinstance(member, ss._UnreadableMember)
            target = getattr(member, "_process", member)
            try:
                if member.pid <= 0 or not target.is_running() \
                        or target.status() == psutil.STATUS_ZOMBIE:
                    continue
                out["unsure" if unsure else "members"].append(
                    [int(member.pid), float(target.create_time())])
            except Exception:
                out["unproven"] = True  # alive at the check, unidentifiable now
                continue
        if snap is None:
            try:
                out["members"].append([int(proc.pid),
                                       float(psutil.Process(proc.pid).create_time())])
            except Exception:
                out["unproven"] = True
        if not out["members"] and not out["unsure"]:
            # The tree was alive at the final check but nothing in it can be re-identified: never
            # read that as gone (Prism 28e1906cda71, 68af07493f6b). Hold to the run's deadline.
            out["unproven"] = True
        return out

    @staticmethod
    def _record_gated_refires(gates: list, since: dict) -> None:
        """Persist one gated re-fire per survivor job: it fires once its members are gone or the
        run's own script timeout (from the run's start) has passed. Never raises."""
        import cron.scheduler as sched
        from cron import script_runner_rearm as rearm_policy

        rows = []
        now = time.time()
        for key, members in gates:
            try:
                home = sched._inflight_home_path(key[0])
                from cron.jobs import get_job
                from cron.scheduler_provider import _profile_cron_scope

                with _profile_cron_scope(home):
                    timeout = int(sched._job_script_kwargs(get_job(key[1]) or {})
                                  .get("timeout_seconds") or 0)
                    if timeout <= 0:
                        from cron.scheduler_script import _get_script_timeout
                        timeout = int(_get_script_timeout())
                started = since.get(key) or now
                rows.append({"home": str(home), "job_id": key[1], "start": started,
                             "until": max(started + timeout, now),
                             "unsure_until": now + float(lanes.RUNNER_GATED_UNSURE_S),
                             **members})
            except Exception:
                logger.error("script runner: gated re-fire for %s not recorded", key,
                             exc_info=True)
        rearm_policy.record_gated_refires(rows)

    @staticmethod
    def _skew_terminate(trees: list) -> list:
        """TERM every tree, wait ≤ ``RUNNER_SKEW_KILL_GRACE_S``, re-snapshot (a descendant forked
        during the TERM grace, Prism 361a15209668), KILL the survivors twice, confirm ≤
        ``_SKEW_CONFIRM_S``. Returns the trees still alive."""
        from cron import scheduler_script as ss

        for _key, proc, snap in trees:
            ss._signal_script_tree(proc, snap, signal.SIGTERM)
        alive = trees
        deadline = time.monotonic() + float(lanes.RUNNER_SKEW_KILL_GRACE_S)
        while True:
            alive = [t for t in alive if ss._script_tree_alive(t[1], t[2])]
            if not alive or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        trees = [(key, proc, ss._script_tree_refresh(proc, snap)) for key, proc, snap in trees]
        alive = [t for t in trees if ss._script_tree_alive(t[1], t[2])]
        kill_sig = getattr(signal, "SIGKILL", signal.SIGTERM)
        for _key, proc, snap in alive:
            ss._signal_script_tree(proc, snap, kill_sig)
        alive = [(key, proc, ss._script_tree_refresh(proc, snap)) for key, proc, snap in alive]
        for _key, proc, snap in alive:
            ss._signal_script_tree(proc, snap, kill_sig)
        deadline = time.monotonic() + _SKEW_CONFIRM_S
        while True:
            alive = [t for t in alive if ss._script_tree_alive(t[1], t[2])]
            if not alive or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        return alive

    # --- SIGTERM shutdown (§5.4) -----------------------------------------------------------

    def request_shutdown(self, reason: str = "signal SIGTERM") -> None:
        self.exit_reason = self.exit_reason or reason
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
    """``hermes cron runner``. Exit 78: config refusal; 70: watchdog trip; 0: SIGTERM or skew.
    Every exit path logs one ``cron script runner exit: reason=...`` line (t_2ddabbc4)."""
    _setup_logging()
    note_start()
    exit_info: Dict[str, Any] = {"reason": None, "code": None}
    try:
        code = _run(exit_info)
        exit_info["code"] = code
        return code
    except BaseException as exc:
        exit_info["reason"] = f"crash {type(exc).__name__}: {exc}"[:300]
        exit_info["code"] = "exception"
        raise
    finally:
        note_exit(exit_info["reason"] or "unknown", exit_info["code"])


def _run(exit_info: Dict[str, Any]) -> int:
    from cron.scheduler_provider import InProcessCronScheduler, resolve_cron_scheduler

    _bootstrap_env()
    for key in _REFUSED_ENV:
        if key in os.environ:
            logger.error("cron script runner refusing to start: %s is set (I6: no outbox, no "
                         "shared claim identity)", key)
            exit_info["reason"] = f"config refused ({key} set)"
            return EXIT_CONFIG
    if not isinstance(resolve_cron_scheduler(), InProcessCronScheduler):
        logger.error("cron script runner refusing to start: cron.provider is not the built-in "
                     "in-process scheduler")
        exit_info["reason"] = "config refused (cron.provider is not in-process)"
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
        # Drain-killed runs whose tree outlived SIGKILL re-fire once it is gone (t_fa0b5cff).
        script_runner_rearm.apply_gated_refires()
    stop = threading.Event()
    kwargs = runner.start_kwargs(stop)
    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):  # windows-footgun: ok — POSIX-only runner (macOS LaunchAgent, spec §2)
        signal.signal(signum, lambda num, _f: runner.request_shutdown(
            f"signal {signal.Signals(num).name}"))
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
    try:
        while not stop.wait(0.5):
            if not thread.is_alive():
                logger.error("cron script runner ticker thread exited; exiting 1")
                exit_info["reason"] = "ticker thread exited"
                runner.end_land_hold()
                return 1
            runner.poll_skew_bound()
        runner.end_land_hold()
        exit_info["reason"] = runner.exit_reason or "stop requested"
        if runner.sigterm:
            return runner.shutdown()
        thread.join(timeout=_SHUTDOWN_WAIT_S)
        return runner.exit_code
    finally:
        # Last step, after shutdown() terminated the scripts (t_3c08d829 round 2).
        runner.release_land_hold()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
