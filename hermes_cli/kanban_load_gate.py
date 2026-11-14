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

2026-10-18 (t_a7978f86): load1 325 with the CPU 54% idle, P-cores 33% busy
and the 8 E-cores 100% busy. The ``cpu_headroom`` row read the mean over
all 32 cores (0.41-0.50 < ``cpu_busy_pause``) and kept admitting 17-26 local
spawns an hour, while the background QoS band (the E-cores on Apple
silicon: test-gate's nice-19 runs, launchd Background agents, dpx units)
got no slice and dpx ``/health`` took 8 s. ``ecore_gate`` (``off`` |
``report`` | ``on``) reads E-core busy from per-cpu ``psutil.cpu_times``
deltas; at ``ecore_busy_pause`` (default 0.95) or above, a CPU-headroom
allowance is NO headroom for local spawns. ``report`` (the default until
the D1 ruling) changes nothing and logs ``kanban load gate (dry): ...
would-refuse N``; ``on`` refuses. Hosts without a perflevel split (Linux,
Intel) have no E-core sample and the knob is inert there.

2026-10-18 (t_da6f84f7, Ace 14:33 / 15:5x, ruling t_0b33557c): ``kanban.admission:
utilization`` (read per tick; absent or ``legacy`` = everything above, byte for
byte) replaces the load1 pause, the CPU-headroom row and ``worker_cpu_cost``
with the ONE fleet predicate, ``hermes_cli.fleet_placement.admission.admit()``, over this
host's own sample: cpu_busy (psutil cpu_times delta), MemAvailable after the
host table's ``mem_reserve_gb``, procs / RLIMIT_NPROC (PSI is not measured on
darwin and is not required). Each worker spawned within ``admission.
DEFAULT_SETTLE_S`` is a lease of the measured estimate for its class on this
host (p90 of var/placement/class-usage.jsonl, the table footprint as prior), so
one tick's admits see each other. ``consumers.kanban.slots`` on this host's
row of config/fleet-hosts.json (the Studio: 2, ``_ceiling_reason``) stays a
CEILING on running workers. The interim keys (pause_above, resume_below,
cpu_busy_pause, worker_cpu_cost, the spill keys) stay readable but are inert in
this mode; a refusal is band ``spilling`` (the pool takes the work) and the
process-slot pause still wins.

2026-11-04 (t_b20850d2, FWA-H; Ace ruling 07:36: ONE layer decides where and how much work
runs): the legacy load1 gate, its CPU-headroom row, the spill band, the E-core leg and the
``kanban.admission`` switch between them are gone. Every caller (the gateway tick, ``kanban
daemon``, one-shot ``kanban dispatch``, ``kanban admit``) runs ``admission.admit_class("kanban")``
over this host's own sample. The keys ``pause_above``, ``resume_below``, ``worker_load_cost*``,
``slope_window_seconds``, ``load5_floor``, ``cpu_corroborate``, ``cpu_busy_pause``,
``worker_cpu_cost``, ``spill_fraction``, ``cpu_spill``, ``cpu_spill_resume``, ``cpu_pause``,
``ecore_gate`` and ``ecore_busy_pause`` are no longer read (``kanban.admission`` itself stays: the
vendored placement scorer and ``unified_filter`` read it). What stays: ``enabled``,
``max_spawn_per_tick`` (pacing), ``ramp_seconds`` (how long a spawn is kept as a lease
candidate) and the process-slot pause (``proc_*``), which guards fork() exhaustion, not
capacity.

``kanban.max_spawn`` stays the hard concurrency ceiling; this gate is the
governor that decides how fast the host is allowed to approach it.

The class is clock-injectable: feed ``admit(cpu_busy=, running=, now=)`` each
tick, then ``record_spawns(n, now=)`` with what was actually spawned.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger(__name__)

