"""The per-tick gate series ``<root>/var/gate-ticks.jsonl`` (Fleet Work
Allocation spec v0.3 §3.6, I-8, AC-12; card t_90887924).

ONE line per GATEWAY tick, appended by the tick loop in
``gateway.kanban_watchers`` after the per-board ``dispatch_once`` loop. Never
from ``GateTickBuilder.build()``: ``build()`` is shared with the standalone
daemon and the one-shot CLI (``standalone_builder``), so an append there would
make every ``kanban dispatch --dry-run`` write a line.

Nothing at runtime reads this file. Its readers are offline queries (the I-0
query: ``portable_waiting > 0 AND allowance_unused > 0`` must never hold), the
Phase 0a tick-cost harness and the Phase 5 views. A missing or corrupt series
changes no decision, so every write here is best-effort.

Rotation: the writer renames the live file once per UTC day
(``gate-ticks.<YYYY-MM-DD>.jsonl``, the UTC date of its last write) and keeps
``KEEP_DAYS`` rotated files.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping, Optional

SERIES_FILE = "gate-ticks.jsonl"
KEEP_DAYS = 14

# Every key a line carries (spec §3.6, v0.3). ``pcore_busy`` is optional:
# written only when the gate samples it (Phase S2).
FIELDS = (
    "ts", "path", "band", "spill_reason", "load1", "cpu_busy", "running",
    "running_local", "running_remote_by_host", "running_last_resort",
    "portable_waiting", "allowance", "allowance_unused", "pool_budget",
    "pool_free_by_host", "tick_ms_cpu", "tick_ms_wall", "boards",
    "rows_routed", "queries",
    # Momus pass 3 on spec v0.3 (t_667f1f6a). R1: the I-0 gate key is
    # per board (``portable_waiting_with_room``); host residual left while a
    # portable row waited is reported apart (``stranded_allowance``). R2:
    # every line says whether a pool plan ran (``pool_planned`` /
    # ``pool_reason``) so an unplanned tick is counted, never silently passed.
    # ``placement_default`` is the flag the tick ran under.
    "portable_waiting_with_room", "stranded_allowance", "pool_planned",
    "pool_reason", "placement_default",
)


def series_path(root: Path) -> Path:
    return Path(root) / "var" / SERIES_FILE


def _utc_day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def rotate(path: Path, now: float, keep: int = KEEP_DAYS) -> Optional[Path]:
    """Rename ``path`` to ``gate-ticks.<day>.jsonl`` when its last write was on
    an earlier UTC day than ``now``; prune rotated files past ``keep``.
    Returns the rotated path, or None when nothing was renamed."""
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    day = _utc_day(mtime)
    if day >= _utc_day(now):
        return None
    stem = path.name[: -len(".jsonl")]
    dest = path.with_name(f"{stem}.{day}.jsonl")
    if dest.exists():
        # A clock step back across midnight can give the live file a day that
        # already has an archive: merge into it, never overwrite it.
        _merge_into(dest, path)
    else:
        os.replace(path, dest)
    rotated = sorted(p for p in path.parent.glob(f"{stem}.????-??-??.jsonl"))
    for old in rotated[: max(0, len(rotated) - keep)]:
        old.unlink(missing_ok=True)
    return dest


def _merge_into(dest: Path, live: Path) -> None:
    """Merge ``live`` onto the end of ``dest`` and remove ``live``, crash-safe.

    The merged archive is written to a temp file and swapped in with one
    ``os.replace``, so ``dest`` is always either the old archive or the whole
    merge. A crash after that swap but before ``live`` is unlinked leaves
    ``dest`` already ending in ``live``'s bytes; the retry sees that suffix and
    only unlinks, so no tick record is ever counted twice (the I-0 query sums
    this series)."""
    old = dest.read_bytes()
    data = live.read_bytes()
    if data and old.endswith(data):
        live.unlink()
        return
    if old and not old.endswith(b"\n"):
        old += b"\n"
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    try:
        tmp.unlink(missing_ok=True)
        _append_all(tmp, old + data)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    live.unlink()


def append_record(path: Path, record: Mapping[str, Any], now: Optional[float] = None) -> None:
    """Append one JSON line (rotating first). Single ``write`` of a line under
    ``O_APPEND``: one gateway writes, so lines never interleave."""
    now = time.time() if now is None else now
    path.parent.mkdir(parents=True, exist_ok=True)
    rotate(path, now)
    line = json.dumps(dict(record), separators=(",", ":"), sort_keys=False) + "\n"
    _append_all(path, line.encode("utf-8"))


def _append_all(path: Path, data: bytes) -> None:
    """Append ``data`` whole or not at all: a short or failed write is
    truncated back to the pre-append size, so the file keeps its JSONL
    boundaries and a retried rotation never duplicates records."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        start = os.fstat(fd).st_size
        done = 0
        try:
            while done < len(data):
                n = os.write(fd, data[done:])
                if n <= 0:
                    raise OSError("short write")
                done += n
        except OSError:
            os.ftruncate(fd, start)
            raise
    finally:
        os.close(fd)


