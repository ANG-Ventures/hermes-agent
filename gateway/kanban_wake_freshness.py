"""Drop kanban wakes whose card went done/archived before the wake ran (t_07ffc6cb).

A wake is built from a card snapshot, then waits: in the owner-wake coalescing
window, or in a busy session's pending queue for many minutes. On 2026-10-03
four stuck/gave_up wakes for t_49ca79b8 / t_c459e6e6 (events 21:11-21:22) were
consumed after both cards were archived at 21:30. So the card's status is read
again at SEND time and again at DEQUEUE time. A wake about a card that is now
``done``/``archived`` is dropped, unless the event itself is the completion or
archive (that is still news). Every drop logs one ``kanban_wake_stale_dropped``
line. Any read failure keeps the wake: a duplicate turn is the safe failure.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

FINAL_STATUSES = frozenset({"done", "archived"})
# Kinds that are themselves the transition to a final status: still news.
STILL_NEWS_KINDS = frozenset({"completed", "archived"})
# MessageEvent.metadata key: [{board, task_id, kinds, event_ts, text}, ...].
META_KEY = "kanban_wake_cards"
GONE = "gone"


def is_stale(status: Optional[str], kind: str) -> bool:
    return (status in FINAL_STATUSES or status == GONE) and kind not in STILL_NEWS_KINDS


def read_status(board: Optional[str], task_id: str) -> Optional[str]:
    """Current status, ``GONE`` for a deleted row. Raises if the board can't be read."""
    from hermes_cli import kanban_db as kb

    conn = kb.connect_readonly(board=board or None)
    try:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()
    return str(row[0] or "") if row else GONE


def log_dropped(task_id: str, kind: str, event_ts: Any, stage: str, status: Optional[str],
                now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    try:
        age = int(now - float(event_ts))
    except (TypeError, ValueError):
        age = -1
    logger.info(
        "kanban_wake_stale_dropped task_id=%s kind=%s event_ts=%s age_s=%d stage=%s status=%s",
        task_id, kind, event_ts, age, stage, status,
    )


def card_entry(board: Optional[str], task_id: str, kinds: Iterable[str], event_ts: Any,
               text: str) -> dict:
    return {"board": board or "", "task_id": task_id, "kinds": sorted(set(kinds)),
            "event_ts": event_ts, "text": text}


def filter_event(event: Any, *, now: Optional[float] = None) -> bool:
    """Re-check a wake event's cards at dequeue. Returns True to DROP the event.

    Narrows ``event.text`` to the cards that are still live when only some went
    stale. Leaves the event alone when it carries no card metadata or when its
    text no longer equals the rendered cards (a human message merged into it).
    """
    cards = (getattr(event, "metadata", None) or {}).get(META_KEY)
    if not cards or not isinstance(cards, list):
        return False
    if (getattr(event, "text", None) or "") != "\n\n".join(str(c.get("text") or "") for c in cards):
        return False
    live = []
    for card in cards:
        try:
            status = read_status(card.get("board"), str(card.get("task_id") or ""))
        except Exception as exc:
            logger.debug("kanban wake freshness: cannot read %s (%s); keeping it",
                         card.get("task_id"), exc)
            live.append(card)
            continue
        kinds = list(card.get("kinds") or [])
        if kinds and all(is_stale(status, k) for k in kinds):
            for k in kinds:
                log_dropped(str(card.get("task_id")), k, card.get("event_ts"), "dequeue",
                            status, now)
            continue
        live.append(card)
    if len(live) == len(cards):
        return False
    if not live:
        return True
    event.text = "\n\n".join(str(c.get("text") or "") for c in live)
    event.metadata[META_KEY] = live
    return False


async def drop_stale(event: Any) -> bool:
    """Async wrapper for :func:`filter_event` (the status reads run off the loop)."""
    if not (getattr(event, "metadata", None) or {}).get(META_KEY):
        return False
    return await asyncio.to_thread(filter_event, event)
