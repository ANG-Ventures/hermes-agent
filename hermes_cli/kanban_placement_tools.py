"""Placed kanban workers register only placement-routed tools (Browser Sandboxes B1 (b), t_a2817825).

A placed worker (a pool host, or ``studio-local`` under ``kanban.local_worker_uid``) runs its
terminal and file tools over ssh as the placement's user. The worker PROCESS stays the
dispatcher's uid (502 on the Studio, NOPASSWD sudo), so any tool that spawns a LOCAL process
without consulting the placement (Chromium/lightpanda launchers, the ``execute_code`` sandbox,
stdio MCP servers, delegation's ACP spawns, the checkpoint manager's git) runs as 502 and walks
around the uid boundary. Those tools are refused at REGISTRATION, so the model never sees them.

Two layers:

* ``ALLOWED_TOOLSETS``, the floor: a placed worker registers a tool only when its toolset is on
  this list (MCP toolsets pass here; their spawn gate is ``refuse_stdio_mcp_servers``).
* ``kanban.placement.local_only_tools_refused`` (root config, read by the dispatcher per spawn
  and handed to the worker in ``REFUSED_ENV``): tool or toolset names refused on top of the floor.
  Absent = ``DEFAULT_REFUSED``. Malformed (not a list of strings) = the floor alone, logged: it
  can never widen what a placed worker registers.

Every refusal is logged and lands on the run as one ``tool_refused_by_placement`` event per
toolset, naming the tools and the placement.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Mapping, Optional, Tuple

KNOB = "local_only_tools_refused"
REFUSED_ENV = "KANBAN_PLACEMENT_TOOLS_REFUSED"
EVENT = "tool_refused_by_placement"
MALFORMED = "!malformed"

# Toolsets whose tools run over the placement (terminal, file) or are pure API / in-process calls.
ALLOWED_TOOLSETS = frozenset({
    "terminal", "file", "kanban", "memory", "memory_write", "skills", "web", "vision", "todo",
    "clarify", "session_search", "context_engine", "homeassistant", "image_gen", "moa", "x_search",
})
# Named for the operator; everything off ALLOWED_TOOLSETS is refused whatever this says.
DEFAULT_REFUSED = ("browser", "browser-cdp", "browser-use", "code_execution", "computer_use",
                   "delegation", "mcp-stdio", "checkpoints")

_log = logging.getLogger(__name__)
_pending: Dict[Tuple[str, str], List[str]] = {}
_recorded: set = set()  # (toolset, reason, name): one event per refusal per process
_logged_malformed = {"done": False}


# -- dispatcher side ---------------------------------------------------------

def spawn_env_value(kanban_cfg: Any) -> str:
    """``REFUSED_ENV`` for a worker placed now: a JSON list, or ``MALFORMED``."""
    pl = kanban_cfg.get("placement") if isinstance(kanban_cfg, Mapping) else None
    if pl is not None and not isinstance(pl, Mapping):
        return MALFORMED
    raw = (pl or {}).get(KNOB, list(DEFAULT_REFUSED))
    if not isinstance(raw, (list, tuple)) or not all(isinstance(x, str) and x.strip() for x in raw):
        return MALFORMED
    return json.dumps(sorted({x.strip() for x in raw}))


# -- worker side -------------------------------------------------------------

def placed_host(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The placement this worker was spawned with (boot record first), else None."""
    from hermes_cli import kanban_worker_hosts as kwh
    if environ is None and kwh.BOOT_PLACED_HOST:
        return kwh.BOOT_PLACED_HOST
    env = os.environ if environ is None else environ
    raw = env.get(kwh.PLACEMENT_ENV)
    if not raw:
        return None
    # A placement env that does not parse is still a placement: refuse, never fall open.
    return kwh._placed_host(raw) or "unknown-placement"


