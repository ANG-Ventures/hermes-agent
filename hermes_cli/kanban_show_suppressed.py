"""``kanban show``: the wakes and lines the notify filter withheld (t_4f6afb67, S5).

``kanban.notify_suppress`` turns an event that is no news to its receiver (the home
session's own close or send-back, an inherited or already-seen red, a closer's
auto-route) into a ``notify_suppressed`` task event instead of a chat turn. The
generic ``Events`` list shows only the last 20 of a busy card, so these receipts
get their own section: every one, oldest first, with the event it stood for.
"""
from __future__ import annotations

from typing import Any, Iterable

EVENT_KIND = "notify_suppressed"  # gateway.kanban_notify_suppress.EVENT_KIND


def suppressed_rows(events: Iterable[Any]) -> list[dict]:
    """``[{"at", "event_id", "kind", "reason", "path", "actor"}]`` for every receipt in ``events``."""
    out = []
    for e in events or ():
        if getattr(e, "kind", None) != EVENT_KIND:
            continue
        p = getattr(e, "payload", None)
        p = p if isinstance(p, dict) else {}
        out.append({"at": getattr(e, "created_at", None), "event_id": p.get("event_id"),
                    "kind": p.get("kind"), "reason": p.get("reason"), "path": p.get("path"),
                    "actor": p.get("actor") or p.get("actor_session_id")})
    return out


def format_rows(rows: list[dict], fmt_ts) -> list[str]:
    return [f"  [{fmt_ts(r['at'])}] {r['kind'] or '?'} #{r['event_id']} withheld: "
            f"{r['reason'] or '?'} ({r['path'] or '?'}{', by ' + str(r['actor']) if r['actor'] else ''})"
            for r in rows]
