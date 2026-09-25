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

* every worker spawned in the last ``ramp_seconds`` (default 120) is counted
  as ``worker_load_cost`` (default 2.0) of load that load1 has not shown yet;
* a tick admits at most ``ceil((pause_above - load1 - pending_ramp) / cost)``
  new workers (min 0), and never more than ``max_spawn_per_tick`` (default 4);
* resuming from a hard pause additionally requires ``load5 < pause_above``
  (``load5_floor``, default true) so one quiet load1 sample cannot re-open
  the gate while the 5-minute average still says the host is saturated.

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
DEFAULT_RAMP_SECONDS = 120.0
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
        self.ncpu = ncpu
        self.paused = False
        self.reason: Optional[str] = None
        # Observable state (snapshot / logs / diagnostics).
        self.state = "admitting"
        self.load1: Optional[float] = None
        self.load5: Optional[float] = None
        self.pending_ramp = 0.0
        self.allowance: Optional[int] = None
        self.admitted_last_tick = 0
        self.last_reason: Optional[str] = None
        self._ramp: deque = deque()  # (monotonic_ts, n_spawned)
        self._last_logged_state: Optional[str] = None
        self._last_summary_at: Optional[float] = None
        self._admitted_since_summary = 0

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
    def pending(self, now: Optional[float] = None) -> float:
        """Load not yet visible in load1: recent spawns x worker_load_cost."""
        now = time.monotonic() if now is None else float(now)
        horizon = now - self.ramp_seconds
        while self._ramp and self._ramp[0][0] <= horizon:
            self._ramp.popleft()
        return sum(n for _, n in self._ramp) * self.worker_load_cost

    def admit(
        self,
        load1: float,
        *,
        load5: Optional[float] = None,
        now: Optional[float] = None,
    ) -> "tuple[Optional[int], Optional[str]]":
        """Return ``(allowance, reason)`` for this tick.

        ``allowance`` is the max number of NEW workers this tick may spawn
        (``None`` = gate disabled, no limit). ``reason`` is non-None exactly
        when ``allowance == 0`` and explains why.
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
        hard = self.update(load1, self.load5)
        self.pending_ramp = self.pending(now)
        if hard:
            self.state, self.allowance, self.last_reason = "paused", 0, hard
            return 0, hard
        headroom = self.pause_above - load1 - self.pending_ramp
        allowance = 0
        if headroom > 0:
            allowance = int(math.ceil(headroom / self.worker_load_cost))
        allowance = max(0, min(allowance, self.max_spawn_per_tick))
        self.allowance = allowance
        if allowance == 0:
            self.state = "saturated"
            self.last_reason = (
                f"projected load1={load1:.1f} + pending_ramp={self.pending_ramp:.1f} "
                f">= pause_above={self.pause_above:.1f} "
                f"(worker_load_cost={self.worker_load_cost:g}, "
                f"ramp={self.ramp_seconds:g}s)"
            )
            return 0, self.last_reason
        self.state, self.last_reason = "admitting", None
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

    def admit_now(self) -> "tuple[Optional[int], Optional[str]]":
        """:meth:`admit` against the host's live loadavg (inert if unavailable)."""
        load1, load5 = sample_loadavg()
        if load1 is None:
            return None, None
        return self.admit(load1, load5=load5)

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
            "ncpu": self.ncpu,
            "updated_at": time.time(),
        }

    def _line(self) -> str:
        l1 = "?" if self.load1 is None else f"{self.load1:.1f}"
        l5 = "?" if self.load5 is None else f"{self.load5:.1f}"
        return (
            f"state={self.state} load1={l1} load5={l5} "
            f"pending_ramp={self.pending_ramp:.1f} ramp_workers={self.ramp_workers()} "
            f"allowance={self.allowance} admitted={self.admitted_last_tick} "
            f"pause_above={self.pause_above:.1f}"
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
            tmp.write_text(json.dumps(self.snapshot(), sort_keys=True))
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass


def sample_loadavg() -> "tuple[Optional[float], Optional[float]]":
    """(load1, load5) or (None, None) on platforms without getloadavg."""
    try:
        la = os.getloadavg()
    except (AttributeError, OSError):
        return None, None
    return float(la[0]), float(la[1])


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
        data = json.loads(p.read_text())
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
