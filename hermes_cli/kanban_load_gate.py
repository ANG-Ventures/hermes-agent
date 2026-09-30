"""Projected-load admission gate for the kanban dispatcher.

Config: ``kanban.dispatch_load_gate`` in config.yaml.

History
-------
2026-09-24: a plain load1 hysteresis gate (pause above ``ncpu``, resume
below ``0.75 * ncpu``) was added after load1 80-110 on the 32-core Studio.

2026-09-25: that gate was LAG-BLIND. load1 is a 1-minute EMA; a worker's
pytest/tool fan-out lands 60-90 s after spawn. Measured in gateway.log:
``spawns RESUMED - load1=20.0`` at 06:30:55 followed by ``spawned=29`` at
06:31:35, then ``PAUSED load1=55`` a minute later; RESUMED at 19.1 ->
``spawned=26``. 66 workers / load1 146 on 32 cores. The gate re-admitted a
whole backlog on every quiet sample.

The fix is admission against PROJECTED load, not current load:

* every worker spawned in the last ``ramp_seconds`` is counted as
  ``worker_load_cost`` of load that load1 has not shown yet (2026-09-29
  revision below: measured cost, linear fade, 600 s);
* a tick admits at most ``(pause_above - load1 - pending_ramp) / cost``
  new workers (min 0), and never more than ``max_spawn_per_tick`` (default 4);
* resuming from a hard pause additionally requires ``load5 < pause_above``
  (``load5_floor``, default true) so one quiet load1 sample cannot re-open
  the gate while the 5-minute average still says the host is saturated.

2026-09-29 (t_bf26e8f1): two more defects, both measured on the Studio.

* The ramp model was too short. A worker's load kept arriving for 5-10 min
  after spawn (task_runs vs load1, 16:00-17:40 PT), but ``pending_ramp``
  dropped every spawn after 120 s. Now each spawn is booked at the MEASURED
  load-per-worker slope, fading linearly over ``ramp_seconds`` (default
  600): the share still pending is the share load1 has not shown yet, so
  nothing is counted twice. The slope is a least-squares fit of load1 on
  the visible running-worker count over the last ``slope_window_seconds``
  (default 600), clamped to ``[worker_load_cost_min, worker_load_cost_max]``;
  ``worker_load_cost`` is the prior when the window has too little
  variation. Allowance is floor(headroom / cost), so it stops short of
  ``pause_above``. It does not overshoot by one.
* load1 alone paused the gate for 2 h with 20 idle cores. load1 was 250
  with the CPU 64% idle: 54 leaked headless-Chrome processes plus a
  background-QoS run queue on the 8 E-cores (t_4d2b7d38, t_55e1beaa, which
  measured about 0.02 cores per worker). A load1 pause now needs CPU
  corroboration. If the host CPU is less than ``cpu_busy_pause`` busy
  (default 0.70), the gate admits against CPU headroom
  (``worker_cpu_cost`` cores per worker, default 1.0, 50x the measured
  cost), still capped per tick. The state is ``cpu_headroom``. With no CPU
  sample, behaviour is the load1-only gate as before.

``kanban.max_spawn`` stays the hard concurrency ceiling; this gate is the
governor that decides how fast the host is allowed to approach it.

The class is pure and clock-injectable: feed ``admit(load1, load5=, now=)``
each tick, then ``record_spawns(n, now=)`` with what was actually spawned.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

DEFAULT_WORKER_LOAD_COST = 2.0
DEFAULT_WORKER_LOAD_COST_MIN = 0.5
DEFAULT_WORKER_LOAD_COST_MAX = 4.0
DEFAULT_RAMP_SECONDS = 600.0
DEFAULT_SLOPE_WINDOW_SECONDS = 600.0
DEFAULT_CPU_BUSY_PAUSE = 0.70
DEFAULT_WORKER_CPU_COST = 1.0
SLOPE_MIN_SAMPLES = 5
SLOPE_MIN_X_STDEV = 1.0
SLOPE_MIN_R2 = 0.5
DEFAULT_MAX_SPAWN_PER_TICK = 4
SUMMARY_INTERVAL_SECONDS = 300.0
STATE_FILENAME = "load_gate.json"


def _num(value: Any, default: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return float(default)
    return f if f > 0 else float(default)


def _pos_int(value: Any, default: int) -> int:
    try:
        i = int(value)
    except (TypeError, ValueError):
        return int(default)
    return i if i >= 1 else int(default)


class LoadGate:
    """Hysteresis + projected-load admission gate for dispatcher SPAWNS.

    Never reclaims or kills anything: it only decides how many NEW workers a
    tick may start. See the module docstring for the incidents it encodes.
    """

    def __init__(self, cfg: Optional[dict], ncpu: int) -> None:
        cfg = cfg if isinstance(cfg, dict) else {}
        self.enabled = bool(cfg.get("enabled", True))
        ncpu = max(1, int(ncpu or 1))
        self.pause_above = _num(cfg.get("pause_above"), float(ncpu))
        self.resume_below = _num(cfg.get("resume_below"), 0.75 * ncpu)
        if self.resume_below >= self.pause_above:
            # A degenerate band would flap; collapse to a sane one.
            self.resume_below = 0.75 * self.pause_above
        self.worker_load_cost = _num(
            cfg.get("worker_load_cost"), DEFAULT_WORKER_LOAD_COST
        )
        self.ramp_seconds = _num(cfg.get("ramp_seconds"), DEFAULT_RAMP_SECONDS)
        self.max_spawn_per_tick = _pos_int(
            cfg.get("max_spawn_per_tick"), DEFAULT_MAX_SPAWN_PER_TICK
        )
        self.load5_floor = bool(cfg.get("load5_floor", True))
        self.worker_load_cost_min = _num(
            cfg.get("worker_load_cost_min"), DEFAULT_WORKER_LOAD_COST_MIN
        )
        self.worker_load_cost_max = max(
            self.worker_load_cost_min,
            _num(cfg.get("worker_load_cost_max"), DEFAULT_WORKER_LOAD_COST_MAX),
        )
        self.slope_window_seconds = _num(
            cfg.get("slope_window_seconds"), DEFAULT_SLOPE_WINDOW_SECONDS
        )
        self.cpu_corroborate = bool(cfg.get("cpu_corroborate", True))
        busy = _num(cfg.get("cpu_busy_pause"), DEFAULT_CPU_BUSY_PAUSE)
        self.cpu_busy_pause = busy if busy <= 1.0 else DEFAULT_CPU_BUSY_PAUSE
        self.worker_cpu_cost = _num(
            cfg.get("worker_cpu_cost"), DEFAULT_WORKER_CPU_COST
        )
        self.ncpu = ncpu
        self.paused = False
        self.reason: Optional[str] = None
        # Observable state (snapshot / logs / diagnostics).
        self.state = "admitting"
        self.load1: Optional[float] = None
        self.load5: Optional[float] = None
        self.pending_ramp = 0.0
        self.cost: float = self.worker_load_cost  # load per worker in use
        self.cost_source = "prior"               # "prior" | "measured"
        self.running: Optional[int] = None
        self.cpu_busy: Optional[float] = None
        self._samples: deque = deque()  # (now, load1, visible_workers)
        self._cpu_prev = None
        self.allowance: Optional[int] = None
        self.admitted_last_tick = 0
        self.last_reason: Optional[str] = None
        self._ramp: deque = deque()  # (monotonic_ts, n_spawned)
        self._last_logged_state: Optional[str] = None
        self._last_summary_at: Optional[float] = None
        self._admitted_since_summary = 0
        # Per-board split of the last tick (gateway dispatcher, t_f78d1938):
        # {slug: {ready, quota, spawned, starved_since}}.
        self.boards: dict = {}

    # -- hysteresis (hard pause) -------------------------------------------
    def update(self, load1: float, load5: Optional[float] = None) -> Optional[str]:
        """Feed one sample; return the HARD pause reason (None = not paused).

        Pauses when ``load1 > pause_above``; resumes only when
        ``load1 < resume_below`` and (with ``load5_floor``) ``load5 <
        pause_above``.
        """
        if not self.enabled:
            self.paused, self.reason = False, None
            return None
        try:
            load1 = float(load1)
        except (TypeError, ValueError):
            return self.reason
        try:
            load5 = float(load5) if load5 is not None else None
        except (TypeError, ValueError):
            load5 = None
        if self.paused:
            load5_ok = (
                not self.load5_floor or load5 is None or load5 < self.pause_above
            )
            if load1 < self.resume_below and load5_ok:
                self.paused, self.reason = False, None
        elif load1 > self.pause_above:
            self.paused = True
        if self.paused:
            tail = ""
            if self.load5_floor and load5 is not None:
                tail = f" and load5 below {self.pause_above:.1f} (load5={load5:.1f})"
            self.reason = (
                f"load1={load1:.1f} > pause_above={self.pause_above:.1f} "
                f"(ncpu={self.ncpu}); resumes below {self.resume_below:.1f}{tail}"
            )
        return self.reason

    # -- projected-load admission ------------------------------------------
    def _prune(self, now: float) -> None:
        horizon = now - self.ramp_seconds
        while self._ramp and self._ramp[0][0] <= horizon:
            self._ramp.popleft()

    def invisible_workers(self, now: Optional[float] = None) -> float:
        """Recent spawns whose load has not reached load1 yet.

        A spawn ``age`` seconds old counts ``1 - age/ramp_seconds``: its load
        arrives linearly over the ramp (measured 5-10 min on the Studio).
        """
        now = time.monotonic() if now is None else float(now)
        self._prune(now)
        return sum(
            n * max(0.0, 1.0 - (now - ts) / self.ramp_seconds)
            for ts, n in self._ramp
        )

    def pending(self, now: Optional[float] = None) -> float:
        """Load not yet visible in load1: invisible workers x cost."""
        return self.invisible_workers(now) * self.cost

    def measured_cost(self, now: Optional[float] = None) -> "Optional[float]":
        """Least-squares load1-per-visible-worker slope over the window.

        ``None`` while the window is too short, the worker count barely
        moved (a flat x has no slope), or the fit explains under half the
        load variance (r^2 < 0.5). Clamped to the configured band: a
        negative slope (outside load rising while workers finish) must not
        make workers look free.
        """
        now = time.monotonic() if now is None else float(now)
        horizon = now - self.slope_window_seconds
        while self._samples and self._samples[0][0] < horizon:
            self._samples.popleft()
        pts = list(self._samples)
        if len(pts) < SLOPE_MIN_SAMPLES:
            return None
        if pts[-1][0] - pts[0][0] < self.slope_window_seconds / 2:
            return None
        n = len(pts)
        mx = sum(p[2] for p in pts) / n
        my = sum(p[1] for p in pts) / n
        sxx = sum((p[2] - mx) ** 2 for p in pts)
        if (sxx / n) ** 0.5 < SLOPE_MIN_X_STDEV:
            return None
        sxy = sum((p[2] - mx) * (p[1] - my) for p in pts)
        syy = sum((p[1] - my) ** 2 for p in pts)
        # Load that moves with something other than the workers (a leaked
        # browser, backupd) gives a slope with no fit behind it; use the prior.
        if syy <= 0 or (sxy * sxy) / (sxx * syy) < SLOPE_MIN_R2:
            return None
        slope = sxy / sxx
        return min(self.worker_load_cost_max, max(self.worker_load_cost_min, slope))

    def observe(self, load1: float, running: Optional[int], now: float) -> None:
        """Record one (load1, visible workers) point for the slope fit."""
        if running is None:
            return
        try:
            running = int(running)
        except (TypeError, ValueError):
            return
        self.running = running
        visible = max(0.0, running - self.invisible_workers(now))
        self._samples.append((now, float(load1), visible))

    def admit(
        self,
        load1: float,
        *,
        load5: Optional[float] = None,
        now: Optional[float] = None,
        running: Optional[int] = None,
        cpu_busy: Optional[float] = None,
    ) -> "tuple[Optional[int], Optional[str]]":
        """Return ``(allowance, reason)`` for this tick.

        ``allowance`` is the max number of NEW workers this tick may spawn
        (``None`` = gate disabled, no limit). ``reason`` is non-None exactly
        when ``allowance == 0`` and explains why. ``running`` (workers
        running on the host) feeds the load-per-worker slope; ``cpu_busy``
        (0..1 of all cores) corroborates a load1 pause. Both are optional.
        """
        if not self.enabled:
            self.state, self.allowance, self.reason = "disabled", None, None
            self.last_reason = None
            return None, None
        try:
            load1 = float(load1)
        except (TypeError, ValueError):
            return None, None
        now = time.monotonic() if now is None else float(now)
        self.load1 = load1
        try:
            self.load5 = float(load5) if load5 is not None else None
        except (TypeError, ValueError):
            self.load5 = None
        try:
            self.cpu_busy = (
                min(1.0, max(0.0, float(cpu_busy))) if cpu_busy is not None else None
            )
        except (TypeError, ValueError):
            self.cpu_busy = None
        self.running = None
        self.observe(load1, running, now)
        measured = self.measured_cost(now)
        self.cost = measured if measured is not None else self.worker_load_cost
        self.cost_source = "measured" if measured is not None else "prior"
        hard = self.update(load1, self.load5)
        self.pending_ramp = self.pending(now)
        if hard:
            if (
                self.cpu_corroborate
                and self.cpu_busy is not None
                and self.cpu_busy < self.cpu_busy_pause
            ):
                return self._admit_on_cpu(now, hard)
            self.state, self.allowance, self.last_reason = "paused", 0, hard
            return 0, hard
        headroom = self.pause_above - load1 - self.pending_ramp
        allowance = 0
        if headroom > 0:
            allowance = int(math.floor(headroom / self.cost))
            allowance = max(allowance, self._empty_host_floor(now))
        allowance = max(0, min(allowance, self.max_spawn_per_tick))
        self.allowance = allowance
        if allowance == 0:
            self.state = "saturated"
            self.last_reason = (
                f"projected load1={load1:.1f} + pending_ramp={self.pending_ramp:.1f} "
                f">= pause_above={self.pause_above:.1f} - one worker "
                f"(worker_load_cost={self.cost:.2f} {self.cost_source}, "
                f"ramp={self.ramp_seconds:g}s)"
            )
            return 0, self.last_reason
        self.state, self.last_reason = "admitting", None
        return allowance, None

    def _empty_host_floor(self, now: float) -> int:
        """1 when the host runs no kanban worker and none is ramping, else 0.

        floor(headroom / cost) is 0 whenever headroom < cost. On a 1-2 core
        host (pause_above = ncpu, cost prior 2.0) that is every tick, so the
        host would never spawn. An empty host may always start one worker
        while there is headroom: the overshoot that floor() guards against
        needs workers already running. ``running`` must be KNOWN to be 0;
        callers that cannot count keep plain floor().
        """
        if self.running == 0 and self.invisible_workers(now) <= 0:
            return 1
        return 0

    def _admit_on_cpu(self, now: float, hard: str) -> "tuple[int, Optional[str]]":
        """load1 is over the bar but the CPU is not: size from CPU headroom."""
        pending_cpu = self.invisible_workers(now) * self.worker_cpu_cost
        headroom = (self.cpu_busy_pause - self.cpu_busy) * self.ncpu - pending_cpu
        allowance = 0
        if headroom > 0:
            allowance = int(math.floor(headroom / self.worker_cpu_cost))
            allowance = max(allowance, self._empty_host_floor(now))
        allowance = max(0, min(allowance, self.max_spawn_per_tick))
        self.allowance = allowance
        self.state = "cpu_headroom"
        if allowance == 0:
            self.last_reason = (
                f"{hard}; cpu_busy={self.cpu_busy:.2f} < {self.cpu_busy_pause:.2f} "
                f"but pending cpu={pending_cpu:.1f} cores fills the headroom"
            )
            return 0, self.last_reason
        self.last_reason = None
        return allowance, None

    def record_spawns(self, n: int, now: Optional[float] = None) -> None:
        """Book ``n`` workers actually spawned this tick into the ramp window."""
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 0
        self.admitted_last_tick = max(0, n)
        if n <= 0:
            return
        now = time.monotonic() if now is None else float(now)
        self._ramp.append((now, n))
        self._admitted_since_summary += n

    def admit_now(
        self, running: Optional[int] = None, cpu_block: float = 0.0,
    ) -> "tuple[Optional[int], Optional[str]]":
        """:meth:`admit` against the host's live loadavg (inert if unavailable).

        CPU busy is the delta of ``psutil.cpu_times()`` since the previous
        call. The first call has no delta: pass ``cpu_block`` > 0 (one-shot
        CLI) to take a short second sample, else that tick is load1-only.
        """
        load1, load5 = sample_loadavg()
        if load1 is None:
            return None, None
        busy, self._cpu_prev = sample_cpu_busy(self._cpu_prev, block=cpu_block)
        return self.admit(load1, load5=load5, running=running, cpu_busy=busy)

    def finish_tick(self, spawned: int, logger=None, path=None) -> None:
        """Book spawns, emit gate lines, publish state for diagnostics."""
        if not self.enabled:
            return
        self.record_spawns(spawned)
        if logger is not None:
            self.log_tick(logger)
        try:
            self.write_state(path if path is not None else state_path())
        except Exception:
            pass

    # -- observability -----------------------------------------------------
    def ramp_workers(self) -> int:
        return sum(n for _, n in self._ramp)

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "state": self.state,
            "reason": self.last_reason,
            "load1": self.load1,
            "load5": self.load5,
            "pending_ramp": round(self.pending_ramp, 2),
            "ramp_workers": self.ramp_workers(),
            "allowance": self.allowance,
            "admitted_last_tick": self.admitted_last_tick,
            "pause_above": self.pause_above,
            "resume_below": self.resume_below,
            "worker_load_cost": self.worker_load_cost,
            "ramp_seconds": self.ramp_seconds,
            "max_spawn_per_tick": self.max_spawn_per_tick,
            "load5_floor": self.load5_floor,
            "cost": round(self.cost, 3),
            "cost_source": self.cost_source,
            "running": self.running,
            "cpu_busy": None if self.cpu_busy is None else round(self.cpu_busy, 3),
            "cpu_busy_pause": self.cpu_busy_pause,
            "ncpu": self.ncpu,
            "boards": self.boards,
            "updated_at": time.time(),
        }

    def _line(self) -> str:
        l1 = "?" if self.load1 is None else f"{self.load1:.1f}"
        l5 = "?" if self.load5 is None else f"{self.load5:.1f}"
        return (
            f"state={self.state} load1={l1} load5={l5} "
            f"pending_ramp={self.pending_ramp:.1f} ramp_workers={self.ramp_workers()} "
            f"allowance={self.allowance} admitted={self.admitted_last_tick} "
            f"pause_above={self.pause_above:.1f} cost={self.cost:.2f}/{self.cost_source} "
            f"running={'?' if self.running is None else self.running} "
            f"cpu_busy={'?' if self.cpu_busy is None else f'{self.cpu_busy:.2f}'}"
        )

    def log_tick(self, logger, now: Optional[float] = None) -> None:
        """Emit a gate line on every state change + a 5-minute summary."""
        if not self.enabled:
            return
        now = time.monotonic() if now is None else float(now)
        if self.state != self._last_logged_state:
            level = logger.warning if self.state == "paused" else logger.info
            level("kanban load gate: %s -> %s",
                  self._last_logged_state or "start", self._line())
            self._last_logged_state = self.state
        if self._last_summary_at is None:
            self._last_summary_at = now
        elif now - self._last_summary_at >= SUMMARY_INTERVAL_SECONDS:
            logger.info(
                "kanban load gate summary: %s admitted_5m=%d",
                self._line(), self._admitted_since_summary,
            )
            self._last_summary_at = now
            self._admitted_since_summary = 0

    def write_state(self, path: "os.PathLike[str] | str") -> None:
        """Atomically publish :meth:`snapshot` for ``hermes kanban diagnostics``."""
        path = Path(path)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.snapshot(), sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass


def split_allowance(
    allowance: Optional[int],
    demand: "list[tuple[str, int]]",
    start: int = 0,
) -> "dict[str, Optional[int]]":
    """Round-robin one tick's spawn ``allowance`` across boards.

    ``demand`` is ``[(slug, spawnable_count), ...]`` in board order. Boards
    with demand get one spawn each in turn, starting at ``start`` (mod the
    number of boards with demand -- pass a tick counter so the first pick
    rotates), until the allowance or every board's demand is spent. Order
    inside a board is left to ``dispatch_once`` (priority, then age).

    2026-09-28: the allowance used to be consumed board-by-board in a fixed
    order, default first; with 17 ready cards on default and allowance 4
    the subs-ace board got ZERO spawns for 93 minutes with 7 ready P1 cards.

    ``allowance is None`` (gate disabled) returns ``None`` for every board.
    """
    if allowance is None:
        return {slug: None for slug, _ in demand}
    quotas: "dict[str, Optional[int]]" = {slug: 0 for slug, _ in demand}
    live = [(slug, int(n)) for slug, n in demand if int(n or 0) > 0]
    left = max(0, int(allowance))
    if not live or left == 0:
        return quotas
    k = int(start) % len(live)
    order = live[k:] + live[:k]
    while left > 0:
        progressed = False
        for slug, need in order:
            if left <= 0:
                break
            if quotas[slug] < need:
                quotas[slug] += 1
                left -= 1
                progressed = True
        if not progressed:
            break
    return quotas


def format_board_starvation_lines(
    state: Optional[dict],
    now: Optional[float] = None,
    threshold_seconds: float = 600.0,
) -> "list[str]":
    """Lines for boards whose ready cards got no spawn for > threshold while
    the gate had allowance (``hermes kanban diagnostics``, t_f78d1938)."""
    boards = (state or {}).get("boards") or {}
    if not isinstance(boards, dict):
        return []
    now = time.time() if now is None else float(now)
    out = []
    for slug in sorted(boards):
        info = boards.get(slug) or {}
        since = info.get("starved_since")
        if not since:
            continue
        age = now - float(since)
        if age < threshold_seconds:
            continue
        out.append(
            f"Board starved: [{slug}] ready={info.get('ready')} got 0 spawns for "
            f"{age / 60:.0f}m while the load gate had allowance "
            f"(last quota={info.get('quota')}); check per-profile / host caps "
            f"or run `hermes kanban --board {slug} dispatch`"
        )
    return out


def sample_loadavg() -> "tuple[Optional[float], Optional[float]]":
    """(load1, load5) or (None, None) on platforms without getloadavg."""
    try:
        la = os.getloadavg()
    except (AttributeError, OSError):
        return None, None
    return float(la[0]), float(la[1])


def sample_cpu_busy(prev=None, block: float = 0.0):
    """``(busy_fraction | None, snapshot)`` from ``psutil.cpu_times()`` deltas.

    Busy is ``1 - idle/total`` over the interval since ``prev``. Linux iowait
    is not idle: a disk-bound host is not free capacity. ``None`` when psutil
    is missing, there is no previous snapshot (and ``block`` is 0), or the
    interval is empty.
    """
    try:
        import psutil  # dependency of hermes-agent; still fail open
    except Exception:
        return None, None
    try:
        cur = psutil.cpu_times()
        if prev is None and block > 0:
            prev = cur
            time.sleep(block)
            cur = psutil.cpu_times()
    except Exception:
        return None, prev
    if prev is None:
        return None, cur
    total = sum(cur) - sum(prev)
    idle = cur.idle - prev.idle
    if total <= 0:
        return None, cur
    return min(1.0, max(0.0, 1.0 - idle / total)), cur


def count_running_workers() -> "Optional[int]":
    """Running tasks across every board on this host.

    Each board is counted in isolation (t_ebbea874): one unreadable board,
    the current one included, no longer hides the healthy boards' count.
    ``None`` only when no board could be read at all.
    """
    try:
        from hermes_cli import kanban_db as _kb

        return _kb.count_running_tasks_host()
    except Exception:
        return None


def gate_from_config(config: Optional[dict] = None) -> LoadGate:
    """Build the gate from ``kanban.dispatch_load_gate`` in config.yaml."""
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            config = {}
    kanban_cfg = (config or {}).get("kanban") if isinstance(config, dict) else None
    kanban_cfg = kanban_cfg if isinstance(kanban_cfg, dict) else {}
    return LoadGate(kanban_cfg.get("dispatch_load_gate"), os.cpu_count() or 1)


def state_path() -> Path:
    from hermes_cli.kanban_db import kanban_home

    return kanban_home() / STATE_FILENAME


def read_state(path: "Optional[os.PathLike[str] | str]" = None) -> Optional[dict]:
    try:
        p = Path(path) if path is not None else state_path()
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def format_state_line(state: Optional[dict], now: Optional[float] = None) -> str:
    """One human line for ``hermes kanban diagnostics``."""
    if not state:
        return "Load gate: no state published (dispatcher not running a gated loop)"
    now = time.time() if now is None else now
    age = now - float(state.get("updated_at") or 0)
    l1 = state.get("load1")
    l5 = state.get("load5")
    return (
        f"Load gate: {state.get('state')} "
        f"load1={'?' if l1 is None else f'{float(l1):.1f}'} "
        f"load5={'?' if l5 is None else f'{float(l5):.1f}'} "
        f"pending_ramp={state.get('pending_ramp')} "
        f"allowance={state.get('allowance')} "
        f"admitted_last_tick={state.get('admitted_last_tick')} "
        f"pause_above={state.get('pause_above')} "
        f"max_spawn_per_tick={state.get('max_spawn_per_tick')} "
        f"(updated {age:.0f}s ago)"
    )
