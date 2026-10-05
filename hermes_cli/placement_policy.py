"""Minimal vendored reader of ``fleet/placement-policy.json`` for the kanban adapter.

Placement PRD v0.4 (``plans/2026-10-04_fleet-resource-aware-placement-PRD.md``)
§5.3. The canonical loader is hermes-home ``scripts/lib/placement_policy.py``;
the fork may not import hermes-home code, so this reads only the keys the
kanban adapter needs (Phase 1b, t_1e4b9684):

* ``stale_after_s`` and ``pressure_path.linux`` (I-3, RC9);
* ``classes.<class>.kanban`` warm/hot ``load_ratio`` + ``hot_streak`` /
  ``clear_streak`` (F-5);
* ``consumers.<c>`` ``cpu_est_prior`` / ``ramp_s`` / ``ttl_s`` (I-8, RC5);
* ``hosts.<h>`` ``class`` and ``max_slots.<c>`` (I-7 clamp).

Until the file is promoted to schema 1 (Phase 1a), ``_FALLBACK`` carries the
PRD §5.3 numbers (each one lifted from the code named in the Phase 0 draft).
A key the schema-1 file sets always wins. Stdlib only, never raises.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

POLICY_FILE = "placement-policy.json"
CONSUMER = "kanban"

_log = logging.getLogger(__name__)

_FALLBACK: Dict[str, Any] = {
    "stale_after_s": 120,
    "pressure_path": {"linux": "/var/lib/placement/host-pressure.json"},
    "classes": {"linux-shared": {"kanban": {
        "warm": {"load_ratio": 0.70}, "hot": {"load_ratio": 0.80},
        "hot_streak": 2, "clear_streak": 3}}},
    "consumers": {
        "kanban": {"cpu_est_prior": 2.0, "ramp_s": 600, "ttl_s": 180},
        "ci": {"cpu_est_prior": 3.0, "ramp_s": 0, "ttl_s": 900},
        "prism": {"cpu_est_prior": 1.0, "ramp_s": 60, "ttl_s": 180},
    },
    "hosts": {
        "ace-ai": {"class": "linux-shared", "max_slots": {"kanban": 4, "ci": 8, "prism": 12}},
        "ace-media": {"class": "linux-shared", "max_slots": {"kanban": 4, "ci": 8, "prism": 2}},
        "ci-box": {"class": "linux-shared"},
    },
}
DEFAULT_CLASS = "linux-shared"
# A max_slots the policy does not state still bounds a ledger row (I-7 (a)):
# the fleet-roles.json slot validator's ceiling.
UNSTATED_MAX_SLOTS = 64


def _num(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # json.loads accepts NaN/Infinity; int(inf) raises.
    return float(value) if math.isfinite(value) else None


def _get(doc: Mapping, *path, default=None):
    cur: Any = doc
    for key in path:
        if not isinstance(cur, Mapping) or key not in cur:
            return default
        cur = cur[key]
    return cur


@dataclass(frozen=True)
class KanbanBand:
    warm: float
    hot: float
    hot_streak: int
    clear_streak: int


@dataclass(frozen=True)
class PlacementPolicy:
    doc: Mapping = field(default_factory=dict)
    source: str = "fallback"     # "file" (schema 1) or "fallback"

    def _pick(self, *path):
        v = _get(self.doc, *path)
        return v if v is not None else _get(_FALLBACK, *path)

    def _pick_num(self, path, fallback_path=None) -> Optional[float]:
        """A numeric key; a value the file sets with the wrong type falls back."""
        v = _num(_get(self.doc, *path))
        return v if v is not None else _num(_get(_FALLBACK, *(fallback_path or path)))

    def _pick_str(self, *path) -> Optional[str]:
        v = _get(self.doc, *path)
        return v if isinstance(v, str) and v else _get(_FALLBACK, *path)

    @property
    def stale_after_s(self) -> float:
        return self._pick_num(("stale_after_s",)) or 120.0

    @property
    def pressure_path(self) -> str:
        return str(self._pick_str("pressure_path", "linux"))

    def host_class(self, host: str) -> str:
        return self._pick_str("hosts", host, "class") or DEFAULT_CLASS

    def kanban_band(self, host: str) -> KanbanBand:
        cls = self.host_class(host)

        def pick(*p) -> float:
            return self._pick_num(("classes", cls, CONSUMER, *p), ("classes", DEFAULT_CLASS, CONSUMER, *p))

        return KanbanBand(warm=float(pick("warm", "load_ratio")), hot=float(pick("hot", "load_ratio")),
                          hot_streak=int(pick("hot_streak")), clear_streak=int(pick("clear_streak")))

    def consumer(self, name: str, key: str):
        return self._pick_num(("consumers", name, key))

    def max_slots(self, host: str, consumer: str) -> int:
        v = self._pick_num(("hosts", host, "max_slots", consumer))
        return int(v) if v is not None and v >= 0 else UNSTATED_MAX_SLOTS


def load(fleet_dir: Path) -> PlacementPolicy:
    """The policy, or the PRD fallback when the file is absent, unreadable or
    still the schema-0 draft (logged)."""
    path = Path(fleet_dir) / POLICY_FILE
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return PlacementPolicy()
    except (OSError, ValueError) as exc:
        _log.warning("placement policy: %s unreadable (%s); using PRD fallback values", path, exc)
        return PlacementPolicy()
    if not isinstance(doc, dict) or doc.get("schema") != 1:
        return PlacementPolicy()
    return PlacementPolicy(doc=doc, source="file")
