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
import threading
import time
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger(__name__)

EVENT_KIND = "worker_capability_gap"
# gap_class is THE key (Momus N2): host classes vs the profile class are never inferred from requirement_id.
HOST_CLASSES = frozenset({"rail_absent", "cmd_not_found", "check_fn_unavailable"})
PROFILE_CLASSES = frozenset({"profile_toolset"})
GAP_CLASSES = HOST_CLASSES | PROFILE_CLASSES
_MESSAGE_MAX = 400

_fho_lock = threading.Lock()
_fho_cache: dict[str, Any] = {}


def fleet_root() -> Path:
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def ledger_path() -> Path:
    return fleet_root() / "state" / "fleet-host-gap" / "gaps.jsonl"


def fleet_host_onboard():
    """hermes-home's ``scripts/fleet_host_onboard.py`` (the single encoding of HOSTS / TOOL_REQUIREMENTS),
    loaded once per fleet root; None when absent or when its import-time validator refuses the table."""
    path = fleet_root() / "scripts" / "fleet_host_onboard.py"
    key = str(path)
    with _fho_lock:
        if key in _fho_cache:
            return _fho_cache[key]
        mod = None
        try:
            spec = importlib.util.spec_from_file_location("_hermes_fleet_host_onboard", path)
            if spec is not None and spec.loader is not None and path.is_file():
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
        except Exception:
            _log.warning("capability gap: cannot load %s; events carry host=None", path, exc_info=True)
            mod = None
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
    path = ledger_path()
    data = (json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


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
