"""Minimal vendored reader of ``fleet/allocation-policy.json`` for the kanban adapter.

Renamed from ``hermes_cli.placement_policy`` / ``placement-policy.json`` in rename wave 3 (t_7be48220,
t_fc257547). For one release the old names stay readable: ``hermes_cli.placement_policy`` is a counted
sys.modules alias of this module, and a fleet dir holding only the old file name loads it, counted in
``<kanban home>/var/ci-box/deprecated-name-reads.log`` (hermes-home ``allocation_policy.count_deprecated_read``
format).

Placement PRD v0.4 (``plans/2026-10-04_fleet-resource-aware-placement-PRD.md``)
§5.3. The canonical loader is hermes-home ``scripts/lib/allocation_policy.py``;
the fork may not import hermes-home code, so this reads only the keys the
kanban adapter needs (Phase 1b, t_1e4b9684):

* ``stale_after_s`` and ``pressure_path.linux`` (I-3, RC9);
* ``classes.<class>.kanban`` warm/hot ``load_ratio`` + ``hot_streak`` /
  ``clear_streak`` (F-5);
* ``consumers.<c>`` ``cpu_est_prior`` / ``ramp_s`` / ``ttl_s`` (I-8, RC5);
* ``hosts.<h>`` ``class`` and ``max_slots.<c>`` (I-7 clamp);
* ``hosts.<h>.targets`` (I-1: ``false`` = never a placement target).
* ``host_preference.last_resort`` / ``consumers.kanban`` (t_c2fce87e: the Studio is
  the last resort; canonical reader hermes-home ``scripts/lib/host_preference.py``).

Until the file is promoted to schema 1 (Phase 1a), ``_FALLBACK`` carries the
PRD §5.3 numbers (each one lifted from the code named in the Phase 0 draft).
A key the schema-1 file sets always wins. Stdlib only, never raises.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

POLICY_FILE = "allocation-policy.json"
LEGACY_POLICY_FILE = "placement-policy.json"   # read only when POLICY_FILE is absent, counted (one release)
CONSUMER = "kanban"
HOST_PREFERENCE = "host_preference"        # t_c2fce87e: the fleet's "Studio last" setting
LAST_RESORT_ENV = "FLEET_LAST_RESORT_HOSTS"

_log = logging.getLogger(__name__)


def _reader_tag() -> str:
    """`` ppid=N argv=<own identity> parent=<parent identity> cwd=<cwd>`` for a deprecated-name-reads line, so a reader
    that has exited by the time someone reads the log can still be named (t_5931d620; a pid alone never could).
    Identity is NAMES only, never an argument value (Prism 80bdcd765cc9 / 918779febe18: a command line can carry a
    credential): the executable's basename, then the first of `-c` (its program text is never written), `-m <module>`
    or an existing script path; no other argv word. Whitespace becomes ``+``, 160 chars a field. Never raises.
    One text in hermes-home scripts/lib and the hermes-agent copies (t_bab23582)."""
    import sys

    def ident(argv):
        argv = [str(a) for a in argv if a]
        if not argv:
            return "-"
        out = [os.path.basename(argv[0])]
        for i, a in enumerate(argv[1:], 1):
            if a == "-c":
                out.append("-c")
                break
            if a == "-m":
                if i + 1 < len(argv) and argv[i + 1].replace(".", "_").isidentifier():
                    out += ["-m", argv[i + 1]]
                break
            try:
                script = not a.startswith("-") and os.path.isfile(a)
            except Exception:  # noqa: BLE001 -- one unreadable word must not drop the whole tag
                script = False
            if script:
                out.append(a)
                break
        return "".join("+" if c.isspace() else c for c in " ".join(out))[:160] or "-"
    try:
        ppid = os.getppid()
        try:
            with open(f"/proc/{ppid}/cmdline", "rb") as fh:
                parent = [p.decode("utf-8", "replace") for p in fh.read().split(b"\0")]
        except OSError:   # macOS: no /proc
            import subprocess
            parent = subprocess.run(["ps", "-o", "command=", "-p", str(ppid)], capture_output=True, text=True,
                                    timeout=2).stdout.split()
        try:
            cwd = os.getcwd()
        except OSError:
            cwd = ""
        own = getattr(sys, "orig_argv", None) or [sys.executable] + list(sys.argv)
        cwd = "".join("+" if c.isspace() else c for c in cwd)[:160] or "-"
        return f" ppid={ppid} argv={ident(own)} parent={ident(parent)} cwd={cwd}"
    except Exception:  # noqa: BLE001 -- a counter must never break a reader
        return ""


def _clock_shifted() -> bool:
    """A clock-shifted test process (hermes-home scripts/lib/clock_shift.py exports both) stamps a FUTURE date: never
    count it into a real log (t_5931d620: 191 rows dated 2026-12 .. 2027-12)."""
    return bool(os.environ.get("HERMES_CLOCK_SHIFT_DAYS") or os.environ.get("FAKETIME"))


def count_deprecated_read(name: str) -> None:
    """Append ``<utc iso> <name> pid=<pid> ppid=.. argv=.. parent=.. cwd=..`` to <kanban home>/var/ci-box/
    deprecated-name-reads.log, the log hermes-home ``allocation_policy.count_deprecated_read`` writes. A
    clock-shifted process writes nothing. Never raises."""
    if _clock_shifted():
        return
    try:
        import datetime

        from hermes_cli import kanban_db as _kb

        d = _kb.kanban_home() / "var" / "ci-box"
        d.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(d / "deprecated-name-reads.log", "a", encoding="utf-8") as fh:
            fh.write(f"{ts} {name} pid={os.getpid()}{_reader_tag()}\n")
    except Exception:  # noqa: BLE001 -- a counter must never break a reader
        pass


def policy_path(fleet_dir: Path) -> Path:
    """``<fleet_dir>/allocation-policy.json``; a dir holding only the legacy name resolves to that file, counted."""
    new = Path(fleet_dir) / POLICY_FILE
    if not new.exists():
        old = Path(fleet_dir) / LEGACY_POLICY_FILE
        if old.exists():
            count_deprecated_read(f"file:{old}")
            return old
    return new

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
        "mac-studio": {"class": "studio-source", "targets": False},
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

    def targets(self, host: str) -> bool:
        """False iff the policy row says ``targets: false`` (I-1). Any other
        value, or no row, is a target."""
        return self._pick("hosts", host, "targets") is not False

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

    def is_last_resort(self, host: str, consumer: str = CONSUMER) -> bool:
        """``host_preference`` (t_c2fce87e): ``host`` gets ``consumer``'s work only as a last resort.

        Mirrors hermes-home ``scripts/lib/host_preference.is_last_resort(host, consumer=...)``
        and holds no host list: the file's ``last_resort`` (env ``FLEET_LAST_RESORT_HOSTS``
        replaces it; empty = off) and ``consumers.<consumer> == "on"``. No key, an off switch
        or the PRD fallback doc = False, i.e. today's behaviour. No ``_FALLBACK`` entry.
        """
        blk = _get(self.doc, HOST_PREFERENCE)
        if not isinstance(blk, Mapping) or _get(blk, "consumers", consumer) != "on":
            return False
        env = os.environ.get(LAST_RESORT_ENV)
        if env is not None:
            names = [n for n in re.split(r"[,\s]+", env.lower()) if n]
        else:
            raw = blk.get("last_resort")
            names = [n.strip().lower() for n in raw if isinstance(n, str)] if isinstance(raw, list) else []
        return isinstance(host, str) and host.strip().lower() in names


def load(fleet_dir: Path) -> PlacementPolicy:
    """The policy, or the PRD fallback when the file is absent, unreadable or
    still the schema-0 draft (logged)."""
    path = policy_path(fleet_dir)
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