DEFAULT_RAMP_SECONDS = 600.0
DEFAULT_MAX_SPAWN_PER_TICK = 4
DEFAULT_PROC_PAUSE_FRACTION = 0.80
DEFAULT_PROC_RESUME_FRACTION = 0.65
SUMMARY_INTERVAL_SECONDS = 300.0
STATE_FILENAME = "load_gate.json"
# The only admission this gate runs (t_b20850d2). Published in load_gate.json for readers that key on it.
ADMISSION = "utilization"
UTIL_CLASS = "kanban-worker-llm-wait"
UTIL_HOST_ALIAS = "local"     # the dispatcher's own row of config/fleet-hosts.json (mac-studio carries it)
# Every attribute this module reads off the vendored placement modules. A re-vendor that drops one
# (af0437b0a63 dropped admission.estimate, t_865feb4e) is logged at import and fails
# tests/hermes_cli/test_kanban_load_gate_admission_contract.py.
VENDORED_CONTRACT = {
    "admission": ("Estimate", "Lease", "Limits", "admit", "read_slice_memory", "read_history"),
    "placement": ("load_hosts", "need_of", "class_usage_path", "CLASS_USAGE_WINDOW_S"),
    "work_estimate": ("estimate",),
}


def missing_contract() -> list[str]:
    """``module.name`` for each VENDORED_CONTRACT attribute the vendored modules do not define."""
    import importlib

    out = []
    for mod, names in VENDORED_CONTRACT.items():
        m = importlib.import_module(f"hermes_cli.fleet_placement.{mod}")
        out.extend(f"{mod}.{n}" for n in names if not hasattr(m, n))
    return out


_MISSING = missing_contract()
if _MISSING:
    _log.error("kanban load gate: vendored placement modules lack %s; the utilization path will fail",
               ", ".join(_MISSING))


def _denom(ncpu: Any = None) -> str:
    """This host's load denominator, house form ``24c·32t`` (Ace ruling 2026-10-12: a load figure is
    never bare; it names physical cores and hardware threads)."""
    from hermes_platform.host.facts import load_denominator

    try:
        return load_denominator(int(ncpu) if ncpu else None)
    except (TypeError, ValueError):
        return load_denominator()


