"""Kanban dispatch admission on the TARGET host's memory + published band.

Saturation spec §4.2 row 1 (``plans/2026-10-12_fleet-work-allocation-saturation-SPEC.md``,
card t_31350981): a pool host takes kanban workers only while
``mem_available_gb >= n x per-worker reservation`` and the band the host's
sampler publishes for kanban (``band.kanban`` in host-pressure.json) is not
``hot``.

This module reads the sampler's verdict; it never computes a band (that is
``scripts/host-pressure.py``, cards t_260e8379 / t_499e107f). A pressure doc
with no ``mem_available_gb`` or no ``band.kanban`` sets no cap on that axis:
the planner's UNKNOWN path already refuses an unreadable or stale file.

Changing the dispatch load metric needs Ace's explicit yes (standing rule D1),
so ``kanban.placement.admit_on_memory`` defaults to false. Off, the cap is
computed, logged per tick (``would-refuse N``) and recorded on the plan, and
no slot changes. On, a host's free slots are cut to the cap.
"""

from __future__ import annotations

import json
import math
from typing import Mapping, Optional, Tuple

DEFAULT_RESERVATION_GB = 6.0
SATURATED_BANDS = frozenset({"hot"})


def _placement(kanban_cfg) -> Mapping:
    pl = kanban_cfg.get("placement") if isinstance(kanban_cfg, Mapping) else None
    return pl if isinstance(pl, Mapping) else {}


def enabled(kanban_cfg) -> bool:
    """``kanban.placement.admit_on_memory`` (default false; D1 gate)."""
    return _placement(kanban_cfg).get("admit_on_memory") is True


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def reservation_gb(kanban_cfg) -> float:
    """``kanban.placement.worker_reservation_gb`` (default 6)."""
    v = _num(_placement(kanban_cfg).get("worker_reservation_gb"))
    return v if v is not None and v > 0 else DEFAULT_RESERVATION_GB


def cap(pressure_text: str, *, reservation: float) -> Tuple[Optional[int], str]:
    """``(max new workers, why)`` for one host this tick; ``(None, "")`` = no cap.

    The memory axis allows ``floor(mem_available_gb / reservation)``; a hot
    ``band.kanban`` allows 0. ``why`` names every axis that capped.
    """
    try:
        doc = json.loads(pressure_text) if pressure_text else None
    except ValueError:
        doc = None
    if not isinstance(doc, Mapping):
        return None, ""
    limit: Optional[int] = None
    why = []
    mem = _num(doc.get("mem_available_gb"))
    if mem is not None:
        limit = max(0, int(math.floor(mem / reservation)))
        why.append(f"mem_available {mem:g} GB / {reservation:g} GB reservation = {limit}")
    band_why = _band_saturated(doc)
    if band_why:
        limit = 0
        why.append(band_why)
    return limit, "; ".join(why)


def band_cap(pressure_text: str) -> str:
    """The band axis of :func:`cap` alone: ``"band.kanban hot (...)"`` when the host's PUBLISHED kanban band is
    saturated, else "". The unified rule retires the memory axis only (D9); this axis still closes the host
    (Prism ce581503f8d8, t_c466b7ee)."""
    try:
        doc = json.loads(pressure_text) if pressure_text else None
    except ValueError:
        doc = None
    return _band_saturated(doc) if isinstance(doc, Mapping) else ""


def _band_saturated(doc: Mapping) -> str:
    bands = doc.get("band")
    band = bands.get("kanban") if isinstance(bands, Mapping) else None
    if not (isinstance(band, str) and band in SATURATED_BANDS):
        return ""
    reasons = doc.get("band_reason")
    detail = reasons.get("kanban") if isinstance(reasons, Mapping) else None
    return f"band.kanban {band}" + (f" ({detail})" if detail else "")


def readback_line(refused: Mapping[str, str], *, enforcing: bool, reservation: float) -> str:
    """The per-tick line: ``would-refuse N`` (flag off) or ``refused N`` (on)."""
    verb = "refused" if enforcing else "would-refuse"
    mode = "enforce" if enforcing else "dry"
    body = "; ".join(f"{h}: {w}" for h, w in sorted(refused.items())) or "-"
    return (f"kanban admission ({mode}, reservation {reservation:g} GB): "
            f"{verb} {len(refused)} ({body})")
