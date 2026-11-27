"""Drop kanban wakes whose card went done/archived before the wake ran (t_07ffc6cb).

A wake is built from a card snapshot, then waits: in the owner-wake coalescing
window, or in a busy session's pending queue for many minutes. On 2026-10-03
four stuck/gave_up wakes for t_49ca79b8 / t_c459e6e6 (events 21:11-21:22) were
consumed after both cards were archived at 21:30. So the card's status is read
again at SEND time and again at DEQUEUE time. A wake about a card that is now
``done``/``archived`` is dropped, unless the event itself is the completion or
archive (that is still news). A status notice (review_requested, blocked, gave_up,
block_loop_detected) is also dropped once the card no longer holds that status
(t_05badfca, see :func:`is_stale_notice`). Every drop logs one ``kanban_wake_stale_dropped``
line. Any read failure keeps the wake: a duplicate turn is the safe failure.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

FINAL_STATUSES = frozenset({"done", "archived"})
# Kinds that are themselves the transition to a final status: still news.
STILL_NEWS_KINDS = frozenset({"completed", "archived"})
# MessageEvent.metadata key: [{board, task_id, kinds, event_ts, text}, ...].
META_KEY = "kanban_wake_cards"
GONE = "gone"
OBLIGATION_META_KEY = "kanban_wake_obligations"
_OBLIGATION_HEADER = re.compile(
    r"\[obligations\] card (t_[0-9a-f]+) has (\d+) open decision\(s\); claim one to hold it:")
_OBLIGATION_ROW = re.compile(
    r"- (ob_[0-9a-f]+) \([^\n]*\): [\s\S]*?\n"
    r"  claim: hermes kanban --board (\S+) obligation claim \1")


def obligation_wake(text: str, *, now: Optional[float] = None) -> tuple[str, dict]:
    """Stamp the control wake before it can wait in an adapter queue.

    Parse only the reconciler's complete wire format; arbitrary text/merged human
    messages must never be discarded because they happen to mention an obligation.
    """
    header, sep, body = text.partition("\n")
    match = _OBLIGATION_HEADER.fullmatch(header)
    if not match or not sep:
        return text, {}
    rows = list(_OBLIGATION_ROW.finditer(body))
    if not rows or len(rows) != int(match[2]) or "\n".join(r[0] for r in rows) != body:
        return text, {}
    sent_at = time.time() if now is None else now
    header += f" [sent-at={sent_at:.3f}]"
    tagged = header + "\n" + body
    return tagged, {OBLIGATION_META_KEY: {
        "task_id": match[1], "sent_at": sent_at, "header": header,
        "text": tagged, "rows": [
            {"id": r[1], "board": r[2], "text": r[0]} for r in rows],
    }}


def filter_obligations(event: Any, *, now: Optional[float] = None) -> bool:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_work_obligations import OPEN_STATES, post_logs

    meta = (getattr(event, "metadata", None) or {}).get(OBLIGATION_META_KEY)
    if not meta or event.text != meta["text"]:
        return False
    if not meta["rows"]:
        return True
    live, removed = [], []
    for row in meta["rows"]:
        try:
            conn = kb.connect_readonly(board=row["board"])
            try:
                current = conn.execute("SELECT state FROM obligations WHERE id = ? AND task_id = ?",
                                       (row["id"], meta["task_id"])).fetchone()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("obligation wake freshness: cannot read %s (%s); keeping it", row["id"], exc)
            live.append(row)
            continue
        if current and current[0] in OPEN_STATES:
            live.append(row)
        else:
            removed.append(row)
    if not removed:
        return False
    delivered_at = time.time() if now is None else now
    receipt = (f"[obligations] stale wake {'suppressed' if not live else 'narrowed'} "
               f"card={meta['task_id']} rows={','.join(r['id'] for r in removed)} "
               f"sent-at={meta['sent_at']:.3f} delivered-at={delivered_at:.3f}")
    logger.info("%s", receipt)
    post_logs(receipt)
    meta["rows"] = live
    if not live:
        return True
    header = re.sub(r"has \d+ open decision", f"has {len(live)} open decision", meta["header"], count=1)
    event.text = header + "\n" + "\n".join(r["text"] for r in live)
    meta.update(header=header, text=event.text)
    return False


def closed_card_news(kind: Any, payload: Any) -> bool:
    """t_28b3f37c: main-train's owner notice (hermes-home main_train.Kanban._boomerang through
    card_mechanical_wake.emit) tells the session behind a merged PR that the PR turned main red. The
    card it names is usually closed already, so a done/archived status does not make it stale."""
    p = payload if isinstance(payload, dict) else {}
    return (kind == "mechanical_wake" and p.get("by") == "card-mechanical-wake"
            and p.get("source") == "main-train"
            and str(p.get("episode") or "").startswith("main-train-owner:"))


def is_stale(status: Optional[str], kind: str, payload: Any = None) -> bool:
    if status in FINAL_STATUSES and closed_card_news(kind, payload):
        return False
    return (status in FINAL_STATUSES or status == GONE) and kind not in STILL_NEWS_KINDS


def is_stale_notice(status: Optional[str], kind: str, payload: Any = None) -> bool:
    """:func:`is_stale`, and also a status notice whose status the card no longer holds.

    t_05badfca: on 11-18 house-voice t_305ecaca's review_requested wake (queued 16:24:25) ran at
    16:24:48, after the owner had requested changes (16:24:28) and the implementer's run had
    claimed the card. An unknown status (``None``, a read failure) keeps the wake."""
    if is_stale(status, kind, payload):
        return True
    held = NOTICE_STATUSES.get(kind)
    return bool(held) and bool(status) and status not in held


# The status each notice kind leaves the card in, measured from the board events (3 days, 11-18):
# blocked -> blocked, gave_up -> blocked, block_loop_detected -> triage, review_requested -> review.
NOTICE_STATUSES = {
    "review_requested": frozenset({"review"}),
    "blocked": frozenset({"blocked"}),
    "gave_up": frozenset({"blocked"}),
    "block_loop_detected": frozenset({"triage"}),
}


def current_status(conn: Any, task_id: str) -> str:
    """Status, ``GONE`` for a deleted row; a card RUNNING under a review claim (the claim's
    ``source_status`` is review) reads as ``review``: the review it was handed off for is underway."""
    row = conn.execute("SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        return GONE
    status = str(row[0] or "")
    if status == "running" and row[1] is not None:
        claim = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? AND kind = 'claimed' "
            "ORDER BY id DESC LIMIT 1", (task_id, row[1])).fetchone()
        try:
            payload = json.loads(claim[0]) if claim and claim[0] else {}
        except (TypeError, ValueError):
            payload = {}
        if isinstance(payload, dict) and payload.get("source_status") == "review":
            return "review"
    return status


def read_status(board: Optional[str], task_id: str) -> Optional[str]:
    """:func:`current_status` on ``board``. Raises if the board can't be read."""
    from hermes_cli import kanban_db as kb

    conn = kb.connect_readonly(board=board or None)
    try:
        return current_status(conn, task_id)
    finally:
        conn.close()


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
               text: str, closed_news: bool = False) -> dict:
    entry = {"board": board or "", "task_id": task_id, "kinds": sorted(set(kinds)),
             "event_ts": event_ts, "text": text}
    if closed_news:   # t_28b3f37c: the wake carries an owner notice, news on a done/archived card
        entry["closed_news"] = True
    return entry


def filter_event(event: Any, *, now: Optional[float] = None) -> bool:
    """Re-check a wake event's cards at dequeue. Returns True to DROP the event.

    Narrows ``event.text`` to the cards that are still live when only some went
    stale. Leaves the event alone when it carries no card metadata or when its
    text no longer equals the rendered cards (a human message merged into it).
    """
    if filter_obligations(event, now=now):
        return True
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
        news = card.get("closed_news") is True and status in FINAL_STATUSES
        if kinds and not news and all(is_stale_notice(status, k) for k in kinds):
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
    metadata = getattr(event, "metadata", None) or {}
    if not (metadata.get(META_KEY) or metadata.get(OBLIGATION_META_KEY)):
        return False
    return await asyncio.to_thread(filter_event, event)
