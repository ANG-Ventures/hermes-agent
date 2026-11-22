"""A card notice with no live owning session goes to the agent inbox, not another session's chat
(t_10556779, Ace 2026-11-21 20:38, Discord msg 1573920509014450197).

When the card's home session is live in this gateway, the home/owner wake paths deliver to it
(gateway/kanban_alerts_home.py, gateway/kanban_owner_wake.py). When it is NOT live, or the card has
no home, the notifier used to post the line to the card's notify sub; the route sweep moves #alerts
subs to #apollo, so the line landed in a chat whose session does not own the card. Now that line is
one row in the fleet agent log (``<fleet root>/scripts/lib/agent_inbox.py`` write_agent_log, the
writer kanban_boomerang_wake.py uses) and nothing posts.

Unchanged: a sub that IS the home's own chat, a live home (any chat), an ``operator:`` home (a human
owner), an unreadable state.db (unknown, not absent), api_server subs, lines routed to the lifecycle
channel, and the wake. With no agent-log library, or a failed write, the line posts as before.

Kill switch: ``KANBAN_NOTIFY_NO_OWNER=post`` in the gateway env restores today's routing.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

KILL_SWITCH_ENV = "KANBAN_NOTIFY_NO_OWNER"
AGENT_LOG_PRODUCER = "kanban-notifier"


def enabled() -> bool:
    return os.environ.get(KILL_SWITCH_ENV, "").strip().lower() != "post"


def no_live_owner(store: Any, sub: dict, home_sid: Optional[str], home_res: Any,
                  read_row: Callable[[str], Optional[dict]]) -> Optional[str]:
    """The reason this sub's lines go to the agent inbox (``no-home`` / ``home-not-live``), else None.

    Blocking (may read state.db); worker thread only. Any doubt keeps today's post."""
    from gateway import kanban_home_route as _hr
    from gateway import kanban_owner_wake as _ow

    if not enabled() or str(sub.get("platform") or "").lower() == "api_server":
        return None
    sid = str(home_sid or "").strip()
    if sid.startswith("operator:") or _hr.home_lookup_failed(home_res):
        return None
    target = getattr(home_res, "target", None)
    if _hr.same_chat(target, sub.get("platform"), sub.get("chat_id"), sub.get("thread_id")):
        return None  # the sub is the owning session's own chat
    if target is None and ":" in sid and _key_names_sub_chat(sid, sub):
        return None  # a session-key home names its chat in the key itself
    if not sid:
        return "no-home"
    try:
        if _ow.resolve_owner(store, sid, read_row) is not None:
            return None
        # A raw id with no row in this gateway's state.db may be a secondary profile's session
        # (its rows live in that profile's state.db): unknown, not offline, so today's post stays.
        if ":" not in sid and read_row(sid) is None:
            return None
    except _hr.SessionLookupError as exc:
        logger.debug("kanban notice-inbox: home %s unreadable (%s); today's route", sid, exc)
        return None
    return "home-not-live"


def _key_names_sub_chat(key: str, sub: dict) -> bool:
    """True when a session-key home (``agent:<ns>:<platform>:<type>:<chat>...``) is the sub's chat.

    HomeCache reads state.db by raw id only, so a key home has no target once it is not live.
    A key with no thread part matches on platform + chat (doubt keeps today's post)."""
    from gateway import kanban_home_route as _hr
    from gateway.run import _parse_session_key

    parsed = _parse_session_key(key) or {}
    if not parsed.get("platform") or not parsed.get("chat_id"):
        return False
    if parsed.get("thread_id"):
        target = _hr.HomeTarget(str(parsed["platform"]).lower(), str(parsed["chat_id"]),
                                str(parsed.get("thread_id")))
        return _hr.same_chat(target, sub.get("platform"), sub.get("chat_id"), sub.get("thread_id"))
    return (str(parsed["platform"]).lower() == str(sub.get("platform") or "").strip().lower()
            and str(parsed["chat_id"]) == str(sub.get("chat_id") or "").strip())


def write_inbox(task_id: str, board: Optional[str], kind: str, text: str, why: str) -> bool:
    """One agent-log row for the notice. False when there is no library or the write fails."""
    try:
        from cron.fork_ext.alerts_audience_gate import _fleet_root, _load_lib
        from hermes_constants import get_hermes_home

        root = _fleet_root(Path(get_hermes_home()))
        lib = _load_lib(root) if root is not None else None
        if lib is None or not hasattr(lib, "write_agent_log"):
            return False
        lib.write_agent_log(root, AGENT_LOG_PRODUCER, "info", text, task_id=task_id,
                            board=board or "", kind=kind, why=why)
        return True
    except Exception as exc:
        logger.warning("kanban notice-inbox: agent-log row for %s not written: %s", task_id, exc)
        return False
