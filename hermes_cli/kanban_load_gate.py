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

2026-10-02 (t_b660edb6): the Studio ran out of per-uid process slots
(kern.maxprocperuid 10666) while load1 sat at 7-8. A runaway self-recursing
microbench under one worker climbed from 1.4k to 10.3k processes over 3 h;
then every fork() returned EAGAIN, every hook failed closed, and the
gateway's watchdog exited it. load1 never saw it, so nothing paused and
nothing paged. The gate now also counts this user's processes against
RLIMIT_NPROC: at ``proc_pause_fraction`` (default 0.80) of the limit it is
``proc_paused`` (a hard pause CPU headroom cannot override), and it resumes
below ``proc_resume_fraction`` (default 0.65). An unreadable count or
limit leaves the gate load-only (fail open).

2026-10-04 (t_010c27d3, KWLB PR A): a two-signal SPILL BAND on top of the
states above, so the dispatcher can move portable work to pool hosts before
the Studio pauses (decision doc "Kanban spill off the Studio is two-signal",
PRD 2026-10-03_kanban-worker-load-balancing section 5.2.1). Four keys,
``spill_fraction`` / ``cpu_spill`` / ``cpu_spill_resume`` / ``cpu_pause``,
all default 1.0, which leaves ``admit()`` exactly as before. They are
validated as a SET (``cpu_spill_resume < cpu_spill <= cpu_pause <= 1``,
``0 < spill_fraction <= 1``, ``cpu_spill >= cpu_busy_pause``); a violation
forces all four to 1.0 and sets ``spill_keys_error``, because a partial edit
would re-open the 0.70-0.80 CPU edge. ``band`` is ``admitting`` /
``spilling`` / ``paused`` / ``proc_paused``. Spilling is entered at
``projected >= spill_above`` OR ``cpu >= cpu_spill`` and left only when
``projected < spill_resume`` AND ``cpu < cpu_spill_resume``; the flag is
updated on every tick in every state, and a missing CPU sample is an absent
leg (never enters, always satisfies the exit). While spilling, the local
allowance is ``min(load allowance against spill_above, CPU allowance against
cpu_pause)`` (option D). ``cpu >= cpu_pause`` is the CPU twin of the hard
pause. ``cpu_headroom`` keeps its own allowance and is a ``spilling`` row.

``kanban.max_spawn`` stays the hard concurrency ceiling; this gate is the
governor that decides how fast the host is allowed to approach it.

The class is pure and clock-injectable: feed ``admit(load1, load5=, now=)``
each tick, then ``record_spawns(n, now=)`` with what was actually spawned.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger(__name__)

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
DEFAULT_PROC_PAUSE_FRACTION = 0.80
DEFAULT_PROC_RESUME_FRACTION = 0.65
# The four spill-band keys. 1.0 for all four = band off (today's gate).
SPILL_KEYS = ("spill_fraction", "cpu_spill", "cpu_spill_resume", "cpu_pause")
SPILL_INERT = 1.0
SPILL_KEYS_WARN_INTERVAL_SECONDS = 300.0
SUMMARY_INTERVAL_SECONDS = 300.0
STATE_FILENAME = "load_gate.json"


