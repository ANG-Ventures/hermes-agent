"""Oracle extracted with git show origin/main:hermes_cli/kanban_worker_hosts.py.
Source: 583676b772; frozen carry_size_ok, no candidate implementation."""
import os
from typing import Optional
from hermes_cli.kanban_worker_hosts import (CARRY_MAX_BYTES, CARRY_MAX_ENTRIES, CARRY_SCAN_MAX_SECONDS, CARRY_SCAN_TTL_SECONDS, CarryScanBudget, carry_path_safe, _raise, _log)
_carry_scan_cache = {}
_carry_waiting = {}

def carry_size_ok(path: Optional[str], *, max_bytes: int = CARRY_MAX_BYTES,
                  max_entries: int = CARRY_MAX_ENTRIES, now: Optional[float] = None,
                  budget: Optional[CarryScanBudget] = None,
                  max_seconds: float = CARRY_SCAN_MAX_SECONDS) -> bool:
    """True when the LOCAL workspace is small enough to carry (bytes and entries, symlinks not followed) and its
    path is :func:`carry_path_safe`. Any scan error is False (the card keeps the old rule). Cached per path and
    limits for ``CARRY_SCAN_TTL_SECONDS`` from the END of the walk: the dispatcher asks every tick.

    The walk uses a cooperative deadline: once a check observes ``max_seconds`` elapsed, the refusal is cached.
    Tick-budget exhaustion instead queues the path FIFO for a later tick without caching a refusal.
    Checks run between directories and every 256 entries. Canonicalization or a blocking scandir/lstat can
    overrun the deadline and still delay routing; this is not a hard filesystem timeout. A refused carry
    retains this card's old local_files rule rather than authorizing a transfer with an incomplete census."""
    import time as _time
    if not path or not carry_path_safe(path):
        return False
    try:
        from hermes_cli.kanban_db_workspace import canon
        root = canon(path)
    except (OSError, RuntimeError, ValueError):
        return False
    clock = budget.clock if budget is not None else _time.monotonic
    key = (root, max_bytes, max_entries)
    t = clock() if now is None else now
    hit = _carry_scan_cache.get(key)
    if hit is not None and t - hit[0] < CARRY_SCAN_TTL_SECONDS:
        return hit[1]
    if budget is not None and (budget.left() <= 0 or not budget.may_walk(key)):
        budget.defer(key)
        return False
    start = clock()
    tick_bound = budget is not None and budget.left() < max_seconds
    stop = start + (budget.left() if tick_bound else max_seconds)
    total = entries = 0
    ok = True
    timed_out = False
    try:
        for dirpath, dirnames, filenames in os.walk(root, onerror=_raise):
            for name in (*dirnames, *filenames):
                entries += 1
                total += os.lstat(os.path.join(dirpath, name)).st_size
                if entries > max_entries or total > max_bytes:
                    ok = False
                    break
                if entries % 256 == 0 and clock() >= stop:
                    ok, timed_out = False, True
                    break
            if ok and clock() >= stop:
                ok, timed_out = False, True
            if not ok:
                break
    except OSError:
        ok = False
    end = clock()
    if budget is not None:
        budget.spent_s += max(0.0, end - start)
    if timed_out and tick_bound:
        budget.defer(key)
        return False
    _carry_waiting.pop(key, None)
    if timed_out:
        _log.info("carry census of %s refused: walk passed %.1fs at %d entries", root, max_seconds, entries)
    _carry_scan_cache[key] = (end if now is None else now, ok)
    return ok