def tick_record(*, ts: float, gate, tick, builder, allowance: Optional[int],
                pool_budget: Optional[int],
                spawn_paused: Optional[str], local_spawned: int, results,
                tick_ms_cpu: float, tick_ms_wall: float, boards: int,
                queries: Optional[int]) -> dict:
    """The §3.6 line for one gateway tick.

    ``allowance_unused`` = the local allowance granted minus the local spawns
    every board made, i.e. AFTER the per-board split and the ``_spare``
    redistribution: a board-local surplus another board consumed is not
    unused host allowance. 0 while spawning is paused, None when the gate is
    disabled (no allowance exists).
    ``portable_waiting`` = portable rows still in ``placement_waits`` at the
    end of the tick, summed over boards.
    ``pool_budget`` is the plan's free slots read right after ``build()``,
    before any board took one (``take()`` does not count every placement in
    ``taken``), None without a plan.
    ``running_last_resort`` is None until Phase 2 records a ``last-resort``
    spawn reason (there is no such spawn before the flip).
    ``portable_waiting_with_room`` = portable rows the pool refused that then
    waited while their OWN board's local allowance had room (the I-0 gate
    key, Momus pass 3 R1: must be 0). ``stranded_allowance`` = host
    ``allowance_unused`` left while other portable rows waited without room on
    their board (quota stranded by the forward-only ``_spare`` split): a
    defect to report, not the I-0 violation.
    """
    running_local = getattr(builder, "running_local", None)
    remote = dict(getattr(builder, "_remote_by_host", {}) or {})
    plan = getattr(tick, "remote_plan", None)
    free = {n: int(v) for n, v in plan.slots.items()} if plan is not None else {}
    if allowance is None:
        unused: Optional[int] = None
    elif spawn_paused:
        unused = 0
    else:
        unused = max(0, int(allowance) - int(local_spawned))
    res = [r for _slug, r in (results or []) if r is not None]
    waiting = sum(int(getattr(r, "portable_waiting", 0) or 0) for r in res)
    with_room = sum(int(getattr(r, "portable_waiting_with_room", 0) or 0) for r in res)
    pool = getattr(gate, "pool", None)
    pool = pool if isinstance(pool, dict) else {}
    rec = {
        "ts": round(ts, 3),
        "path": "gateway",
        "band": getattr(tick, "band", None),
        "spill_reason": getattr(tick, "spill_reason", None),
        "load1": getattr(gate, "load1", None),
        "cpu_busy": getattr(gate, "cpu_busy", None),
        "running": None if running_local is None else running_local + sum(remote.values()),
        "running_local": running_local,
        "running_remote_by_host": remote,
        "running_last_resort": None,
        "portable_waiting": waiting,
        "allowance": allowance,
        "allowance_unused": unused,
        "pool_budget": pool_budget,
        "pool_free_by_host": free,
        "tick_ms_cpu": round(tick_ms_cpu, 3),
        "tick_ms_wall": round(tick_ms_wall, 3),
        "boards": boards,
        "rows_routed": sum(int(getattr(r, "rows_routed", 0) or 0) for r in res),
        "queries": queries,
        "portable_waiting_with_room": with_room,
        "stranded_allowance": (unused or 0) if waiting > with_room else 0,
        "pool_planned": plan is not None,
        "pool_reason": None if plan is not None else pool.get("reason"),
        "placement_default": "remote" if getattr(plan, "default_remote", False) else "local",
    }
    pcore = getattr(gate, "pcore_busy", None)
    if pcore is not None:
        rec["pcore_busy"] = pcore
    return rec
