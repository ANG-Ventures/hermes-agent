"""Deferred (never dropped) kanban wakes for human-owned cards (t_b7b31563).

Ace 2026-10-12 06:25, amending t_74bf5296: "a downgraded wake must be
DEFERRED, never dropped". Incident 05:38 PT the same day: the notifier logged
``wake for t_ac5f359a on discord/… sent as notify (host load 184)`` for a
``review_requested`` handoff to ``human:apollo``. The passive line posted, no
agent turn ran, and the chain sat 25 minutes until Ace spoke.

Mechanism: when the contention gate downgrades a wake whose event kind is in
:data:`DEFER_KINDS` and whose reviewer/owner is a human session
(``human:<name>`` assignee or reviewer), the notifier writes a PENDING row
here instead of forgetting the wake. Every notifier poll :func:`tick`
re-evaluates the rows and fires the wake when the gate clears (the same
``WakeGate.downgrade_reason`` read, ``human_owner=True``) OR when the row is
older than ``kanban.wake_defer_ceiling_s`` (default 1200 s), whichever comes
first. The fired wake carries the ORIGINAL synthetic handoff text (#70752
contract) plus one line ``(deferred Nm under <reason>)``.

Dedup: a row whose card is now done/archived, or whose owner acted on the card
after the deferred cursor (an event or comment by the owner's session /
operator label), is dropped with a log line: the wake already happened in
effect. A delivery failure keeps the row for the next poll.

State: ``<HERMES_HOME>/gateway/kanban_pending_wakes.json`` (the same durable
gateway state dir as the owner-wake ledger; fsync + rename), so a restart
between the downgrade and the fire still fires.

Log lines (grep ``kanban wake deferred``):

* ``kanban wake deferred for <task> on <platform>/<chat>: <reason> (cursor N); fires when the gate clears or after <ceiling>s``
* ``kanban wake deferred-fire for <task> on <platform>/<chat> after <N>m: gate cleared (was: <reason>)``
* ``kanban wake deferred-fire for <task> on <platform>/<chat> after <N>m: ceiling <S>s reached (still: <reason>)``
* ``kanban wake deferred dropped for <task> on <platform>/<chat>: <why>``
* ``kanban wake deferred-fire failed for <task> …; kept for retry``
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from gateway import kanban_notify_suppress as _ns
from gateway import kanban_wake_freshness as _fresh

logger = logging.getLogger(__name__)

STATE_FILENAME = "kanban_pending_wakes.json"
DEFAULT_CEILING_S = 1200
# ``triage`` = block_loop_detected (the notifier's "routed to TRIAGE" line);
# ``needs_input`` arrives as a ``blocked`` event with payload kind needs_input.
DEFER_KINDS = frozenset({"review_requested", "blocked", "block_loop_detected"})
DEFER_TEXT_KINDS = DEFER_KINDS


def state_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "gateway" / STATE_FILENAME


def ceiling_seconds() -> int:
    """``kanban.wake_defer_ceiling_s`` (default 1200); a bad value keeps the default."""
    try:
        from hermes_cli.config import load_config_readonly

        kcfg = (load_config_readonly() or {}).get("kanban") or {}
        raw = kcfg.get("wake_defer_ceiling_s", DEFAULT_CEILING_S) if isinstance(kcfg, dict) else DEFAULT_CEILING_S
        value = int(raw)
    except Exception:
        return DEFAULT_CEILING_S
    return value if value >= 1 else DEFAULT_CEILING_S


def _is_human(value: Any) -> bool:
    from hermes_cli.kanban_db import is_human_reviewer

    return is_human_reviewer(value)


def human_owner(task: Any, events: Iterable[Any]) -> Optional[str]:
    """The human owner label (``human:apollo``) when the card's assignee or any
    claimed event's reviewer is a human session, else None."""
    assignee = getattr(task, "assignee", None) if task is not None else None
    if _is_human(assignee):
        return str(assignee).strip()
    for ev in events:
        payload = getattr(ev, "payload", None) or {}
        reviewer = payload.get("reviewer") if isinstance(payload, dict) else None
        if _is_human(reviewer):
            return str(reviewer).strip()
    return None


def deferrable(kinds: Iterable[str]) -> bool:
    return any(k in DEFER_KINDS for k in kinds)


def row_key(board: Optional[str], sub: dict) -> str:
    return "|".join((
        str(board or ""), str(sub.get("task_id") or ""), str(sub.get("platform") or ""),
        str(sub.get("chat_id") or ""), str(sub.get("session_id") or ""),
    ))


