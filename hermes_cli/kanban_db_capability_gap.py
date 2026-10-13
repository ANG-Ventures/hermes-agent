"""The ONE ``worker_capability_gap`` emitter (Fleet Host Lanes spec v0.2 §4.3, card t_b1100bf0).

A dispatcher-owned kanban worker that hits a missing host tool, a toolset its profile does not enable,
or a tool whose ``check_fn`` fails leaves one board event plus one line in the fleet ledger
``<fleet root>/state/fleet-host-gap/gaps.jsonl``. That is all it does: no pager, no notify, no
subprocess, no network (I5, I6). Every message to a human is produced by the hourly
``fleet-host gaps --escalate`` in hermes-home, which reads only the ledger.

Host identity and the tool -> column map are NOT redefined here: they live in hermes-home's
``scripts/fleet_host_onboard.py`` (``resolve_host``, ``TOOL_REQUIREMENTS``; spec I1/I7) and are loaded
from the fleet root by path. When that module cannot be loaded the event still lands with
``host=None`` (the view shows it under ``unknown host``).

Callers: ``tools/capability_gap_hooks.py`` only (I3, pinned by tests/hermes_cli/test_capability_gap.py).
"""
from __future__ import annotations

import fnmatch
import importlib.util
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

try:
    import fcntl
except ImportError:  # Windows: no flock; whole-line + rollback still hold for a single writer
    fcntl = None

_log = logging.getLogger(__name__)

EVENT_KIND = "worker_capability_gap"
# gap_class is THE key (Momus N2): host classes vs the profile class are never inferred from requirement_id.
HOST_CLASSES = frozenset({"rail_absent", "cmd_not_found", "check_fn_unavailable"})
PROFILE_CLASSES = frozenset({"profile_toolset"})
GAP_CLASSES = HOST_CLASSES | PROFILE_CLASSES
_MESSAGE_MAX = 400

_fho_lock = threading.Lock()
_fho_cache: dict[str, Any] = {}       # fleet root script path -> loaded module (successes only)
_fho_failed: dict[str, float] = {}    # path -> monotonic time of the last failed load
_FHO_RETRY_S = 60.0


def fleet_root() -> Path:
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def ledger_path() -> Path:
    return fleet_root() / "state" / "fleet-host-gap" / "gaps.jsonl"


def fleet_host_onboard():
    """hermes-home's ``scripts/fleet_host_onboard.py`` (the single encoding of HOSTS / TOOL_REQUIREMENTS),
    loaded once per fleet root; None when absent or when its import-time validator refuses the table. A failed
    load is retried after ``_FHO_RETRY_S`` so a transient error never blinds the process for its lifetime."""
    path = fleet_root() / "scripts" / "fleet_host_onboard.py"
    key = str(path)
    with _fho_lock:
        if key in _fho_cache:
            return _fho_cache[key]
        failed_at = _fho_failed.get(key)
        if failed_at is not None and time.monotonic() - failed_at < _FHO_RETRY_S:
            return None
        mod = None
        try:
            spec = importlib.util.spec_from_file_location("_hermes_fleet_host_onboard", path)
            if spec is not None and spec.loader is not None and path.is_file():
                mod = importlib.util.module_from_spec(spec)
                # @dataclass resolves annotations through sys.modules[cls.__module__]: register first.
                sys.modules[spec.name] = mod
                try:
                    spec.loader.exec_module(mod)
                finally:
                    sys.modules.pop(spec.name, None)
        except Exception:
            _log.warning("capability gap: cannot load %s; events carry host=None", path, exc_info=True)
            mod = None
        if mod is None:
            _fho_failed[key] = time.monotonic()
        else:
            _fho_failed.pop(key, None)
            _fho_cache[key] = mod
        return mod


def resolve_host(host_raw: Optional[str]) -> Optional[str]:
    mod = fleet_host_onboard()
    if mod is None:
        return None
    try:
        return mod.resolve_host(host_raw)
    except Exception:
        return None


def requirement_for(name: Optional[str]) -> Optional[str]:
    """The column id that provisions tool/command ``name`` (``TOOL_REQUIREMENTS``, glob keys such as
    ``browser_*`` honoured), or None."""
    mod = fleet_host_onboard()
    if mod is None or not name:
        return None
    reqs = getattr(mod, "TOOL_REQUIREMENTS", {}) or {}
    if name in reqs:
        return reqs[name]
    return next((cid for pat, cid in reqs.items() if fnmatch.fnmatchcase(name, pat)), None)


def validate(gap_class: str, requirement_id: Optional[str]) -> None:
    if gap_class not in GAP_CLASSES:
        raise ValueError(f"worker_capability_gap: unknown gap_class {gap_class!r}")
    if gap_class in PROFILE_CLASSES and requirement_id is not None:
        raise ValueError(f"worker_capability_gap: {gap_class} is a profile gap; requirement_id must be None, "
                         f"got {requirement_id!r}")


def _append_ledger(line: dict) -> None:
    """Append one whole line or nothing. Writers serialize on an exclusive flock of the ledger itself; a short
    ``os.write`` is continued, and any failure truncates back to the pre-append size, so a fragment never
    survives to be joined onto the next record."""
    path = ledger_path()
    data = (json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        start = os.fstat(fd).st_size
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError(f"short write to {path}: {n} of {len(view)} bytes")
                view = view[n:]
        except BaseException:
            try:
                os.ftruncate(fd, start)
            except OSError:
                _log.warning("capability gap: could not roll back a partial ledger append at %s", path,
                             exc_info=True)
            raise
    finally:
        os.close(fd)   # closing the fd releases the flock


def record_capability_gap(
    conn: sqlite3.Connection, task_id: str, *, run_id: Optional[int], host_raw: Optional[str],
    profile: Optional[str], gap_class: str, requirement_id: Optional[str], tool: str, message: str,
    placed: bool = False, advisory: bool = False, hook: str = "",
) -> Optional[dict]:
    """Write one event row (own short transaction) + one ledger line; return the payload.

    Raises ``ValueError`` ONLY for an invalid ``gap_class`` / a profile gap carrying a requirement_id
    (the hook drops the event). Every other failure is logged and swallowed: a DB error returns None
    and writes no ledger line (AC10: one line per row); a ledger failure keeps the row.
    """
    validate(gap_class, requirement_id)
    payload = {
        "host_raw": host_raw, "host": resolve_host(host_raw), "profile": profile, "gap_class": gap_class,
        "requirement_id": requirement_id, "tool": tool, "message": str(message or "")[:_MESSAGE_MAX],
        "placed": bool(placed), "advisory": bool(advisory), "hook": hook,
    }
    try:
        from hermes_cli import kanban_db as kb
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, EVENT_KIND, payload, run_id=run_id)
    except Exception:
        _log.warning("capability gap: event row for %s not written", task_id, exc_info=True)
        return None
    try:
        _append_ledger({**payload, "task_id": task_id, "run_id": run_id, "at": int(time.time())})
    except Exception:
        _log.warning("capability gap: ledger append failed (%s); event row kept", ledger_path(), exc_info=True)
    return payload
