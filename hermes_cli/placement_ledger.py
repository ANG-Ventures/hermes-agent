"""Cross-consumer reservation ledger + ``projected()`` (Placement PRD v0.4 §5.2, I-4, I-7, I-8, F-6).

One file per consumer, ``<root>/var/placement/host-reservations.<consumer>.json``
on the Studio::

    {"consumer": "kanban", "at": 1791200000, "ttl_s": 180,
     "hosts": {"ace-ai": {"busy_units": 2, "cpu_est": 2.0, "ramp_s": 600,
                          "placed_at": [1791199950.0, 1791199990.0]}}}

* The filename is the writer's identity (I-7 (b)); the reader clamps
  ``busy_units`` to ``hosts.<h>.max_slots.<consumer>`` from the policy and logs
  ``ledger_bounds_violation`` (I-7 (a)).
* A file whose ``at`` is older than its ``ttl_s`` is ignored (I-4): a dead
  writer never pins capacity.
* ``projected(host) = load1 + sum(cpu_est x unrealised_fraction(age, ramp))``
  over every unit with a ``placed_at`` (I-8). ``ramp <= 0`` contributes 0 and a
  unit with no ``placed_at`` contributes 0, never "fresh" (F-6). Same linear
  fade as ``kanban_load_gate.LoadGate.invisible_workers()``.
* ``cpu_est`` in a row is the writer's estimate (kanban writes its measured
  slope when it has one); the policy ``cpu_est_prior`` is used only when the
  row carries none (RC5).

Writes are flock'd read-modify-write with an atomic rename, the
``fleet-host`` kanban-pool.json pattern. Stdlib only.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

try:  # POSIX only; Windows never runs the gateway pool path.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

FILE_PREFIX = "host-reservations."
FILE_SUFFIX = ".json"
LOCK_WAIT_SECONDS = 5.0

_log = logging.getLogger(__name__)


def ledger_dir(root: Path) -> Path:
    return Path(root) / "var" / "placement"


def ledger_path(directory: Path, consumer: str) -> Path:
    return Path(directory) / f"{FILE_PREFIX}{consumer}{FILE_SUFFIX}"


def _num(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


@dataclass(frozen=True)
class Reservation:
    consumer: str
    host: str
    busy_units: int
    cpu_est: float
    ramp_s: float
    placed_at: Tuple[float, ...]


def unrealised_fraction(age: Optional[float], ramp_s: Optional[float]) -> float:
    """Share of a unit's load not yet in load1. ``ramp <= 0`` or an unknown
    age is 0 (F-6): a unit we cannot date is assumed already visible."""
    if age is None or ramp_s is None or ramp_s <= 0:
        return 0.0
    return max(0.0, 1.0 - max(0.0, age) / ramp_s)


def read_all(directory: Path, policy, *, now: Optional[float] = None) -> List[Reservation]:
    """Every live reservation across consumer files, clamped (I-7 (a)).
    Unreadable files and expired files contribute nothing (I-4)."""
    now = time.time() if now is None else float(now)
    out: List[Reservation] = []
    try:
        paths = sorted(Path(directory).glob(f"{FILE_PREFIX}*{FILE_SUFFIX}"))
    except OSError:
        return out
    for path in paths:
        consumer = path.name[len(FILE_PREFIX):-len(FILE_SUFFIX)]
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("hosts"), dict):
            continue
        at = _num(doc.get("at"))
        ttl = _num(doc.get("ttl_s"))
        if ttl is None:
            ttl = _num(policy.consumer(consumer, "ttl_s"))
        if at is None or ttl is None or now - at > ttl:
            continue
        prior = _num(policy.consumer(consumer, "cpu_est_prior")) or 0.0
        pramp = _num(policy.consumer(consumer, "ramp_s"))
        for host, row in doc["hosts"].items():
            if not isinstance(host, str) or not isinstance(row, dict):
                continue
            units = _num(row.get("busy_units"))
            units_i = max(0, int(units)) if units is not None else 0
            cap = policy.max_slots(host, consumer)
            if units_i > cap:
                _log.warning("ledger_bounds_violation: %s busy_units=%d on %s clamped to max_slots %d",
                             consumer, units_i, host, cap)
                units_i = cap
            cpu = _num(row.get("cpu_est"))
            ramp = _num(row.get("ramp_s"))
            stamps = row.get("placed_at")
            stamps = [float(s) for s in stamps if _num(s) is not None] if isinstance(stamps, list) else []
            out.append(Reservation(
                consumer=consumer, host=host, busy_units=units_i,
                cpu_est=cpu if cpu is not None and cpu > 0 else prior,
                ramp_s=ramp if ramp is not None else (pramp if pramp is not None else 0.0),
                placed_at=tuple(sorted(stamps, reverse=True)[:units_i]),
            ))
    return out


def pending(host: str, reservations: Iterable[Reservation], *, now: Optional[float] = None) -> float:
    """Load (cores) placed on ``host`` that load1 does not show yet."""
    now = time.time() if now is None else float(now)
    total = 0.0
    for r in reservations:
        if r.host != host:
            continue
        total += r.cpu_est * sum(unrealised_fraction(now - t, r.ramp_s) for t in r.placed_at)
    return total


def projected(host: str, load1: float, reservations: Iterable[Reservation], *,
              now: Optional[float] = None) -> float:
    """I-8: ``load1 + sum(cpu_est x unrealised_fraction)``. The only formula."""
    return float(load1) + pending(host, reservations, now=now)


# -- writer -------------------------------------------------------------------

@contextlib.contextmanager
def _locked(path: Path, wait_s: float = LOCK_WAIT_SECONDS):
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path.with_name(path.name + ".lock"), "a")  # noqa: SIM115 - closed in finally
    try:
        if fcntl is not None:
            deadline = time.monotonic() + wait_s
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"ledger lock {path} held for {wait_s:g}s") from None
                    time.sleep(0.05)
        yield
    finally:
        fh.close()


def update_json(path: Path, fn: Callable[[Mapping], Mapping]) -> dict:
    """flock'd read-modify-write of one JSON file; ``fn(old) -> new``. The
    rename is atomic, so a reader never sees a torn file."""
    path = Path(path)
    with _locked(path):
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
        new = dict(fn(old if isinstance(old, dict) else {}))
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(new, fh, sort_keys=True)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    return new


def update(directory: Path, consumer: str,
           fn: Callable[[Mapping], Mapping]) -> dict:
    """Read-modify-write of one consumer's ledger file (its name is its identity)."""
    def stamp(old: Mapping) -> Mapping:
        new = dict(fn(old))
        new["consumer"] = consumer
        return new

    return update_json(ledger_path(directory, consumer), stamp)