def deferred_line(first_seen: float, reason: str, now: Optional[float] = None) -> str:
    now = time.time() if now is None else now
    minutes = max(0, int((now - float(first_seen)) // 60))
    return f"(deferred {minutes}m under {reason})"


class PendingWakes:
    """Pending-wake rows persisted as JSON (fsync + rename), keyed by :func:`row_key`."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return
        rows = raw.get("pending") if isinstance(raw, dict) else None
        if isinstance(rows, dict):
            self.rows = {str(k): v for k, v in rows.items() if isinstance(v, dict)}

    def write(self) -> None:
        """Blocking (fsync + rename): call via ``asyncio.to_thread`` from the loop."""
        data = {"pending": self.rows}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".pending-wakes-", dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning("kanban wake deferred: cannot persist state %s: %s", self.path, exc)

    def add(self, *, board: Optional[str], sub: dict, sub_profile: Optional[str], cursor: int,
            kinds: Iterable[str], text: str, reason: str, owner: str, home_sid: Optional[str],
            session_key: str, event_ts: Any, comment_cursor: int = 0,
            now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        key = row_key(board, sub)
        existing = self.rows.get(key)
        row = {
            "board": board or "",
            "task_id": sub["task_id"],
            "platform": sub["platform"],
            "chat_id": sub["chat_id"],
            "thread_id": sub.get("thread_id") or "",
            "sub": {k: sub.get(k) for k in (
                "task_id", "platform", "chat_id", "thread_id", "chat_type", "user_id",
                "user_id_alt", "scope_id", "notifier_profile", "delivery_metadata", "delivery_mode",
            )},
            "sub_profile": sub_profile or "",
            "cursor": int(cursor),
            # Comments by the owner after this id count as "acted" (an id
            # cursor: a same-second comment is not lost to a timestamp compare).
            "comment_cursor": int(comment_cursor or 0),
            "kinds": sorted(set(kinds)),
            "text": text,
            "reason": reason,
            "owner": owner,
            "home_sid": home_sid or "",
            "session_key": session_key or "",
            "event_ts": event_ts,
            # A re-downgrade of a later event on the same sub keeps the first
            # clock: the owner has been waiting since then.
            "first_seen": float(existing["first_seen"]) if existing else now,
        }
        self.rows[key] = row
        return row


def _owner_labels(owner: str, home_sid: Optional[str]) -> tuple[str, set[str]]:
    return _ns._operator_label(owner), {s for s in (home_sid,) if s}  # noqa: SLF001


def owner_acted(conn: sqlite3.Connection, row: dict) -> Optional[str]:
    """Why the owner has already acted on the card since the deferred cursor, else None.

    Evidence: a ``task_events`` row after the cursor whose actor is the card's
    home session or the owner's operator label, or a ``task_comments`` row by
    that operator newer than ``first_seen``. A table or column the board does
    not carry is not evidence.
    """
    owner = str(row.get("owner") or "")
    label, sids = _owner_labels(owner, row.get("home_sid"))
    task_id = row["task_id"]
    try:
        for r in conn.execute(
            "SELECT id, kind, actor_profile, actor_session_id FROM task_events "
            "WHERE task_id = ? AND id > ? ORDER BY id",
            (task_id, int(row.get("cursor") or 0)),
        ):
            sid = str(r["actor_session_id"] or "")
            if sid and sid in sids:
                return f"owner session wrote {r['kind']} (event {r['id']})"
            if label and _ns.same_operator(r["actor_profile"], label):
                return f"owner {owner} wrote {r['kind']} (event {r['id']})"
    except sqlite3.Error as exc:
        logger.debug("kanban wake deferred: event read for %s failed: %s", task_id, exc)
    try:
        for r in conn.execute(
            "SELECT id, author FROM task_comments WHERE task_id = ? AND id > ? ORDER BY id",
            (task_id, int(row.get("comment_cursor") or 0)),
        ):
            if label and _ns.same_operator(r["author"], label):
                return f"owner {owner} commented (comment {r['id']})"
    except sqlite3.Error as exc:
        logger.debug("kanban wake deferred: comment read for %s failed: %s", task_id, exc)
    return None


def evaluate(conn: sqlite3.Connection, row: dict) -> Optional[str]:
    """Sync (worker thread): the drop reason for ``row``, or None when it still waits."""
    status = conn.execute("SELECT status FROM tasks WHERE id = ?", (row["task_id"],)).fetchone()
    now_status = str(status[0] or "") if status else _fresh.GONE
    if all(_fresh.is_stale(now_status, k) for k in row.get("kinds") or ()):
        return f"card is {now_status}"
    return owner_acted(conn, row)


def _read_drop_reason(kb: Any, row: dict) -> Optional[str]:
    conn = kb.connect_readonly(board=row.get("board") or None)
    try:
        conn.row_factory = sqlite3.Row
        return evaluate(conn, row)
    finally:
        conn.close()


def _state(runner: Any) -> PendingWakes:
    state = getattr(runner, "_kanban_pending_wakes", None)
    if state is None:
        state = PendingWakes(state_path())
        runner._kanban_pending_wakes = state
    return state


def latest_comment_id(kb: Any, board: Optional[str], task_id: str) -> int:
    """Sync: the card's newest ``task_comments.id`` (0 when none / unreadable)."""
    try:
        conn = kb.connect_readonly(board=board or None)
    except Exception:
        return 0
    try:
        row = conn.execute("SELECT MAX(id) FROM task_comments WHERE task_id = ?", (task_id,)).fetchone()
        return int(row[0] or 0) if row else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def record(runner: Any, **fields: Any) -> dict:
    """Persist one pending row (call from the loop; the write is offloaded by the caller)."""
    state = _state(runner)
    row = state.add(**fields)
    logger.info(
        "kanban wake deferred for %s on %s/%s: %s (cursor %d); fires when the gate clears "
        "or after %ds",
        row["task_id"], row["platform"], row["chat_id"], row["reason"], row["cursor"],
        ceiling_seconds(),
    )
    return row


async def tick(runner: Any, *, now: Optional[float] = None,
               downgrade: Optional[Callable[[dict], Optional[str]]] = None) -> int:
    """Re-evaluate every pending row. Returns the number of wakes fired.

    ``downgrade(row)`` answers the gate for the row's profile (``None`` = clear);
    the default is the notifier's ``_wake_downgrade_reason`` with ``human_owner=True``.
    """
    import asyncio

    from gateway.config import Platform as _Platform
    from gateway.kanban_watchers_common import _to_thread_process_service, _wake_downgrade_reason
    from gateway.kanban_watchers_notifier import _adapter_for_subscription
    from gateway.wake import adapter_supports_push, deliver_wake
    from hermes_cli import kanban_db as kb

    state = getattr(runner, "_kanban_pending_wakes", None)
    if state is None:
        state = _state(runner)
    if not state.rows:
        return 0
    now = time.time() if now is None else now
    ceiling = ceiling_seconds()
    fired = 0
    dirty = False
    for key, row in list(state.rows.items()):
        task_id, platform_str, chat_id = row["task_id"], row["platform"], row["chat_id"]
        try:
            drop = await asyncio.to_thread(_read_drop_reason, kb, row)
        except Exception as exc:
            logger.warning("kanban wake deferred: cannot re-read %s (%s); retrying", key, exc)
            continue
        if drop:
            logger.info("kanban wake deferred dropped for %s on %s/%s: %s",
                        task_id, platform_str, chat_id, drop)
            state.rows.pop(key, None)
            dirty = True
            continue
        age = now - float(row.get("first_seen") or now)
        sub = dict(row.get("sub") or {})
        sub.setdefault("task_id", task_id)
        sub.setdefault("platform", platform_str)
        sub.setdefault("chat_id", chat_id)
        sub_profile = row.get("sub_profile") or None
        try:
            plat = _Platform(platform_str)
        except ValueError:
            logger.warning("kanban wake deferred dropped for %s on %s/%s: unknown platform",
                           task_id, platform_str, chat_id)
            state.rows.pop(key, None)
            dirty = True
            continue
        if downgrade is not None:
            reason = downgrade(row)
        else:
            reason = await _to_thread_process_service(
                _wake_downgrade_reason, runner,
                runner._wake_gate_profile(plat, sub, sub_profile), True,
            )
        if reason and age < ceiling:
            continue  # still contended, under the ceiling: wait
        why = (f"gate cleared (was: {row.get('reason')})" if not reason
               else f"ceiling {ceiling}s reached (still: {reason})")
        adapter = _adapter_for_subscription(runner, plat, sub, sub_profile)
        if adapter is None:
            logger.info("kanban wake deferred-fire for %s on %s/%s waits: no adapter",
                        task_id, platform_str, chat_id)
            continue
        text = f"{row.get('text') or ''}\n{deferred_line(row.get('first_seen') or now, str(row.get('reason') or '?'), now)}"
        try:
            if adapter_supports_push(adapter):
                source, _identity = runner._build_wake_source(
                    plat, adapter, sub, profile=sub_profile,
                    creator_session_key=row.get("session_key") or None,
                )
                await deliver_wake(
                    adapter, text=text, session_id=row.get("session_key") or "", source=source,
                    metadata={_fresh.META_KEY: [_fresh.card_entry(
                        row.get("board") or None, task_id, row.get("kinds") or (),
                        row.get("event_ts"), text,
                    )]},
                )
            else:
                await deliver_wake(
                    adapter, text=text, session_id=row.get("session_key") or chat_id,
                    profile=sub_profile,
                )
        except Exception as exc:
            logger.warning("kanban wake deferred-fire failed for %s on %s/%s: %s; kept for retry",
                           task_id, platform_str, chat_id, exc, exc_info=True)
            continue
        fired += 1
        logger.info("kanban wake deferred-fire for %s on %s/%s after %dm: %s",
                    task_id, platform_str, chat_id, int(age // 60), why)
        state.rows.pop(key, None)
        dirty = True
    if dirty:
        await asyncio.to_thread(state.write)
    return fired
