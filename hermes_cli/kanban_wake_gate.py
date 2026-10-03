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
``sample_seconds``); unset thresholds inherit ``dispatch_load_gate``.

State is written to ``<kanban_home>/wake_gate.json`` on every transition and
summary so ``hermes kanban notify-status`` can show the live mode.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

from hermes_cli.kanban_load_gate import LoadGate

_log = logging.getLogger(__name__)

STATE_FILENAME = "wake_gate.json"
DEFAULT_SAMPLE_SECONDS = 15.0
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
        lane_probe: Optional[Callable[[str], Optional[str]]] = None,
        state_file: Optional[Path] = None,
    ) -> None:
        cfg = _merged_cfg(config)
        self.enabled = bool(cfg["enabled"])
        self.lane_headroom = bool(cfg["lane_headroom"])
        try:
            self.sample_seconds = max(0.0, float(cfg["sample_seconds"]))
        except (TypeError, ValueError):
            self.sample_seconds = DEFAULT_SAMPLE_SECONDS
        self._gate = LoadGate(
            {
                "enabled": self.enabled,
                "pause_above": cfg["pause_above"],
                "resume_below": cfg["resume_below"],
                "load5_floor": cfg["load5_floor"],
            },
            ncpu or os.cpu_count() or 1,
        )
        self._loadavg = loadavg
        self._lane_probe = lane_probe or _default_lane_probe
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
        """``"host load 71"`` while the load band is tripped, else None."""
        if not self.enabled:
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
            self._write_state()
        if self._gate.paused:
            shown = self.load1 if self.load1 is not None else self.pause_above
            return f"host load {shown:.0f}"
        return None

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
    ) -> Optional[str]:
        """Why a wake for ``profile``'s session is sent as a notify, or None."""
        return self.host_contended(now) or self.lane_contended(profile, now)

    # -- observability -----------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        mode = "off" if not self.enabled else ("notify" if self._gate.paused else "wake")
        return {
            "mode": mode,
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
                    "kanban wake gate: mode %s -> %s (load1=%s, pause_above=%.0f, "
                    "resume_below=%.0f)",
                    self._last_written, snap["mode"], snap["load1"],
                    self.pause_above, self.resume_below,
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


def state_path() -> Path:
    from hermes_cli.kanban_db import kanban_home

    return kanban_home() / STATE_FILENAME


def read_state(path: Optional[Path] = None) -> Optional[dict]:
    try:
        data = json.loads((path or state_path()).read_text(encoding="utf-8"))
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
