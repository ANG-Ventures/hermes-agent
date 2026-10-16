"""The four ``worker_capability_gap`` hook sites (Fleet Host Lanes spec v0.2 §4.3, card t_b1100bf0).

A  rails           ``kanban_worker_exit.block_on_rail_absent`` after ``block_task`` committed -> ``rail_absent``
B  toolset scope   the ``'<tool>' is not available in this session`` branches (``agent/tool_executor.py``
                   tool_call unwrap, ``model_tools`` bridge dispatch) -> ``profile_toolset`` when the profile's
                   toolsets do not select the tool, ``check_fn_unavailable`` when they do and its check_fn fails
C  terminal        ``<name>: command not found`` for a ``TOOL_REQUIREMENTS`` command -> ``cmd_not_found``
D  tool_describe   ``not_found`` names that are ``TOOL_REQUIREMENTS`` keys -> ``profile_toolset`` (advisory)

Every hook: only the dispatcher-owned worker (``owned_kanban_task()``) emits; one event per (task, run, tool),
except that an actionable occurrence supersedes an earlier advisory one (Hook D first, then a real block: the
real block is recorded too, because advisory rows are never escalated); a slot is held only while its write is
in flight and released when the write fails, so a transient DB error never loses the gap for the rest of the
run; never raises into the tool call; no notify (the hourly ``fleet-host gaps --escalate`` owns every message).
"""
from __future__ import annotations

import logging
import os
import socket
import threading
from typing import Iterable, Optional

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_emitted: dict = {}            # (task_id, run_id, tool) -> rank recorded (or in flight) this process
_ADVISORY, _ACTUAL = 1, 2
_refused_logged: set = set()   # (gap_class, tool) whose ValueError was logged once

_RAIL_TOOL = {"BROWSER_RAIL_ABSENT": "browser_*", "SCREEN_RAIL_ABSENT": "computer_use"}


def _owned_task() -> str:
    from agent.delegation_context import owned_kanban_task
    return owned_kanban_task()


def _run_id() -> Optional[int]:
    try:
        return int(os.environ.get("HERMES_KANBAN_RUN_ID") or "")
    except ValueError:
        return None


def _placed_host() -> Optional[str]:
    from tools.rail_preflight import _placed_host as placed
    return placed()


def _profile() -> Optional[str]:
    from hermes_cli.profiles import current_profile_name
    return current_profile_name()


def _emit(*, gap_class: str, tool: str, requirement_id: Optional[str], host_raw: Optional[str], message: str,
          hook: str, advisory: bool = False) -> bool:
    """Record one gap for the owning worker; False when not a worker, a duplicate, refused, or failed."""
    try:
        task = _owned_task()
        if not task:
            return False
        run_id = _run_id()
        key = (task, run_id, tool)
        rank = _ADVISORY if advisory else _ACTUAL
        with _lock:
            prior = _emitted.get(key, 0)
            if prior >= rank:
                return False
            _emitted[key] = rank   # in-flight reservation: a concurrent same-rank call drops
        ok = False
        try:
            ok = _record(task, run_id, gap_class=gap_class, tool=tool, requirement_id=requirement_id,
                         host_raw=host_raw, message=message, hook=hook, advisory=advisory)
        finally:
            if not ok:
                with _lock:
                    if _emitted.get(key) == rank:
                        if prior:
                            _emitted[key] = prior
                        else:
                            _emitted.pop(key, None)
        return ok
    except Exception:
        _log.warning("capability gap hook %s failed", hook, exc_info=True)
        return False


def _record(task: str, run_id: Optional[int], *, gap_class: str, tool: str, requirement_id: Optional[str],
            host_raw: Optional[str], message: str, hook: str, advisory: bool) -> bool:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_capability_gap as gap
    try:
        gap.validate(gap_class, requirement_id)
    except ValueError:
        if (gap_class, tool) not in _refused_logged:
            _refused_logged.add((gap_class, tool))
            _log.warning("capability gap dropped (%s, tool=%s, requirement_id=%r)", gap_class, tool,
                         requirement_id, exc_info=True)
        return False
    conn = kb.connect()
    try:
        return gap.record_capability_gap(
            conn, task, run_id=run_id, host_raw=host_raw, profile=_profile(), gap_class=gap_class,
            requirement_id=requirement_id, tool=tool, message=message, placed=bool(_placed_host()),
            advisory=advisory, hook=hook) is not None
    finally:
        conn.close()


