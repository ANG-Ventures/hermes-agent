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
after the deferred cursor, is dropped with a log line: the wake already happened
in effect. "Owner acted" needs an event written from a HUMAN session, proven by the
session's chat source in the owner profile's state.db (see
:func:`_human_session`): a worker running under the owner's profile (any
board), a delegate child, a cron, or a session-less script is not the owner
taking a turn (Prism 1a09cf36846b, 7d9c8fa1f2ee, cb8149c5fbc5). A
delivery failure keeps the row for the next poll.

State: ``<HERMES_HOME>/gateway/kanban_pending_wakes.json`` (the same durable
gateway state dir as the owner-wake ledger; fsync + rename), so a restart
between the downgrade and the fire still fires. :func:`record` raises when the
row cannot be made durable, so the notifier rewinds its claim instead of
advancing the cursor past a wake that lives only in memory (Prism ac41fdbadf47).

Log lines (grep ``kanban wake deferred``):

* ``kanban wake deferred for <task> on <platform>/<chat>: <reason> (cursor N); fires when the gate clears or after <ceiling>s``
* ``kanban wake deferred-fire for <task> on <platform>/<chat> after <N>m: gate cleared (was: <reason>)``
* ``kanban wake deferred-fire for <task> on <platform>/<chat> after <N>m: ceiling <S>s reached (still: <reason>)``
* ``kanban wake deferred dropped for <task> on <platform>/<chat>: <why>``
* ``kanban wake deferred-fire failed for <task> …; kept for retry``
"""
from __future__ import annotations

import contextlib
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
from hermes_cli.kanban_db import HUMAN_REVIEWER_SENTINEL, SUBAGENT_AUTHOR_MARKER

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
        # True while the in-memory rows differ from disk after a failed write;
        # :func:`tick` retries the write every poll until it lands.
        self.unsaved = False
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
        """Blocking (fsync + rename): call via ``asyncio.to_thread`` from the loop.

        Raises ``OSError`` when the state is not durable: callers must not
        acknowledge (advance a cursor past) an event whose wake is only in memory.
        """
        data = {"pending": self.rows}
        tmp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".pending-wakes-", dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            self.unsaved = True
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
            logger.warning("kanban wake deferred: cannot persist state %s: %s", self.path, exc)
            raise
        self.unsaved = False

    def add(self, *, board: Optional[str], sub: dict, sub_profile: Optional[str], cursor: int,
            kinds: Iterable[str], text: str, reason: str, owner: str, home_sid: Optional[str],
            session_key: str, event_ts: Any, now: Optional[float] = None) -> dict:
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
            # Owner events (comments included: each writes a ``commented``
            # event) after this id count as "acted".
            "cursor": int(cursor),
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


# ``sessions.source`` values written by autonomous runs: a dispatched worker
# (``kanban``), a delegate child (``subagent``), cron, one-shot / agent runs and
# recovery replays. Anything else in the owner profile's state.db is a chat
# surface (cli, tui, discord, telegram, ...) a human drives.
_MACHINE_SOURCES = frozenset({
    "kanban", "subagent", "cron", "oneshot", "agent", "batch", "cache-probe",
    "gateway_recovery", "recovered", "api_server", "webhook", "msgraph_webhook",
    "unknown",
})


def _session_source(profile: Any, sid: str) -> Optional[str]:
    """``sessions.source`` of ``sid`` in ``profile``'s own state.db, else None.

    Read-only, one primary-key lookup. Unknown profile, missing DB or row ->
    None (callers treat that as "not provably human").
    """
    try:
        from hermes_cli.profiles import get_profile_dir

        path = get_profile_dir(_ns._operator_label(profile) or "default") / "state.db"  # noqa: SLF001
    except Exception:
        return None
    if not path.is_file():
        return None
    from hermes_state_lockguard import registered_reader
    with registered_reader(path):  # the WAL lock guard never closes a -shm fd under this read (t_aeae3fe7)
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        except sqlite3.Error:
            return None
        try:
            row = db.execute("SELECT source FROM sessions WHERE id = ?", (sid,)).fetchone()
        except sqlite3.Error:
            return None
        finally:
            db.close()
    return str(row[0] or "") if row else None


def _human_session(conn: sqlite3.Connection, sid: Any, profile: Any) -> bool:
    """True only with POSITIVE evidence that ``sid`` is a human chat session.

    The session must be recorded in ``profile``'s own state.db with a chat
    source (not :data:`_MACHINE_SOURCES`). A board-local absence of worker
    signals is not evidence: a worker heartbeats its own board and can comment
    on another (Prism 7d9c8fa1f2ee), and a delegate child never heartbeats
    (Prism cb8149c5fbc5). A worker heartbeat on this board still disqualifies.
    """
    from hermes_cli.kanban_db import is_operator_home, is_unhomed

    sid = str(sid or "").strip()
    if not sid or sid.startswith("cron_") or is_unhomed(sid) or is_operator_home(sid):
        return False
    if conn.execute(
        "SELECT 1 FROM task_events WHERE actor_session_id = ? AND kind = 'heartbeat' LIMIT 1",
        (sid,),
    ).fetchone():
        return False
    source = _session_source(profile, sid)
    return bool(source) and source not in _MACHINE_SOURCES


def _owner_label(owner: Any) -> str:
    """The owner's operator label; ``""`` for the bare ``human`` sentinel.

    ``_operator_label("human")`` is the truthy string ``"human"``, which names
    no profile: treating it as a label made every ``same_operator`` test fail,
    so a bare-``human`` owner's own events never cancelled its wake and a
    STALE wake fired (Apollo review of #1934, cb8149c5fbc5 follow-up).
    """
    label = _ns._operator_label(owner)  # noqa: SLF001
    return "" if label == HUMAN_REVIEWER_SENTINEL else label


def _session_is_owner(conn: sqlite3.Connection, sid: Any, profile: Any, owner: str,
                      home_sid: Optional[str], cache: dict[tuple, bool]) -> bool:
    """A human session (:func:`_human_session`) that is the owner's: its profile
    is the owner's operator label, or, for a bare ``human`` owner with no label,
    it is the card's verified home session."""
    label = _owner_label(owner)
    if label:
        if not _ns.same_operator(profile, label):
            return False
    elif not (home_sid and str(sid or "") == home_sid):
        return False
    key = (str(sid or ""), _ns._operator_label(profile))  # noqa: SLF001
    if key not in cache:
        cache[key] = _human_session(conn, sid, profile)
    return cache[key]


def verified_home_sid(conn: sqlite3.Connection, owner: str, home_sid: Any) -> Optional[str]:
    """Sync: ``home_sid`` when it is verified to belong to ``owner``, else None.

    ``tasks.session_id`` is the card's CREATOR session, which is often the
    originating agent (a worker) rather than the human reviewer; recording it
    would let that agent's later events read as the owner acting (Prism
    dfc7f883625d). Verified = a human session whose own events carry the
    owner's operator label (any profile for a bare ``human`` owner), recorded
    as a chat session in that profile's state.db.
    """
    sid = str(home_sid or "").strip()
    if not sid:
        return None
    label = _owner_label(owner)
    for (profile,) in conn.execute(
        "SELECT DISTINCT actor_profile FROM task_events WHERE actor_session_id = ?", (sid,),
    ):
        if label and not _ns.same_operator(profile, label):
            continue
        if _human_session(conn, sid, profile):
            return sid
    return None


def owner_acted(conn: sqlite3.Connection, row: dict) -> Optional[str]:
    """Why the owner has already acted on the card since the deferred cursor, else None.

    Evidence: a ``task_events`` row after the cursor written from the owner's
    own human session (:func:`_session_is_owner`), or a non-subagent ``commented`` event
    whose author is the explicit ``human:<name>`` label of the owner (a label
    a worker cannot write: ``--author`` needs the operator token). A worker,
    cron or session-less write under the owner's PROFILE label is not evidence
    (Prism 1a09cf36846b), nor is the card's home session unless it passes the
    same check (Prism dfc7f883625d).
    """
    owner = str(row.get("owner") or "")
    home_sid = str(row.get("home_sid") or "") or None
    task_id = row["task_id"]
    try:
        events = conn.execute(
            "SELECT id, kind, payload, actor_profile, actor_session_id FROM task_events "
            "WHERE task_id = ? AND id > ? ORDER BY id",
            (task_id, int(row.get("cursor") or 0)),
        ).fetchall()
        human: dict[tuple, bool] = {}  # one provenance probe per (sid, profile)
        for r in events:
            sid = r["actor_session_id"]
            profiles = [r["actor_profile"]]
            if r["kind"] == "commented":
                try:
                    payload = json.loads(r["payload"] or "{}")
                except ValueError:
                    payload = {}
                author = payload.get("author") if isinstance(payload, dict) else None
                if str(author or "").rstrip().endswith(SUBAGENT_AUTHOR_MARKER.strip()):
                    continue  # a delegate child's comment is never the owner (cb8149c5fbc5)
                if _is_human(author) and (
                    not _owner_label(owner) or _ns.same_operator(author, owner)
                ):
                    # A ``human:<name>`` author needs the operator token; for a
                    # bare ``human`` owner any such author is the human acting.
                    return f"owner {owner} commented (event {r['id']})"
                if author:
                    profiles.append(author)
            if any(_session_is_owner(conn, sid, p, owner, home_sid, human) for p in profiles):
                return f"owner {owner} wrote {r['kind']} (event {r['id']})"
    except sqlite3.Error as exc:
        logger.debug("kanban wake deferred: event read for %s failed: %s", task_id, exc)
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


def read_verified_home_sid(kb: Any, board: Optional[str], owner: str,
                           home_sid: Any) -> Optional[str]:
    """Sync: :func:`verified_home_sid` on the board (None when unreadable)."""
    if not home_sid:
        return None
    try:
        conn = kb.connect_readonly(board=board or None)
    except Exception:
        return None
    try:
        return verified_home_sid(conn, owner, home_sid)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


async def record(runner: Any, **fields: Any) -> dict:
    """Add one pending row and make it durable before returning.

    Raises ``OSError`` when the write fails; the row is rolled back first so a
    retried claim does not leave a memory-only twin that fires later.
    """
    import asyncio

    state = _state(runner)
    key = row_key(fields.get("board"), fields["sub"])
    previous = state.rows.get(key)
    row = state.add(**fields)
    try:
        await asyncio.to_thread(state.write)
    except OSError:
        if previous is None:
            state.rows.pop(key, None)
        else:
            state.rows[key] = previous
        raise
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
    if not state.rows and not state.unsaved:
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
    if dirty or state.unsaved:
        await asyncio.to_thread(state.write)
    return fired