def carry_placed_at(prev: Iterable[float], carried: int, fresh: int, now: float,
                    ramp_s: float) -> List[float]:
    """``placed_at`` for a refreshed row: the newest ``carried`` stamps of the
    previous row (which unit finished is unknown; keeping the newest never
    under-projects), plus ``fresh`` units stamped ``now``. Stamps past the
    ramp contribute 0 and are dropped to keep the row bounded."""
    keep = sorted((float(t) for t in prev if _num(t) is not None), reverse=True)[:max(0, carried)]
    stamps = [now] * max(0, fresh) + keep
    return [t for t in stamps if ramp_s > 0 and now - t < ramp_s]


def kanban_rows(prev: Mapping, busy_by_host: Mapping[str, int], placed_by_host: Mapping[str, int],
                *, cpu_est: float, ramp_s: float, now: float) -> Dict[str, dict]:
    """The kanban writer's per-host rows for this tick."""
    raw_hosts = prev.get("hosts")
    old_hosts: Mapping = raw_hosts if isinstance(raw_hosts, dict) else {}
    rows: Dict[str, dict] = {}
    for host in sorted(set(busy_by_host) | set(placed_by_host)):
        busy = int(busy_by_host.get(host, 0) or 0)
        fresh = int(placed_by_host.get(host, 0) or 0)
        total = busy + fresh
        if total <= 0:
            continue
        raw_old = old_hosts.get(host)
        old: Mapping = raw_old if isinstance(raw_old, dict) else {}
        rows[host] = {"busy_units": total, "cpu_est": float(cpu_est), "ramp_s": float(ramp_s),
                      "placed_at": carry_placed_at(old.get("placed_at") or [], busy, fresh, now, ramp_s)}
    return rows
