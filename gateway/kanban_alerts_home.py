"""A card homed in a LIVE #alerts session wakes THAT session (t_5763e008, Ace 2026-11-15 09:19).

A session born in #alerts owns cards like any other session, but nothing may be posted to #alerts
for them. The notifier used to deliver such a card's handoff to its waker sub, the newest operator
chat (#apollo), whose session does not work the card (t_ca89a948: 11:10 handoff, woken 11:27 by
the review deadman instead). Now, when the card's home resolves to a live session in THIS gateway's
store whose chat is #alerts, the notifier's wake goes to that home session and no line is posted
(hermes-home review_deadman_core.recipient(): route ``home``, chat '', wake only).

Unchanged: a home with no live session here (today's sub route), a home in any other chat, a
contention-downgraded wake (today's line), boomerang-claimed events (their child), notify-only subs.

Kill switch: ``KANBAN_NOTIFY_ALERTS_HOME=reroute`` in the gateway env restores today's routing.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

KILL_SWITCH_ENV = "KANBAN_NOTIFY_ALERTS_HOME"


def enabled() -> bool:
    return os.environ.get(KILL_SWITCH_ENV, "").strip().lower() != "reroute"


def _alerts_channel() -> str:
    from gateway.kanban_watchers import _needs_input_channels

    return str(_needs_input_channels()[0])


def is_alerts_entry(entry: Any) -> bool:
    """The session's chat is the fleet #alerts channel itself (not a thread under it)."""
    origin = getattr(entry, "origin", None)
    plat = getattr(origin, "platform", None)
    plat = str(getattr(plat, "value", plat) or "").lower()
    chat = str(getattr(origin, "chat_id", "") or "").strip()
    thread = str(getattr(origin, "thread_id", "") or "").strip()
    return plat == "discord" and chat == _alerts_channel() and thread in ("", chat)


def live_alerts_home(store: Any, home_sid: Optional[str],
                     read_row: Callable[[str], Optional[dict]]) -> Optional[Any]:
    """The card home's live session-store entry when its chat is #alerts, else None.

    Blocking (may read state.db); worker thread only. An unreadable state.db is None: the
    event keeps today's route rather than being held."""
    if not enabled() or not home_sid:
        return None
    from gateway import kanban_home_route as _hr
    from gateway import kanban_owner_wake as _ow

    try:
        entry = _ow.resolve_owner(store, str(home_sid), read_row)
    except _hr.SessionLookupError as exc:
        logger.debug("kanban alerts-home: home %s unreadable (%s); today's route", home_sid, exc)
        return None
    if entry is None or not _ow.is_chat_origin(entry) or not is_alerts_entry(entry):
        return None
    return entry


def own_session_ids(entry: Any, home_sid: Optional[str]) -> set[str]:
    return {str(home_sid or ""), str(getattr(entry, "session_id", "") or "")} - {""}


async def wake_home(runner: Any, entry: Any, text: str, *, board: Optional[str], task_id: str,
                    title: str, kinds: Any, event_ts: Any, carried_ids: Any = ()) -> None:
    """Enqueue the notifier's wake turn into the #alerts home ``entry``. Raises when the turn was
    not taken, so the notifier rewinds its claim and retries like any wake-only delivery.

    ``carried_ids`` (red handback / ready children / block events) are marked carried for the
    home's own chat, so the owner-wake does not wake the same session again for them."""
    from gateway import kanban_owner_wake as _ow

    kinds = sorted(kinds)
    card = {"board": board, "task_id": task_id, "title": title,
            "items": [{"kind": k, "trigger": k, "event_ts": event_ts} for k in kinds]}
    if not await _ow.deliver_turn(runner, entry, text, [card]):
        raise RuntimeError(f"#alerts home session {getattr(entry, 'session_key', '?')} did not take the wake")
    origin = entry.origin
    try:
        active = runner._active_profile_name()  # noqa: SLF001
    except Exception:
        active = "default"
    _ow.mark_carried(runner, board or "", task_id, carried_ids, origin.platform, origin.chat_id,
                     getattr(origin, "thread_id", "") or "",
                     who=(getattr(origin, "user_id", ""), getattr(origin, "user_id_alt", "")),
                     profile=getattr(origin, "profile", None) or active or "default")
    logger.info("kanban notifier: woke #alerts home %s for %s events=%s",
                getattr(entry, "session_key", "?"), task_id, kinds)