def _num(value: Any, default: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return float(default)
    return f if f > 0 else float(default)


def _spill_keys(
    cfg: dict, cpu_busy_pause: float
) -> "tuple[dict[str, float], Optional[str]]":
    """Validate the four spill keys as one set (PRD D-2c).

    Returns ``(values, error)``. All four absent (or all 1.0) is the inert
    set with no error. A present key that is not a number in (0, 1], or any
    ordering violation, refuses the WHOLE set: every key 1.0 plus the rule
    that failed.
    """
    inert = {k: SPILL_INERT for k in SPILL_KEYS}
    present = [k for k in SPILL_KEYS if cfg.get(k) is not None]
    if not present:
        return inert, None
    vals = dict(inert)
    for k in present:
        try:
            v = float(cfg.get(k))
        except (TypeError, ValueError):
            return inert, f"{k}={cfg.get(k)!r} is not a number"
        if not (0.0 < v <= 1.0):  # also refuses NaN
            return inert, f"{k}={v:g} is outside (0, 1]"
        vals[k] = v
    if all(v == SPILL_INERT for v in vals.values()):
        return inert, None
    sr, sp, cp = vals["cpu_spill_resume"], vals["cpu_spill"], vals["cpu_pause"]
    if not (sr < sp <= cp <= 1.0):
        return inert, (
            f"need cpu_spill_resume < cpu_spill <= cpu_pause <= 1, got "
            f"{sr:g} / {sp:g} / {cp:g}"
        )
    if sp < cpu_busy_pause:
        return inert, (
            f"need cpu_spill >= cpu_busy_pause, got {sp:g} < {cpu_busy_pause:g}"
        )
    return vals, None


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
        pf = _num(cfg.get("proc_pause_fraction"), DEFAULT_PROC_PAUSE_FRACTION)
        self.proc_pause_fraction = pf if pf < 1.0 else DEFAULT_PROC_PAUSE_FRACTION
        rf = _num(cfg.get("proc_resume_fraction"), DEFAULT_PROC_RESUME_FRACTION)
        self.proc_resume_fraction = (
            rf if rf < self.proc_pause_fraction else 0.8 * self.proc_pause_fraction
        )
        # Absolute override of the per-uid process limit (else RLIMIT_NPROC).
        self.proc_limit_override: Optional[int] = (
            _pos_int(cfg.get("proc_limit"), 1) if cfg.get("proc_limit") else None
        )
        spill, self.spill_keys_error = _spill_keys(cfg, self.cpu_busy_pause)
        self.spill_fraction = spill["spill_fraction"]
        self.cpu_spill = spill["cpu_spill"]
        self.cpu_spill_resume = spill["cpu_spill_resume"]
        self.cpu_pause = spill["cpu_pause"]
        # Band machinery is armed only by a valid, non-inert key set; inert
        # keeps every admit() output and band derivation at today's.
        self.spill_armed = any(v != SPILL_INERT for v in spill.values())
        self.spill_above = self.spill_fraction * self.pause_above
        self.spill_resume = self.spill_fraction * self.resume_below
        if self.spill_keys_error:
            _log.warning(
                "kanban load gate: spill keys refused as a set: %s",
                self.spill_keys_error,
            )
        self._spill_warned_at: Optional[float] = None
        self.spilling = False          # hysteresis flag, updated every tick
        self.band = "admitting"        # admitting|spilling|paused|proc_paused
        self.spill_reason: Optional[str] = None  # load|cpu|both|unreadable
        self.remote_allowed = True     # False only in proc_paused
        # Per-tick pool block for load_gate.json; reset to planless on every
        # admit() so a previous tick's plan is never republished (RC-8).
        self.pool: dict = {"planned": False, "reason": "not_needed"}
        self.procs: Optional[int] = None
        self.proc_limit: Optional[int] = None
        self.proc_paused = False
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

    def update_procs(
        self, procs: Optional[int], proc_limit: Optional[int]
    ) -> Optional[str]:
        """Process-slot hysteresis; return the pause reason (None = clear).

        Pauses at ``proc_pause_fraction`` of the limit, resumes below
        ``proc_resume_fraction``. Unknown count or limit: no process gate
        (and a previous pause is held, not silently released).
        """
        limit = self.proc_limit_override or proc_limit
        try:
            procs = int(procs) if procs is not None else None
            limit = int(limit) if limit else None
        except (TypeError, ValueError):
            procs, limit = None, None
        self.procs, self.proc_limit = procs, limit
        if procs is None or not limit or limit <= 0:
            return self._proc_reason() if self.proc_paused else None
        if self.proc_paused:
            if procs < self.proc_resume_fraction * limit:
                self.proc_paused = False
        elif procs >= self.proc_pause_fraction * limit:
            self.proc_paused = True
        return self._proc_reason() if self.proc_paused else None

    def _proc_reason(self) -> str:
        limit = self.proc_limit or 0
        procs = "?" if self.procs is None else str(self.procs)
        return (
            f"PROCESS SLOTS: uid procs={procs} of limit {limit} "
            f">= {self.proc_pause_fraction:.0%}; fork() fails with EAGAIN at the "
            f"limit; resumes below {int(self.proc_resume_fraction * limit)}"
        )

    def host_recovered(self) -> bool:
        """True when this tick's admit() let at least one spawn through.

        Every pause/saturation state (``paused``, ``proc_paused``,
        ``saturated``, a full ``cpu_headroom``) sets allowance 0, so this is
        the gate's own "the host can take work" verdict. Used to requeue
        host-transient blocks."""
        return bool(self.enabled and (self.allowance or 0) > 0)

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
        procs: Optional[int] = None,
        proc_limit: Optional[int] = None,
    ) -> "tuple[Optional[int], Optional[str]]":
        """Return ``(allowance, reason)`` for this tick.

        ``allowance`` is the max number of NEW workers this tick may spawn
        (``None`` = gate disabled, no limit). ``reason`` is non-None exactly
        when ``allowance == 0`` and explains why. ``running`` (workers
        running on the host) feeds the load-per-worker slope; ``cpu_busy``
        (0..1 of all cores) corroborates a load1 pause. Both are optional.
        ``procs`` / ``proc_limit`` (this uid's process count and its limit)
        drive the process-slot pause; either missing = no process gate.
        """
        self.pool = {"planned": False, "reason": "not_needed"}
        if not self.enabled:
            self.state, self.allowance, self.reason = "disabled", None, None
            self.last_reason = None
            self.band, self.spill_reason, self.remote_allowed = "admitting", None, True
            return None, None
        try:
            load1 = float(load1)
        except (TypeError, ValueError):
            self._mark_unreadable()
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
        proc_hard = self.update_procs(procs, proc_limit)
        projected = load1 + self.pending_ramp
        # Every tick, every state: leaving paused must land in spilling even
        # when paused was entered straight from admitting.
        self._update_spilling(projected)
        allowance, reason = self._admit_base(now, load1, hard, proc_hard)
        return self._apply_band(now, load1, projected, allowance, reason)

    def _admit_base(
        self, now: float, load1: float, hard: Optional[str], proc_hard: Optional[str]
    ) -> "tuple[int, Optional[str]]":
        """The pre-band gate: proc pause, load1 pause / CPU headroom, load
        headroom. With the spill keys inert this IS ``admit()``."""
        if proc_hard:
            self.state, self.allowance, self.last_reason = "proc_paused", 0, proc_hard
            return 0, proc_hard
        if hard:
            if (
                self.cpu_corroborate
                and self.cpu_busy is not None
                and self.cpu_busy < self.cpu_busy_pause
            ):
                allowance, pending_cpu = self._cpu_allowance(now, self.cpu_busy_pause)
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
            self.state, self.allowance, self.last_reason = "paused", 0, hard
            return 0, hard
        allowance = self._load_allowance(now, load1, self.pause_above)
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

    def _mark_unreadable(self) -> None:
        """Enabled gate, load1 unreadable: ``admit()`` still returns
        ``(None, None)``; the band tells the tick builder to run local 0
        (``gate_unreadable``) while the pool's own probes decide remote.

        A held process-slot pause wins: the unreadable paths return before
        ``update_procs`` runs, so nothing has cleared it and remote stays off.
        """
        if self.proc_paused:
            self.band, self.spill_reason, self.remote_allowed = "proc_paused", None, False
            self.pool = {"planned": False, "reason": "proc_paused"}
            return
        self.band, self.spill_reason, self.remote_allowed = "paused", "unreadable", True

    def _cpu_over(self, bar: float) -> bool:
        """CPU at/over ``bar``; a missing sample is an absent leg (False)."""
        return self.spill_armed and self.cpu_busy is not None and self.cpu_busy >= bar

    def _update_spilling(self, projected: float) -> None:
        if not self.spill_armed:
            self.spilling = False
            return
        cpu = self.cpu_busy
        if self.spilling:
            if projected < self.spill_resume and (
                cpu is None or cpu < self.cpu_spill_resume
            ):
                self.spilling = False
        elif projected >= self.spill_above or (cpu is not None and cpu >= self.cpu_spill):
            self.spilling = True

    def _apply_band(
        self,
        now: float,
        load1: float,
        projected: float,
        allowance: int,
        reason: Optional[str],
    ) -> "tuple[int, Optional[str]]":
        """Derive the band from the gate's FINAL state (PRD 5.2.1, first
        matching row wins). Only two rows change the local allowance: the
        CPU twin of the hard pause and the spilling row; both need an armed
        key set, so inert keys return ``(allowance, reason)`` untouched."""
        state = self.state
        self.remote_allowed = state != "proc_paused"
        if state == "proc_paused":
            self.band, self.spill_reason = "proc_paused", None
            self.pool = {"planned": False, "reason": "proc_paused"}
            return allowance, reason
        load_over = self.spill_armed and projected >= self.spill_above
        if state == "paused":
            self.band = "paused"
            self.spill_reason = "both" if self._cpu_over(self.cpu_pause) else "load"
            return allowance, reason
        if self._cpu_over(self.cpu_pause):
            self.band = "paused"
            self.spill_reason = "both" if load_over else "cpu"
            why = (
                f"cpu_busy={self.cpu_busy:.2f} >= cpu_pause={self.cpu_pause:.2f} "
                f"(no new local workers; spill band)"
            )
            self.state, self.allowance, self.last_reason = "paused", 0, why
            return 0, why
        if state == "cpu_headroom":
            if not self.spill_armed:
                self.band, self.spill_reason = "admitting", None
                return allowance, reason
            # A spilling row that keeps today's CPU-headroom allowance.
            self.band = "spilling"
            self.spill_reason = "both" if self._cpu_over(self.cpu_spill) else "load"
            return allowance, reason
        if not self.spilling:
            self.band, self.spill_reason = "admitting", None
            return allowance, reason
        cpu_over = self._cpu_over(self.cpu_spill)
        if load_over or cpu_over:
            self.spill_reason = (
                "both" if load_over and cpu_over else ("load" if load_over else "cpu")
            )
        elif self.band != "spilling" or self.spill_reason not in ("load", "cpu", "both"):
            self.spill_reason = "load"
        # else: inside the hysteresis gap, keep the edge that opened it.
        self.band = "spilling"
        local = self._load_allowance(now, load1, self.spill_above)
        if self.cpu_busy is not None:
            local = min(local, self._cpu_allowance(now, self.cpu_pause)[0])
        self.allowance = local
        if local == 0:
            self.state = "saturated"
            cpu = "?" if self.cpu_busy is None else f"{self.cpu_busy:.2f}"
            self.last_reason = (
                f"spilling ({self.spill_reason}): projected load1={projected:.1f} vs "
                f"spill_above={self.spill_above:.1f}, cpu_busy={cpu} vs "
                f"cpu_pause={self.cpu_pause:.2f} leave no local worker"
            )
            return 0, self.last_reason
        self.state, self.last_reason = "admitting", None
        return local, None

    def _load_allowance(self, now: float, load1: float, bar: float) -> int:
        """floor((bar - load1 - pending_ramp) / cost), empty-host floor, cap."""
        headroom = bar - load1 - self.pending_ramp
        allowance = 0
        if headroom > 0:
            allowance = int(math.floor(headroom / self.cost))
            allowance = max(allowance, self._empty_host_floor(now))
        return max(0, min(allowance, self.max_spawn_per_tick))

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

    def _cpu_allowance(self, now: float, bar: float) -> "tuple[int, float]":
        """``(allowance, pending_cpu)`` sized from CPU headroom under ``bar``.

        One function, two bars: ``cpu_busy_pause`` for the ``cpu_headroom``
        row (load1 over the bar, CPU not) and ``cpu_pause`` for the spilling
        row (option D). Needs a CPU sample.
        """
        pending_cpu = self.invisible_workers(now) * self.worker_cpu_cost
        headroom = (bar - self.cpu_busy) * self.ncpu - pending_cpu
        allowance = 0
        if headroom > 0:
            allowance = int(math.floor(headroom / self.worker_cpu_cost))
            allowance = max(allowance, self._empty_host_floor(now))
        return max(0, min(allowance, self.max_spawn_per_tick)), pending_cpu

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
            self.pool = {"planned": False, "reason": "not_needed"}
            if self.enabled:
                self._mark_unreadable()
            return None, None
        busy, self._cpu_prev = sample_cpu_busy(self._cpu_prev, block=cpu_block)
        procs, proc_limit = sample_user_procs() if self.enabled else (None, None)
        return self.admit(
            load1, load5=load5, running=running, cpu_busy=busy,
            procs=procs, proc_limit=proc_limit,
        )

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
        now = time.time()
        pool = dict(self.pool)
        if pool.get("planned"):
            # admit() resets the block every tick, so a planned block is THIS
            # tick's; the watch matches planned_at to updated_at (RC-8).
            pool["planned_at"] = now
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
            "procs": self.procs,
            "proc_limit": self.proc_limit,
            "proc_pause_fraction": self.proc_pause_fraction,
            "ncpu": self.ncpu,
            "boards": self.boards,
            "band": self.band,
            "spill_reason": self.spill_reason,
            "spilling": self.spilling,
            "remote_allowed": self.remote_allowed,
            "spill_above": self.spill_above,
            "spill_resume": self.spill_resume,
            "cpu_spill": self.cpu_spill,
            "cpu_spill_resume": self.cpu_spill_resume,
            "cpu_pause": self.cpu_pause,
            "spill_keys_error": self.spill_keys_error,
            "pool": pool,
            "updated_at": now,
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
            f"cpu_busy={'?' if self.cpu_busy is None else f'{self.cpu_busy:.2f}'} "
            f"procs={'?' if self.procs is None else self.procs}"
            f"/{'?' if not self.proc_limit else self.proc_limit} "
            f"band={self.band}/{self.spill_reason or '-'}"
        )

    def log_tick(self, logger, now: Optional[float] = None) -> None:
        """Emit a gate line on every state change + a 5-minute summary."""
        if not self.enabled:
            return
        now = time.monotonic() if now is None else float(now)
        if self.spill_keys_error and (
            self._spill_warned_at is None
            or now - self._spill_warned_at >= SPILL_KEYS_WARN_INTERVAL_SECONDS
        ):
            logger.warning(
                "kanban load gate: spill keys refused as a set: %s",
                self.spill_keys_error,
            )
            self._spill_warned_at = now
        if self.state != self._last_logged_state:
            level = logger.warning if self.state == "paused" else logger.info
            if self.state == "proc_paused":
                level = logger.error
            level("kanban load gate: %s -> %s%s",
                  self._last_logged_state or "start", self._line(),
                  f" -- {self.last_reason}" if self.state == "proc_paused" else "")
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