def refused_extra(environ: Optional[Mapping[str, str]] = None) -> frozenset:
    env = os.environ if environ is None else environ
    raw = env.get(REFUSED_ENV)
    if raw is None:
        return frozenset(DEFAULT_REFUSED)
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        value = None
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        if not _logged_malformed["done"]:
            _logged_malformed["done"] = True
            _log.warning("kanban.placement.%s is malformed: placed worker registers the allow-list only", KNOB)
        return frozenset()
    return frozenset(value)


def refusal_reason(name: str, toolset: str, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Why a placed worker must not register ``name``, or None to register it."""
    if not toolset.startswith("mcp-") and toolset not in ALLOWED_TOOLSETS:
        return "not_on_allow_list"
    extra = refused_extra(environ)
    if name in extra or toolset in extra:
        return "refused_by_config"
    return None


def gate_registration(name: str, toolset: str) -> bool:
    """True when the registry must drop this registration (called from ``registry.register``)."""
    host = placed_host()
    if host is None:
        return False
    reason = refusal_reason(name, toolset)
    if reason is None:
        return False
    record(name, toolset, host, reason)
    return True


def gate_feature(name: str) -> bool:
    """True when a non-registry local spawner (``checkpoints``) must stay off in a placed worker."""
    host = placed_host()
    if host is None:
        return False
    record(name, name, host, "local_spawner")
    return True


def refuse_stdio_mcp_servers(servers: Dict[str, dict]) -> Dict[str, dict]:
    """Drop stdio MCP servers before any spawn in a placed worker; HTTP servers pass."""
    host = placed_host()
    if host is None or not servers:
        return servers
    kept = {}
    for name, cfg in servers.items():
        if isinstance(cfg, Mapping) and "url" not in cfg:
            record(f"mcp:{name}", "mcp-stdio", host, "local_spawner")
        else:
            kept[name] = cfg
    flush_events()
    return kept


def record(name: str, toolset: str, host: str, reason: str) -> None:
    if (toolset, reason, name) in _recorded:
        return
    _recorded.add((toolset, reason, name))
    _pending.setdefault((toolset, reason), []).append(name)
    _log.info("%s: %s (toolset %s) on placement %s: %s", EVENT, name, toolset, host, reason)


def flush_events() -> int:
    """Write pending refusals onto THIS run (one event per toolset). Returns events written."""
    if not _pending:
        return 0
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    try:
        run_id = int(os.environ.get("HERMES_KANBAN_RUN_ID") or "")
    except ValueError:
        run_id = None
    try:
        from agent.delegation_context import is_dispatcher_owned_worker_context
        owner = is_dispatcher_owned_worker_context()
    except Exception:
        owner = False
    if not task_id or not owner:
        _pending.clear()
        return 0
    host = placed_host() or ""
    batch = {k: list(v) for k, v in _pending.items()}
    try:
        from hermes_cli import kanban_db as kb
        conn = kb.connect()
        try:
            with kb.write_txn(conn):
                for (toolset, reason), names in sorted(batch.items()):
                    kb._append_event(conn, task_id, EVENT, {
                        "placement": host, "toolset": toolset, "tools": sorted(names), "reason": reason,
                    }, run_id=run_id)
        finally:
            conn.close()
    except Exception:
        # The refusal itself held; only its audit did not land. Keep the batch for the next flush.
        _log.warning("could not record %s on run %s; kept for the next flush", EVENT, run_id, exc_info=True)
        return 0
    for key, names in batch.items():  # drop only what committed; record() may have appended since
        left = [n for n in _pending.get(key, []) if n not in names]
        if left:
            _pending[key] = left
        else:
            _pending.pop(key, None)
    return len(batch)


def at_agent_init(checkpoints_enabled: bool) -> bool:
    """Agent init in a placed worker: checkpoints off, registration-time refusals onto the run.

    Keys on ``placed_host()`` (the boot record first), like the registry and MCP gates: the env
    can be clobbered after boot, and the checkpoint manager's git would then run as the
    dispatcher's uid. Returns whether checkpoints stay enabled.
    """
    if placed_host() is None:
        return checkpoints_enabled
    if checkpoints_enabled:
        checkpoints_enabled = not gate_feature("checkpoints")
    flush_events()
    return checkpoints_enabled
