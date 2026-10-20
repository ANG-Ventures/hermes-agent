"""Contention gate for kanban WAKES (Ace 2026-10-03 13:28/13:40, t_74bf5296).

Ace's rule: "Notify tells the human, Wake tells the agent, Notify plus Wake
does both. By default I always want Notify, and Wake too, except if we have
resource contention. Then we turn Wake off."

So a ``notify+wake`` / ``wake`` subscription is DOWNGRADED to a plain notify
for one EVENT when either is true at delivery time:

* host load: the same hysteresis band as the dispatcher's spawn gate
  (``kanban.dispatch_load_gate`` ``pause_above`` / ``resume_below``, 64 / 48
  on the Studio, plus the load5 floor). A dedicated :class:`LoadGate`
  instance is fed ``os.getloadavg()``, so the band applies here too: it trips
  above ``pause_above`` and clears only below ``resume_below``, and does not
  flap at the bar.
* lane headroom: the waker profile's model lane is out of capacity
  (``kanban_provider_health.capped_provider``, the probe the dispatcher uses
  before a spawn: relay ``eligible_count`` / box ``five_hour`` rejection).

The subscription row is never changed. Config: ``kanban.wake_load_gate``
(``enabled``, ``pause_above``, ``resume_below``, ``lane_headroom``,
``sample_seconds``); unset thresholds inherit ``dispatch_load_gate``. With no
``pause_above`` configured in either block the HOST half is off: the
dispatcher's ncpu fallback is a spawn heuristic, not a measured band, and on
a 4-core box it would silence every wake whenever load1 > 4.

State is written to ``<kanban_home>/wake_gate.json`` on every transition and
summary so ``hermes kanban notify-status`` can show the live mode.

2026-10-12 05:38 (t_b7b31563, Ace 06:25): a review handoff to ``human:apollo``
was downgraded on ``host load 184`` alone and the chain sat 25 min. For a wake
whose owner is a HUMAN session the host half is no longer load1 by itself:
host load counts only while the dispatcher's own LoadGate is ``proc_paused``
(read from ``load_gate.json``); the lane-headroom probe still applies. The
notifier then DEFERS such a downgrade (``gateway.kanban_wake_defer``) instead
of dropping it.

2026-10-18 (t_0e52c7ab, Ace 22:22): "overloaded" is measured, not load1. The
Studio ran load1 166-180 with CPU 53 % idle (a run queue of short-lived
processes), and the load band silenced every advisor completion wake. The host
half now reads CPU busy from ``cpu_times()`` tick deltas, the dispatcher's own
axis (``sample_cpu_busy``), and trips at the dispatcher's ``cpu_pause`` (0.90).
``kanban.wake_load_gate.host_axis: load1`` restores the load band (kill switch).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

from hermes_cli.kanban_load_gate import LoadGate, _denom, sample_cpu_busy

_log = logging.getLogger(__name__)

STATE_FILENAME = "wake_gate.json"
DEFAULT_SAMPLE_SECONDS = 15.0
DEFAULT_CPU_PAUSE = 0.90
DEFAULT_CPU_MAX_INTERVAL = 300.0  # seconds; an older CPU baseline is not "now"
HOST_AXES = ("cpu", "load1")
LANE_CACHE_SECONDS = 60.0


def _merged_cfg(config: Optional[dict]) -> dict:
    kanban = (config or {}).get("kanban") if isinstance(config, dict) else None
    kanban = kanban if isinstance(kanban, dict) else {}
    dispatch = kanban.get("dispatch_load_gate")
    dispatch = dispatch if isinstance(dispatch, dict) else {}
    own = kanban.get("wake_load_gate")
    own = own if isinstance(own, dict) else {}
    merged = {
        "enabled": own.get("enabled", True),
        "pause_above": own.get("pause_above") or dispatch.get("pause_above"),
        "resume_below": own.get("resume_below") or dispatch.get("resume_below"),
        "load5_floor": own.get("load5_floor", dispatch.get("load5_floor", True)),
        "lane_headroom": own.get("lane_headroom", True),
        "sample_seconds": own.get("sample_seconds", DEFAULT_SAMPLE_SECONDS),
        "host_axis": own.get("host_axis", "cpu"),
        "cpu_pause": own.get("cpu_pause") or dispatch.get("cpu_pause") or DEFAULT_CPU_PAUSE,
        "cpu_max_interval": own.get("cpu_max_interval", DEFAULT_CPU_MAX_INTERVAL),
    }
    return merged


class WakeGate:
    """Per-event wake -> notify downgrade under measured contention."""

    def __init__(
        self,
        config: Optional[dict] = None,
        *,
        ncpu: Optional[int] = None,
        loadavg: Callable[[], tuple] = os.getloadavg,
        cpu_sampler: Callable = sample_cpu_busy,
        lane_probe: Optional[Callable[[str], Optional[str]]] = None,
        dispatcher_state: Optional[Callable[[], Optional[dict]]] = None,
        state_file: Optional[Path] = None,
    ) -> None:
        cfg = _merged_cfg(config)
        self.enabled = bool(cfg["enabled"])
        self.host_gate = self.enabled and cfg["pause_above"] is not None
        self.lane_headroom = bool(cfg["lane_headroom"])
        axis = str(cfg["host_axis"]).strip().lower()
        self.host_axis = axis if axis in HOST_AXES else "cpu"
        try:
            self.cpu_pause = float(cfg["cpu_pause"])
        except (TypeError, ValueError):
            self.cpu_pause = DEFAULT_CPU_PAUSE
        self._cpu_sampler = cpu_sampler
        self._cpu_prev = None
        self._cpu_prev_at: Optional[float] = None
        try:
            self.cpu_max_interval = max(1.0, float(cfg["cpu_max_interval"]))
        except (TypeError, ValueError):
            self.cpu_max_interval = DEFAULT_CPU_MAX_INTERVAL
        self.cpu_busy: Optional[float] = None
        try:
            self.sample_seconds = max(0.0, float(cfg["sample_seconds"]))
        except (TypeError, ValueError):
            self.sample_seconds = DEFAULT_SAMPLE_SECONDS
        # A baseline is only refreshed once per sample_seconds, so a max interval
        # shorter than that (plus a poll's slack) expires EVERY baseline before the
        # next sample can use it and the CPU axis never trips (Prism 6360f05606ba).
        # Floor it at twice the sampling cadence.
        self.cpu_max_interval = max(self.cpu_max_interval, 2.0 * self.sample_seconds)
        self._gate = LoadGate(
            {
                "enabled": self.host_gate,
                "pause_above": cfg["pause_above"],
                "resume_below": cfg["resume_below"],
                "load5_floor": cfg["load5_floor"],
            },
            ncpu or os.cpu_count() or 1,
        )
        self._loadavg = loadavg
        self._lane_probe = lane_probe or _default_lane_probe
        self._dispatcher_state = dispatcher_state or _default_dispatcher_state
        self._lane_cache: dict[str, tuple[float, Optional[str]]] = {}
        self._sampled_at: Optional[float] = None
        self.load1: Optional[float] = None
        self.load5: Optional[float] = None
        self._state_file = state_file
        self._last_written: Optional[str] = None

    @property
    def pause_above(self) -> float:
        return self._gate.pause_above

    @property
    def resume_below(self) -> float:
        return self._gate.resume_below

    # -- host load ---------------------------------------------------------
    def host_contended(self, now: Optional[float] = None) -> Optional[str]:
        """``"host cpu 95% busy (load1 172)"`` while the CPU is saturated, else None.

        ``host_axis: load1`` (kill switch) answers ``"host load 71"`` from the
        load band instead. On the cpu axis an unreadable CPU sample (no psutil,
        first sample, empty interval) is not evidence of contention.
        """
        if not self.host_gate:
            return None
        now = time.monotonic() if now is None else float(now)
        if self._sampled_at is None or now - self._sampled_at >= self.sample_seconds:
            try:
                avg = self._loadavg()
                self.load1, self.load5 = float(avg[0]), float(avg[1])
            except (OSError, TypeError, ValueError, IndexError):
                self.load1 = self.load5 = None
            self._sampled_at = now
            if self.load1 is not None:
                self._gate.update(self.load1, self.load5)
            if self.host_axis == "cpu":
                # Busy is an average over [prev, now]. Wakes can be hours apart, and an
                # hours-long average says nothing about the CPU right now: a baseline
                # older than cpu_max_interval is dropped and this sample fails open
                # toward WAKE (the next event inside the window reads a fresh delta).
                if (self._cpu_prev_at is not None
                        and now - self._cpu_prev_at > self.cpu_max_interval):
                    self._cpu_prev = None
                before = self._cpu_prev
                try:
                    self.cpu_busy, self._cpu_prev = self._cpu_sampler(before)
                except Exception:
                    self.cpu_busy = None
                # Stamp the baseline only when the sampler handed back a NEW snapshot. A
                # failed read keeps the old snapshot (or returns it unchanged); stamping it
                # `now` would let an hours-old baseline pass as fresh (Prism eb61a0216806).
                if self._cpu_prev is None:
                    self._cpu_prev_at = None
                elif self._cpu_prev is not before:
                    self._cpu_prev_at = now
            self._write_state()
        if self.host_axis == "cpu":
            if self._cpu_saturated():
                load = f" (load1 {self.load1:.0f})" if self.load1 is not None else ""
                return f"host cpu {self.cpu_busy * 100:.0f}% busy{load}"
            return None
        if self._gate.paused:
            shown = self.load1 if self.load1 is not None else self.pause_above
            return f"host load {shown:.0f}"
        return None

    def _cpu_saturated(self) -> bool:
        return self.cpu_busy is not None and self.cpu_busy >= self.cpu_pause

    # -- lane headroom -----------------------------------------------------
    def lane_contended(self, profile: Optional[str], now: Optional[float] = None) -> Optional[str]:
        if not (self.enabled and self.lane_headroom and profile):
            return None
        now = time.monotonic() if now is None else float(now)
        hit = self._lane_cache.get(profile)
        if hit is not None and now - hit[0] < LANE_CACHE_SECONDS:
            return hit[1]
        try:
            reason = self._lane_probe(profile)
        except Exception:
            reason = None  # an unreadable probe is not evidence of contention
        self._lane_cache[profile] = (now, reason)
        return reason

    def downgrade_reason(
        self, profile: Optional[str] = None, now: Optional[float] = None,
        *, human_owner: bool = False,
    ) -> Optional[str]:
        """Why a wake for ``profile``'s session is sent as a notify, or None.

        ``human_owner``: the wake targets a human-owned operator session (a
        ``human:*`` reviewer/owner). Host load1 alone never blocks it; the
        host half counts only while the dispatcher LoadGate is ``proc_paused``
        (t_b7b31563), and the lane probe still applies.
        """
        host = self.host_contended(now)
        if host and human_owner:
            host = self.dispatcher_proc_paused()
        return host or self.lane_contended(profile, now)

    def dispatcher_proc_paused(self) -> Optional[str]:
        """``"dispatcher proc_paused"`` while the spawn gate is in that state, else None.

        Read from the dispatcher's own ``load_gate.json`` (one gateway writes it
        per tick). An unreadable or absent state is not evidence of contention.
        """
        try:
            state = self._dispatcher_state()
        except Exception:
            return None
        if isinstance(state, dict) and state.get("state") == "proc_paused":
            return "dispatcher proc_paused"
        return None

    # -- observability -----------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        if not self.host_gate:
            mode = "off"
        elif self.host_axis == "cpu":
            mode = "notify" if self._cpu_saturated() else "wake"
        else:
            mode = "notify" if self._gate.paused else "wake"
        return {
            "mode": mode,
            "host_axis": self.host_axis,
            "cpu_busy": self.cpu_busy,
            "cpu_pause": self.cpu_pause,
            "load1": self.load1,
            "load5": self.load5,
            "pause_above": self.pause_above,
            "resume_below": self.resume_below,
            "lane_headroom": self.lane_headroom,
            "lanes_capped": {
                p: r for p, (_, r) in sorted(self._lane_cache.items()) if r
            },
            "updated_at": int(time.time()),
        }

    def _write_state(self) -> None:
        snap = self.snapshot()
        path = self._state_file
        if path is None:
            try:
                path = state_path()
            except Exception:
                return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(snap, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            return
        if snap["mode"] != self._last_written:
            if self._last_written is not None:
                _log.info(
                    "kanban wake gate: mode %s -> %s (axis=%s, cpu_busy=%s, cpu_pause=%.2f, "
                    "load1=%s, pause_above=%.0f, resume_below=%.0f; load %s tasks / %s)",
                    self._last_written, snap["mode"], self.host_axis, snap["cpu_busy"],
                    self.cpu_pause, snap["load1"],
                    self.pause_above, self.resume_below, snap["load1"], _denom(self._gate.ncpu),
                )
            self._last_written = snap["mode"]


def _default_lane_probe(profile: str) -> Optional[str]:
    """``"lane <provider> capped"`` when ``profile``'s model lane has no headroom."""
    from hermes_cli import kanban_provider_health as _ph

    task = SimpleNamespace(assignee=profile, model_override=None, provider_override=None)
    capped = _ph.capped_provider(
        task,
        _ph.configured_probes(),
        {},
        min_eligible=_ph.configured_min_eligible(),
        pool_urls=_ph.configured_pool_health_urls(),
        box_health=_ph.configured_box_health(),
    )
    if not capped:
        return None
    return f"lane {capped.get('provider') or '?'} capped"


def _default_dispatcher_state() -> Optional[dict]:
    """The dispatcher LoadGate's published ``load_gate.json`` (None when absent)."""
    from hermes_cli.kanban_load_gate import read_state as _read_gate_state

    return _read_gate_state()


def state_path() -> Path:
    from hermes_cli.kanban_db import kanban_home

    return kanban_home() / STATE_FILENAME


def read_state(path: Optional[Path] = None) -> Optional[dict]:
    try:
        data = json.loads((path or state_path()).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def gate_from_config(config: Optional[dict] = None) -> WakeGate:
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            config = {}
    return WakeGate(config)