def loadavg_supported() -> bool:
    """False on hosts with no ``os.getloadavg`` (native Windows): the gate
    has no load signal there and must not fail closed on its absence."""
    return hasattr(os, "getloadavg")


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


def sample_user_procs() -> "tuple[Optional[int], Optional[int]]":
    """(this uid's process count, its process limit) or None for either.

    Reads the process table through psutil (sysctl/proc, no fork), so it
    still works when the host is out of process slots. The limit is the
    soft RLIMIT_NPROC (kern.maxprocperuid on macOS); unlimited reads as None.
    """
    limit: Optional[int] = None
    try:
        import resource

        soft, _hard = resource.getrlimit(resource.RLIMIT_NPROC)
        if soft != resource.RLIM_INFINITY and soft > 0:
            limit = int(soft)
    except Exception:
        limit = None
    try:
        import psutil

        uid = os.getuid()  # windows-footgun: ok (inside try; Windows -> fail open)
        count = 0
        for p in psutil.process_iter(["uids"]):
            uids = p.info.get("uids")
            if uids is not None and uids.real == uid:
                count += 1
    except Exception:
        return None, limit
    return count, limit


def top_user_proc_families(n: int = 3) -> "list[tuple[str, int]]":
    """The ``n`` most common process names for this uid (no fork)."""
    try:
        import psutil

        uid = os.getuid()  # windows-footgun: ok (inside try; Windows -> fail open)
        counts: dict = {}
        for p in psutil.process_iter(["uids", "name"]):
            uids = p.info.get("uids")
            if uids is not None and uids.real == uid:
                name = p.info.get("name") or "?"
                counts[name] = counts.get(name, 0) + 1
    except Exception:
        return []
    return sorted(counts.items(), key=lambda kv: -kv[1])[:n]


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
        data = json.loads(p.read_text(encoding="utf-8-sig"))
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
        f"procs={state.get('procs')}/{state.get('proc_limit')} "
        f"band={state.get('band')}/{state.get('spill_reason') or '-'} "
        f"(updated {age:.0f}s ago)"
    )