def on_rail_absent(message: str) -> bool:
    """Hook A. Called only after the board decision for a rail ``*_ABSENT`` line committed.

    One parser for both spellings (t_ecfd5591 review 77f581ae3801): #1876's
    ``BROWSER_RAIL_ABSENT: no browser session on <host>.`` and the placement gates'
    ``BROWSER_RAIL_ABSENT cap=browser-session host=<host> ...`` (``kanban_placement_failfast``)."""
    from hermes_cli.kanban_placement_failfast import parse_absent
    parsed = parse_absent(message)
    tool = _RAIL_TOOL.get(parsed[0]) if parsed else None
    if tool is None:
        return False
    from hermes_cli.kanban_db_capability_gap import requirement_for
    return _emit(gap_class="rail_absent", tool=tool, requirement_id=requirement_for(tool),
                 host_raw=parsed[2], message=message, hook="rail")


def _classify_scope(tool: str, enabled_toolsets, disabled_toolsets) -> Optional[str]:
    """``profile_toolset`` when the session's toolsets do not select ``tool``; ``check_fn_unavailable`` when
    they do and its registry check_fn fails; None otherwise (selected, available, just not reachable here)."""
    import model_tools
    from tools.registry import _check_fn_cached, registry
    selected = model_tools._select_tool_names(
        list(enabled_toolsets) if enabled_toolsets is not None else None,
        list(disabled_toolsets) if disabled_toolsets else None, True)
    if tool not in selected:
        return "profile_toolset"
    entry = registry.get_entry(tool)
    if entry is not None and entry.check_fn is not None and not _check_fn_cached(entry.check_fn):
        return "check_fn_unavailable"
    return None


def on_scope_block(tool: str, message: str, *, enabled_toolsets=None, disabled_toolsets=None) -> bool:
    """Hook B: the ``not available in this session`` branch. Classifies BEFORE emitting (Momus N2)."""
    try:
        if not _owned_task():
            return False
        gap_class = _classify_scope(tool, enabled_toolsets, disabled_toolsets)
    except Exception:
        _log.warning("capability gap: scope classification failed for %s", tool, exc_info=True)
        return False
    if gap_class is None:
        return False
    from hermes_cli.kanban_db_capability_gap import requirement_for
    requirement_id = None if gap_class == "profile_toolset" else requirement_for(tool)
    return _emit(gap_class=gap_class, tool=tool, requirement_id=requirement_id,
                 host_raw=socket.gethostname(), message=message, hook="scope")


def on_command_not_found(missing: Optional[str]) -> bool:
    """Hook C: a terminal command failed with ``<missing>: command not found``. Only the command name is
    persisted: the terminal buffer can carry secrets and must never reach the board or the ledger."""
    if not missing:
        return False
    from hermes_cli.kanban_db_capability_gap import requirement_for
    requirement_id = requirement_for(missing)
    if requirement_id is None:
        return False
    return _emit(gap_class="cmd_not_found", tool=missing, requirement_id=requirement_id,
                 host_raw=_placed_host() or socket.gethostname(), message=f"{missing}: command not found",
                 hook="terminal")


def on_describe_not_found(names: Iterable[str]) -> int:
    """Hook D: ``tool_describe`` could not describe these names. Advisory profile gaps, never escalated."""
    from hermes_cli.kanban_db_capability_gap import requirement_for
    n = 0
    for name in names or ():
        if requirement_for(name) is None:
            continue
        n += _emit(gap_class="profile_toolset", tool=name, requirement_id=None, host_raw=socket.gethostname(),
                   message=f"tool_describe: {name} not_found", hook="describe", advisory=True)
    return n