def _num(value: Any, default: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return float(default)
    return f if f > 0 else float(default)


def _opt_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _pos_int(value: Any, default: int) -> int:
    try:
        i = int(value)
    except (TypeError, ValueError):
        return int(default)
    return i if i >= 1 else int(default)


class LoadGate:
    """Admission gate for dispatcher SPAWNS: the ONE rule, ``admission.admit_class("kanban")``.

    Never reclaims or kills anything: it only decides how many NEW workers a
    tick may start. See the module docstring for the incidents it encodes.
    """

    def __init__(self, cfg: Optional[dict], ncpu: int) -> None:
        cfg = cfg if isinstance(cfg, dict) else {}
        self.enabled = bool(cfg.get("enabled", True))
        self.ncpu = max(1, int(ncpu or 1))
        self.ramp_seconds = _num(cfg.get("ramp_seconds"), DEFAULT_RAMP_SECONDS)
        self.max_spawn_per_tick = _pos_int(
            cfg.get("max_spawn_per_tick"), DEFAULT_MAX_SPAWN_PER_TICK
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
        self.band = "admitting"        # admitting|spilling|proc_paused
        self.spill_reason: Optional[str] = None  # utilization while spilling
        self.remote_allowed = True     # False only in proc_paused
        # Per-tick pool block for load_gate.json; reset to planless on every
        # admit() so a previous tick's plan is never republished (RC-8).
        self.pool: dict = {"planned": False, "reason": "not_needed"}
        self.procs: Optional[int] = None
        self.proc_limit: Optional[int] = None
        self.proc_paused = False
        # Observable state (snapshot / logs / diagnostics).
        self.state = "admitting"
        self.load1: Optional[float] = None   # display only: never read by admission
        self.load5: Optional[float] = None
        self.running: Optional[int] = None
        self.cpu_busy: Optional[float] = None
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
        self.util: dict = {}

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
        """True when this tick's admit() let at least one spawn through (the
        gate's own "the host can take work" verdict). Used to requeue
        host-transient blocks."""
        return bool(self.enabled and (self.allowance or 0) > 0)

    def _prune(self, now: float) -> None:
        horizon = now - self.ramp_seconds
        while self._ramp and self._ramp[0][0] <= horizon:
            self._ramp.popleft()

    def admit(
        self,
        load1: Optional[float] = None,
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
        when ``allowance == 0`` and explains why. ``load1`` / ``load5`` are
        recorded for display only. ``cpu_busy`` (0..1 of all cores) is the
        sample's CPU axis; ``procs`` / ``proc_limit`` drive the process-slot
        pause (either missing = no process gate).
        """
        self.pool = {"planned": False, "reason": "not_needed"}
        if not self.enabled:
            self.state, self.allowance, self.last_reason = "disabled", None, None
            self.band, self.spill_reason, self.remote_allowed = "admitting", None, True
            return None, None
        now = time.monotonic() if now is None else float(now)
        self.load1, self.load5 = _opt_float(load1), _opt_float(load5)
        busy = _opt_float(cpu_busy)
        self.cpu_busy = None if busy is None else min(1.0, max(0.0, busy))
        return self._admit_utilization(now, running, procs, proc_limit)

    def _admit_utilization(
        self, now: float, running: Optional[int], procs: Optional[int], proc_limit: Optional[int],
    ) -> "tuple[int, Optional[str]]":
        """admission.admit_class("kanban") over this host's sample, one lease per
        spawn the sample cannot show yet, under the host table's kanban ceiling."""
        from hermes_cli.fleet_placement import admission as _ad

        try:
            self.running = int(running) if running is not None else None
        except (TypeError, ValueError):
            self.running = None
        self._prune(now)
        proc_hard = self.update_procs(procs, proc_limit)
        self.remote_allowed = not proc_hard
        if proc_hard:
            self.state, self.allowance, self.last_reason = "proc_paused", 0, proc_hard
            self.band, self.spill_reason = "proc_paused", None
            self.pool = {"planned": False, "reason": "proc_paused"}
            return 0, proc_hard
        # Prism b9b45a76cc17: a kanban kind pause on this host closes local starts (the pool path closes it in
        # placement.choose); remote hosts stay allowed, they answer for their own pause. Unreadable = closed.
        from hermes_cli import kanban_headroom as _kh
        try:
            paused = _kh.local_kind_pause()
        except Exception as exc:
            paused = f"kind pause unreadable ({type(exc).__name__}: {exc}); refusing, fail closed"
        if paused:
            self.state, self.allowance, self.last_reason = "saturated", 0, paused
            self.band, self.spill_reason = "spilling", "kind_paused"
            return 0, paused
        ctx = utilization_context()
        sample = {"host": ctx["host"], "os": _os_name(), "at": now, "ncpu": self.ncpu, "cpu_busy": self.cpu_busy,
                  "mem_available_gb": ctx["mem_available_gb"], "mem_total_gb": ctx["mem_total_gb"],
                  "procs": self.procs, "maxproc": self.proc_limit, **sample_psi()}
        leases = [_ad.Lease(host=ctx["host"], job_class=UTIL_CLASS, need=ctx["need"], at=ts)
                  for ts, n in self._ramp for _ in range(int(n))]
        try:
            # t_02aa6d86 (doctrine rule 15): every OTHER consumer's lease on this host (Prism, test-gate, CI) is load
            # this gate's ramp never saw; an unreadable shared ledger fails closed, never as "nothing booked"
            # Lease.at is wall-clock; ``now`` is the gate's clock (time.monotonic() in admit_now): re-date each lease by
            # its age, as GateTickBuilder.seed_out_of_tick does, or a settled lease reads newer than the sample and is
            # counted on top of the cpu_busy that already holds it (Prism c-79e25e334a28b343)
            wall = time.time()
            leases += [dataclasses.replace(le, at=now - max(0.0, wall - le.at)) for le in shared_leases(ctx["host"])]
        except Exception as exc:
            why = f"utilization: shared ledger unreadable ({type(exc).__name__}: {exc}); refusing, fail closed"
            self.state, self.allowance, self.last_reason = "saturated", 0, why
            self.band, self.spill_reason = "spilling", "utilization"
            return 0, why
        allowance, verdict = 0, None
        # the worker slice's own memory cap (Linux kanbanw.slice; None elsewhere = host axis only, Prism 616c5f0203bd)
        slice_mem = _ad.read_slice_memory(classes=(UTIL_CLASS,))
        unified = unified_admit()
        demand, priority = unified_inputs(ctx["host"]) if unified else ([], _ad.Priority())
        for _ in range(self.max_spawn_per_tick):
            if unified:
                # t_3694ab3c (inventory D8/D9): the ONE rule, admit_class("kanban"): the fleet line
                # (fleet/admission-priority.json) and the queued demand of every class above kanban
                v = _ad.admit_class(sample, UNIFIED_WORKLOAD, ctx["need"], leases, demand=demand,
                                    priority=priority, host=ctx["host"], limits=ctx["limits"], slice_mem=slice_mem)
            else:
                v = _ad.admit(sample, ctx["need"], leases, host=ctx["host"], limits=ctx["limits"], slice_mem=slice_mem)
            if not v.ok:
                verdict = v
                break
            allowance += 1
            leases.append(_ad.Lease(host=ctx["host"], job_class=UTIL_CLASS, need=ctx["need"], at=now))
        ceiling, why = ctx["ceiling"], None
        if ceiling is not None and self.running is None:
            # a CEILING on running workers with the count unknown fails closed (Prism 710b1c8865a5)
            allowance, why = 0, f"ceiling: running count unknown under kanban ceiling {ceiling}"
        elif ceiling is not None:
            room = max(0, ceiling - self.running)
            if room < allowance:
                allowance = room
                why = f"ceiling: {self.running} running of kanban ceiling {ceiling} ({ctx['ceiling_reason']})"
        if allowance == 0 and why is None:
            why = f"utilization: {verdict.reason} (binding {verdict.binding})" if verdict else "utilization: no room"
        self.util = {"rule": "admit_class" if unified else "admit", "host": ctx["host"],
                     "need": {"cpus": ctx["need"].cpus, "ram_gb": ctx["need"].ram_gb, "source": ctx["need"].source},
                     "ceiling": ceiling, "leases": len(leases) - allowance,
                     "binding": verdict.binding if verdict else None,
                     "free": {k: round(float(x), 3) for k, x in (verdict.free if verdict else {}).items()}}
        self.allowance = allowance
        if allowance == 0:
            self.state, self.last_reason = "saturated", why
            self.band, self.spill_reason = "spilling", "utilization"
            return 0, why
        self.state, self.last_reason = "admitting", None
        self.band, self.spill_reason = "admitting", None
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
        """:meth:`admit` against the host's live sample.

        CPU busy is the delta of ``psutil.cpu_times()`` since the previous
        call. The first call has no delta, so it takes a short (>= 0.25 s)
        second sample: the predicate refuses an unmeasured cpu_busy.
        """
        load1, load5 = sample_loadavg()
        if self._cpu_prev is None:
            cpu_block = max(cpu_block, 0.25)
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
            "ramp_workers": self.ramp_workers(),
            "allowance": self.allowance,
            "admitted_last_tick": self.admitted_last_tick,
            "ramp_seconds": self.ramp_seconds,
            "max_spawn_per_tick": self.max_spawn_per_tick,
            "running": self.running,
            "cpu_busy": None if self.cpu_busy is None else round(self.cpu_busy, 3),
            "procs": self.procs,
            "proc_limit": self.proc_limit,
            "proc_pause_fraction": self.proc_pause_fraction,
            "ncpu": self.ncpu,
            "boards": self.boards,
            "band": self.band,
            "spill_reason": self.spill_reason,
            "remote_allowed": self.remote_allowed,
            "pool": pool,
            "admission": ADMISSION,
            "util": self.util,
            "updated_at": now,
        }

    def _line(self) -> str:
        l1 = "?" if self.load1 is None else f"{self.load1:.1f}"
        return (
            f"state={self.state} admission={ADMISSION} "
            f"(load {l1} tasks / {_denom(self.ncpu)}, not read) "
            f"ramp_workers={self.ramp_workers()} "
            f"allowance={self.allowance} admitted={self.admitted_last_tick} "
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
        if self.state != self._last_logged_state:
            level = logger.error if self.state == "proc_paused" else logger.info
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


def _os_name() -> str:
    import sys

    return "darwin" if sys.platform == "darwin" else ("linux" if sys.platform.startswith("linux") else sys.platform)


def sample_psi(root: str = "/proc/pressure") -> dict:
    """PSI ``some avg10`` for cpu / memory / io as ``psi_<x>_some10`` (Linux). An unreadable file is left out, so
    admission.admit() refuses it as unmeasured rather than reading 0; darwin has no /proc/pressure and admit()
    exempts those axes there (Prism 8952c3f1bbe9 / 616c5f0203bd)."""
    out: dict = {}
    for res, key in (("cpu", "psi_cpu_some10"), ("memory", "psi_mem_some10"), ("io", "psi_io_some10")):
        try:
            with open(os.path.join(root, res), encoding="utf-8-sig") as fh:
                for ln in fh:
                    if ln.startswith("some "):
                        out[key] = float(dict(kv.split("=", 1) for kv in ln.split()[1:])["avg10"])
                        break
        except (OSError, ValueError, KeyError):
            continue
    return out


UNIFIED_WORKLOAD = "kanban"


def shared_leases(host: str, root=None, now: Optional[float] = None) -> list:
    """The shared reservation ledger's live leases on ``host`` booked by every consumer BUT kanban (t_02aa6d86,
    doctrine rule 15): a Prism review, a test-gate run or a CI runner admitted on this host is load the gate must
    count before its sample shows it. Kanban's own rows are left out: the gate's ramp already carries every local
    kanban start (its own spawns plus GateTickBuilder.seed_out_of_tick), so they would be booked twice. A missing
    ledger is empty; an unreadable one raises (the caller fails closed)."""
    from hermes_cli.fleet_placement import placement as _pl

    base = Path(root) if root is not None else _pl.fleet_root()
    rpath = base / _pl.RESERVATIONS_REL
    if not rpath.exists():
        return []
    try:
        table = _pl.load_hosts(base / _pl.TABLE_REL)
    except Exception:            # no table: the ledger's own host names, matched as written
        table = None
    return [le for le in _pl.ledger_leases(rpath, now=now, table=table)
            if le.host == host and not str(le.job_class).startswith(UNIFIED_WORKLOAD)]


def unified_admit(env: Optional[dict] = None) -> bool:
    """KANBAN_UNIFIED_ADMIT (t_3694ab3c): "0" = today's rule (admission.admit, the caller's own line); anything else =
    admission.admit_class("kanban") with the fleet priority + queued demand (admission_demand.unified)."""
    from hermes_cli.fleet_placement import admission_demand as _dmd

    return _dmd.unified(UNIFIED_WORKLOAD, env)


def unified_inputs(host: str) -> "tuple[list, Any]":
    """(queued demand that may run on ``host``, Priority) from the shared kanban root. An unreadable priority file is
    the built-in default (said once by admission_demand); an unreadable demand dir holds nothing back."""
    from hermes_cli.fleet_placement import admission as _ad
    from hermes_cli.fleet_placement import admission_demand as _dmd
    from hermes_cli.fleet_placement import placement as _pl

    try:
        # resolve folds aliases on both sides of a demand's ``hosts`` restriction, as may_start does (Prism f6d8d4445d4c)
        try:
            resolve = _pl.load_hosts().resolve
        except Exception:  # no table: string match only, as before
            resolve = None
        return _dmd.read_demand(host, resolve=resolve), _dmd.load_priority()
    except Exception as exc:  # never a crash in the tick: no demand, the default line
        _log.warning("kanban load gate: unified admission inputs unreadable (%s); no holdback this tick", exc)
        return [], _ad.Priority()


def publish_ready_demand(queued: int, root=None) -> Optional[str]:
    """Kanban's queue for the ONE rule (t_3694ab3c), published through allocd's pair door as kanban@<host> for
    EVERY table host (hermes-home #9223 deleted the local writer): a portable card may run on any of them, which the
    old ``hosts: null`` file said and the door's per-pair ``hosts`` union now says. Unit = the table's kanban
    footprint (the prior work_estimate seeds from). Demand is advisory, so a failed publish never stops the tick: it
    returns the error text (None = published) for the caller to log, and the next tick publishes again (Prism
    8397e82995e1).

    Fire-and-forget (fault-taxonomy ruling 2026-11-05): the whole call spends at most PUBLISH_BUDGET_S across every
    host (a host left when the budget is gone is skipped, not waited for), never raises, and is counted in
    PUBLISH_STATS (ok / failed / skipped hosts). A host with no local publisher token is skipped, not failed, and is
    re-checked every pass (t_93618cb2)."""
    try:
        return _publish_ready_demand(queued, root)
    except Exception as exc:  # advisory: nothing here may fail the tick
        PUBLISH_STATS["failed"] += 1
        return f"{type(exc).__name__}: {exc}"


PUBLISH_BUDGET_S = 1.0
PUBLISH_STATS = {"ok": 0, "failed": 0, "skipped": 0}
TOKENLESS_REFUSAL = "provisioned demand publisher token required"
_TOKENLESS_LOGGED = set()                 # host names already logged as token-less; gates the log line only


def _publish_ready_demand(queued, root):
    import time as _time
    from hermes_cli.fleet_placement import alloc_client as _ac
    from hermes_cli.fleet_placement import admission as _ad
    from hermes_cli.fleet_placement import placement as _pl

    from hermes_cli import kanban_priority_door as door
    if door.enabled():
        return None  # routed publisher owns host-specific waiting and verdicts
    unit = _ad.Estimate(cpus=1.0, ram_gb=3.0, source="prior")
    try:
        table = _pl.load_hosts(None if root is None else Path(root) / _pl.TABLE_REL)
        unit = _pl.need_of(table.footprint(UNIFIED_WORKLOAD))
    except Exception as exc:
        PUBLISH_STATS["failed"] += 1
        return f"{type(exc).__name__}: {exc}"
    errors = []
    deadline = _time.monotonic() + PUBLISH_BUDGET_S
    for hid in sorted(table.hosts):
        left = deadline - _time.monotonic()
        if left <= 0.01:
            PUBLISH_STATS["skipped"] += 1
            errors.append(f"{hid}: skipped (publish budget {PUBLISH_BUDGET_S} s spent)")
            continue
        try:
            _ac.publish_queue(UNIFIED_WORKLOAD, hid, int(queued),
                              {"cpus": unit.cpus, "ram_gb": unit.ram_gb, "gpu_mib": unit.gpu_mib},
                              unified=unified_admit(), timeout=left)
            PUBLISH_STATS["ok"] += 1
        except _ac.Refused as exc:
            if isinstance(exc, _ac.AuthorizeRefused) or str(exc) != TOKENLESS_REFUSAL:
                PUBLISH_STATS["failed"] += 1
                errors.append(f"{hid}: {type(exc).__name__}: {exc}")
                continue
            # a local token-file check, not an answer from a host: re-checked every pass (a token provisioned
            # later is used at once), never a failure, logged once per host per process (t_93618cb2)
            PUBLISH_STATS["skipped"] += 1
            if hid not in _TOKENLESS_LOGGED:
                _TOKENLESS_LOGGED.add(hid)
                _log.warning("kanban demand: %s has no demand publisher token; not published until one is "
                             "provisioned (checked every tick, logged once)", hid)
        except Exception as exc:
            PUBLISH_STATS["failed"] += 1
            errors.append(f"{hid}: {type(exc).__name__}: {exc}")
    return "; ".join(errors) or None


def utilization_context() -> dict:
    """This host's inputs for the utilization path: its row of the placement
    table (via the ``local`` alias), the kanban ceiling + reason, memory from
    psutil, and the measured estimate for a kanban worker here."""
    from hermes_cli.fleet_placement import admission as _ad
    from hermes_cli.fleet_placement import placement as _pl
    from hermes_cli.fleet_placement import work_estimate as _we

    host, spec, ceiling, reason, prior = "local", None, None, "", _ad.Estimate(cpus=1.0, ram_gb=3.0)
    try:
        table = _pl.load_hosts()
        hid = table.resolve(UTIL_HOST_ALIAS)
        if hid:
            host, spec = hid, table.hosts[hid]
            ceiling = spec.ceiling_for("kanban")
            reason = str(((json.loads(table.path.read_text(encoding="utf-8-sig"))["hosts"][hid].get("consumers") or {})
                          .get("kanban") or {}).get("_ceiling_reason") or "")
        prior = _pl.need_of(table.footprint("kanban"))
    except Exception as exc:  # no table = no ceiling, the prior footprint; never a crash in the tick
        _log.debug("kanban load gate: placement table unavailable: %s", exc)
    try:
        hist = _ad.read_history(_pl.class_usage_path(), since=time.time() - _pl.CLASS_USAGE_WINDOW_S)
    except Exception:
        hist = []
    need = _we.estimate(UTIL_CLASS, host, hist, seed=prior)
    avail = total = None
    try:
        import psutil

        vm = psutil.virtual_memory()
        avail, total = vm.available / 2 ** 30, vm.total / 2 ** 30
    except Exception:
        pass
    limits = _ad.Limits(mem_reserve_gb=spec.mem_reserve_gb if spec else 0.0,
                        cpu_saturable=bool(spec.cpu_saturable) if spec else False,
                        # R2c closures follow this host's scorer, as placement._limits() does (Prism 8e21846c06c9)
                        r2c=spec is not None and spec.scorer == "v2",
                        unmeasurable=tuple(a for a in (spec.unmeasurable_axes if spec else ()) if a.startswith("psi_")))
    return {"host": host, "ceiling": ceiling, "ceiling_reason": reason or "no reason recorded", "need": need,
            "limits": limits, "mem_available_gb": avail, "mem_total_gb": total}


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
    l1_txt = "?" if l1 is None else f"{float(l1):.1f}"
    return (
        f"Load gate: {state.get('state')} admission={state.get('admission')} "
        f"cpu_busy={state.get('cpu_busy')} "
        f"load1={l1_txt} "
        f"load5={'?' if l5 is None else f'{float(l5):.1f}'} "
        f"(load {l1_txt} tasks / {_denom(state.get('ncpu'))}, not read) "
        f"allowance={state.get('allowance')} "
        f"admitted_last_tick={state.get('admitted_last_tick')} "
        f"max_spawn_per_tick={state.get('max_spawn_per_tick')} "
        f"procs={state.get('procs')}/{state.get('proc_limit')} "
        f"band={state.get('band')}/{state.get('spill_reason') or '-'} "
        f"(updated {age:.0f}s ago)"
    )
