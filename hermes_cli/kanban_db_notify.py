"""Notification subscriptions consumed by the gateway kanban-notifier: per-(task, platform, chat, thread) rows with delivery metadata, unseen-event cursors and purge of stale done-task subs.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path
from typing import Any
from typing import Iterable
from typing import Mapping
from typing import Optional
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from hermes_cli.kanban_db import Event

# Notifier reaction to a terminal event: "notify" = passive adapter.send only
# (default); "notify+wake" = send AND wake the destination agent; "wake" = wake only.
_NOTIFY_DELIVERY_MODES = ("notify", "notify+wake", "wake")
_SCALAR_TYPES = (str, int, float, bool)

# Subscription primary key predicate; every per-row statement below binds
# ``(task_id, platform, chat_id, thread_id or "")`` against it.
_SUB_KEY_WHERE = "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?"


def _sub_key(task_id: str, platform: str, chat_id: str, thread_id: Optional[str]) -> tuple:
    return (task_id, platform, chat_id, thread_id or "")


def _encode_notify_delivery_metadata(metadata: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Serialize platform send metadata stored on notification subscriptions."""
    if not isinstance(metadata, Mapping):
        return None
    clean = {
        str(key): value
        for key, value in metadata.items()
        if value is not None and isinstance(value, _SCALAR_TYPES)
    }
    if not clean:
        return None
    return json.dumps(clean, sort_keys=True, separators=(",", ":"))


def _decode_notify_delivery_metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if not raw:
        return {}
    try:
        data = json.loads(str(raw))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, _SCALAR_TYPES)}


def add_notify_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    user_id: Optional[str] = None,
    user_id_alt: Optional[str] = None,
    chat_type: Optional[str] = None,
    scope_id: Optional[str] = None,
    notifier_profile: Optional[str] = None,
    delivery_mode: Optional[str] = None,
    delivery_metadata: Optional[Mapping[str, Any]] = None,
) -> None:
    """Register a gateway source that wants terminal-state notifications
    for ``task_id``. Idempotent on (task, platform, chat, thread).

    ``user_id_alt`` records the originating source's platform-specific stable
    alt ID (Signal UUID, Feishu union_id, ...) alongside ``user_id``. Active-wake
    replay must reproduce it so the woken turn's ``build_session_key`` matches
    the original event's — ``build_session_key`` prefers ``user_id_alt`` over
    ``user_id`` (gateway/session.py), so replaying only ``user_id`` would key a
    wake into a different session whenever the two diverge for this source.

    ``chat_type`` records the originating source's chat type; the active-wake
    delivery modes replay it so the woken turn resolves the operator's real
    channel. ``None`` keeps an existing row's value.

    ``delivery_mode`` (see ``_NOTIFY_DELIVERY_MODES``) selects how the
    kanban-notifier reacts to a terminal event for this subscription. ``None``
    leaves an existing row's mode untouched (and inserts the ``"notify"``
    default for a fresh row); an explicit value is last-write-wins, so an
    operator can intentionally re-subscribe to change the mode (e.g.
    ``notify`` -> ``wake``). An unknown value falls back to ``"notify"``.
    New subscriptions start "caught up": ``last_event_id`` snaps to the
    task's current ``MAX(task_events.id)`` at creation instead of the
    schema default 0. A cursor of 0 on an already-active task made the
    gateway notifier replay every historical terminal event on its next
    tick — and with many stale subs, a single boot-time burst of 100+
    messages (issue #29905). Subscribers only want events that occur
    AFTER they subscribe; the gateway/tool auto-subscribe paths run at
    task creation, where the snapshot is 0 anyway.

    ``delivery_metadata`` merges supplied routing anchors into an existing
    row so re-subscribing never discards them.

    ``user_id_alt`` and ``scope_id`` carry the two remaining session-key
    inputs that ``user_id`` alone cannot express (see the wake site in
    ``gateway/kanban_watchers.py``). Both are pure routing DATA copied off
    the creating turn's own ``SessionSource`` — never inferred, never
    defaulted — so a row written without them behaves exactly as before.
    """
    insert_mode = delivery_mode if delivery_mode in _NOTIFY_DELIVERY_MODES else (
        # api_server is stateless: the adapter has no send() — the wake
        # self-post IS the delivery on that path (see gateway/wake.py and
        # test_kanban_notifier_apiserver_wake). A plain-'notify' default
        # would leave those subscriptions with no delivery mechanism at
        # all, regressing the pre-delivery_mode behavior where a task
        # carrying a session_id always woke. Explicit modes still win.
        "notify+wake" if platform == "api_server" else "notify"
    )
    # 🔴 CHOKE POINT: canonicalize here, not at each caller.
    #
    # A Discord guild chat is keyed 'group' by build_session_key. A row stored
    # as chat_type='channel' cannot match its own chat's routing entry, so the
    # wake finds no identity, keys a bare group:<chat> session, and mints a
    # phantom that replies into the user's channel at the config default
    # (2026-09-12 incident; readers fixed in #682, two writers in #684).
    #
    # #684 put the guard at two of the FOUR callers — the classic per-caller
    # placement bug, blind to every call site added later. Measured after that
    # deploy: subscribe_calling_session() correctly stored 'group' while a
    # direct add_notify_sub(chat_type='channel') still persisted 'channel'.
    # This function is the single layer every writer shares (the CLI's
    # --chat-type, the dashboard plugin, the gateway tools), so the invariant
    # belongs here and cannot be bypassed by a future caller.
    #
    # Non-Discord platforms are untouched: canonical_chat_type is a no-op for
    # them, and Teams/Telegram/HomeAssistant own 'channel' as a real type.
    # Fails open — a canonicalizer import error must never block a
    # subscription (losing the sub is worse than storing a legacy spelling,
    # which the fleet data-lint still repairs).
    if chat_type:
        try:
            from gateway.routing_identity import canonical_chat_type
            chat_type = canonical_chat_type(platform, chat_type)
        except Exception:  # pragma: no cover - never block a subscription
            pass
    insert_chat_type = chat_type or "dm"
    now = int(time.time())
    with _kbc.write_txn(conn):
        # ``delivery_metadata`` merges supplied routing anchors into an existing
        # row so re-subscribing never discards them (upstream semantics).
        existing = conn.execute(
            "SELECT delivery_metadata FROM kanban_notify_subs "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
            (task_id, platform, chat_id, thread_id or ""),
        ).fetchone()
        existing_metadata = (
            _decode_notify_delivery_metadata(existing["delivery_metadata"]) if existing else {}
        )
        merged_metadata = dict(existing_metadata or {})
        if delivery_metadata:
            merged_metadata.update(delivery_metadata)
        metadata_json = _encode_notify_delivery_metadata(merged_metadata) if merged_metadata else None
        conn.execute(
            """
            INSERT OR IGNORE INTO kanban_notify_subs
                (task_id, platform, chat_id, thread_id, user_id, user_id_alt,
                 chat_type, scope_id, notifier_profile, delivery_mode,
                 delivery_metadata, created_at, last_event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT MAX(id) FROM task_events WHERE task_id = ?), 0))
            """,
            (
                task_id,
                platform,
                chat_id,
                thread_id or "",
                user_id,
                user_id_alt,
                insert_chat_type,
                scope_id,
                notifier_profile,
                insert_mode,
                metadata_json,
                now,
                task_id,
            ),
        )
        if chat_type:
            # Explicit chat_type is last-write-wins on re-subscribe.
            conn.execute(
                """
                UPDATE kanban_notify_subs
                   SET chat_type = ?
                 WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?
                """,
                (chat_type, task_id, platform, chat_id, thread_id or ""),
            )
        if notifier_profile:
            # Self-heal legacy rows that predate notifier ownership.
            conn.execute(
                """
                UPDATE kanban_notify_subs
                   SET notifier_profile = ?
                 WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?
                   AND (notifier_profile IS NULL OR notifier_profile = '')
                """,
                (notifier_profile, task_id, platform, chat_id, thread_id or ""),
            )
        if user_id:
            # Self-heal the participant identity, same fill-a-hole rule as
            # chat_type / notifier_profile above. Load-bearing beyond tidiness:
            # the wake injector rebuilds the creator's scope from this column
            # (gateway/kanban_watchers.py), and build_session_key() omits the
            # participant segment when it is absent. An identity-less row
            # therefore wakes a SECOND session key for the same chat — one no
            # human can send to, so /reasoning and /model overrides set by the
            # user never reach it. Because this INSERT is OR IGNORE, a later
            # subscribe from a turn that DOES carry an identity is the only
            # chance to repair a row written before the identity was plumbed.
            #
            # Fill-a-hole ONLY: never repoint a sub that already names a
            # participant, or a second user subscribing in the same chat would
            # hijack the first user's notification lane.
            conn.execute(
                """
                UPDATE kanban_notify_subs
                   SET user_id = ?
                 WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?
                   AND (user_id IS NULL OR user_id = '')
                """,
                (user_id, task_id, platform, chat_id, thread_id or ""),
            )
        if user_id_alt:
            # Same fill-a-hole rule, one level deeper. ``build_session_key``
            # keys the participant on ``user_id_alt or user_id``, so on an
            # alt-keyed platform this column — not ``user_id`` — is what the
            # wake must reproduce.
            #
            # Gated on the row being identity-less in BOTH columns rather than
            # just this one. Backfilling the alt id onto a row that already
            # names a DIFFERENT ``user_id`` would repoint that row's lane to
            # another participant, which is precisely the hijack the ``user_id``
            # guard above exists to prevent: the pair is one identity and must
            # be healed as a unit, never interleaved.
            conn.execute(
                """
                UPDATE kanban_notify_subs
                   SET user_id_alt = ?
                 WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?
                   AND (user_id_alt IS NULL OR user_id_alt = '')
                   AND (user_id IS NULL OR user_id = '' OR user_id = ?)
                """,
                (user_id_alt, task_id, platform, chat_id, thread_id or "",
                 user_id or ""),
            )
        if scope_id:
            # Workspace scope is a property of the CHAT, not of a participant,
            # so healing it can never repoint someone's lane — but keep it
            # fill-a-hole anyway: a row that already names a scope came from a
            # real turn in that workspace, and a differing value means the
            # chat_id is ambiguous across workspaces, which is a condition to
            # preserve for diagnosis rather than silently overwrite.
            conn.execute(
                """
                UPDATE kanban_notify_subs
                   SET scope_id = ?
                 WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?
                   AND (scope_id IS NULL OR scope_id = '')
                """,
                (scope_id, task_id, platform, chat_id, thread_id or ""),
            )
        if delivery_mode in _NOTIFY_DELIVERY_MODES:
            # Explicit delivery_mode is last-write-wins on re-subscribe.
            conn.execute(
                """
                UPDATE kanban_notify_subs
                   SET delivery_mode = ?
                 WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?
                """,
                (delivery_mode, task_id, platform, chat_id, thread_id or ""),
            )
        if metadata_json:
            # Refresh the routing anchor for duplicate subscriptions.
            conn.execute(
                """
                UPDATE kanban_notify_subs
                   SET delivery_metadata = ?
                 WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?
                """,
                (metadata_json, task_id, platform, chat_id, thread_id or ""),
            )


def _notify_profile_filter(
    notifier_profiles: Optional[Iterable[str]],
    *,
    include_unowned: bool,
) -> tuple[str, list[str]]:
    """Build an optional SQL predicate for notification profile ownership."""
    if notifier_profiles is None:
        return "", []

    profiles = sorted({str(p).strip() for p in notifier_profiles if str(p).strip()})
    clauses: list[str] = []
    params: list[str] = []
    if profiles:
        clauses.append("notifier_profile IN (" + ",".join("?" for _ in profiles) + ")")
        params.extend(profiles)
    if include_unowned:
        clauses.append("notifier_profile IS NULL OR notifier_profile = ''")
    if not clauses:
        return "0", []
    return "(" + ") OR (".join(clauses) + ")", params


def list_notify_subs(
    conn: sqlite3.Connection,
    task_id: Optional[str] = None,
    *,
    notifier_profiles: Optional[Iterable[str]] = None,
    include_unowned: bool = False,
) -> list[dict]:
    """List subscriptions, optionally restricted to notifier profile owners.

    No ``notifier_profiles`` -> all subscriptions. Gateway notifiers pass the
    profiles they own so they cannot claim another gateway's events;
    ``include_unowned`` (dispatch owner) covers legacy rows without a stamp.
    """
    owner_where, owner_params = _notify_profile_filter(
        notifier_profiles, include_unowned=include_unowned,
    )
    where: list[str] = []
    params: list[Any] = []
    if task_id is not None:
        where.append("task_id = ?")
        params.append(task_id)
    if owner_where:
        where.append(owner_where)
        params.extend(owner_params)
    sql = "SELECT * FROM kanban_notify_subs"
    if where:
        sql += " WHERE " + " AND ".join(f"({clause})" for clause in where)
    out: list[dict] = []
    for row in conn.execute(sql, params).fetchall():
        item = dict(row)
        if "delivery_metadata" in item:
            item["delivery_metadata"] = _decode_notify_delivery_metadata(item.get("delivery_metadata"))
        out.append(item)
    return out


def count_notify_subs(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
    notifier_profiles: Optional[Iterable[str]] = None,
    include_unowned: bool = False,
    platform: Optional[str] = None,
    chat_id: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> int:
    """Count ``kanban_notify_subs`` rows via a read-only connection — the
    notifier's cheap zero-subscription early exit. Unlike :func:`connect` it
    never creates the file, runs init/migration or opens writable; WAL rows are
    still visible so a fresh sub is never missed. Missing DB / missing table
    counts as zero; platform matches case-insensitively (as notifier routing),
    chat/thread exactly. Raises :class:`sqlite3.Error` if the DB exists but is
    unreadable — callers pick their own fallback.
    """
    path = db_path if db_path is not None else _kb.kanban_db_path(board=board)
    if not path.exists():
        return 0
    owner_where, owner_params = _notify_profile_filter(
        notifier_profiles, include_unowned=include_unowned,
    )
    clauses: list[str] = []
    params: list[Any] = []
    if owner_where:
        clauses.append(f"({owner_where})")
        params.extend(owner_params)
    for clause, value in (
        ("LOWER(platform) = LOWER(?)", platform),
        ("chat_id = ?", chat_id),
        ("thread_id = ?", thread_id),
    ):
        if value is not None:
            clauses.append(clause)
            params.append(value)
    query = "SELECT COUNT(*) FROM kanban_notify_subs"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        try:
            row = conn.execute(query, params).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return 0
            raise
        return int(row[0]) if row else 0
    finally:
        conn.close()


def remove_notify_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
) -> bool:
    with _kb.write_txn(conn):
        cur = conn.execute(
            "DELETE FROM kanban_notify_subs " + _SUB_KEY_WHERE,
            _sub_key(task_id, platform, chat_id, thread_id),
        )
    return cur.rowcount > 0


def purge_stale_done_notify_subs(conn: sqlite3.Connection, *, max_age_days: int = 30) -> int:
    """Delete notify subs whose task sat in ``done``/``blocked`` untouched for
    longer than ``max_age_days`` (``<= 0`` disables); returns rows deleted.

    Subs survive ``done`` because a reopened task must still notify its origin,
    which accumulates forever on never-archiving boards. ``blocked`` is
    abandoned (unlike ``backlog``/``ready``) so it reaps on the same clock. Age
    = latest event, else ``completed_at``, else ``created_at`` — any activity,
    including a reopen, exempts the sub.

    The notifier keeps subscriptions alive through ``done`` because a completed task can be reopened (review
    corrections, continuation) and the reopened cycle must still notify its origin session. On boards that
    never archive, that retention would otherwise accumulate subscription rows forever — each one scanned
    every notifier tick. This GC bounds that: a task that has been ``done`` with no new events for the
    retention window is treated as settled and its subscriptions are purged. ``blocked`` tasks
    (circuit-breaker trips, dead workers) are reaped on the same clock — they are abandoned, not idle,
    unlike a ``backlog``/``ready`` card that is merely waiting for pickup (#100955).
    """
    try:
        days = int(max_age_days)
    except (TypeError, ValueError):
        days = 30
    if days <= 0:
        return 0
    cutoff = int(time.time()) - days * 86400
    with _kb.write_txn(conn):
        cur = conn.execute(
            "DELETE FROM kanban_notify_subs WHERE task_id IN ("
            " SELECT t.id FROM tasks t"
            " WHERE t.status IN ('done', 'blocked')"
            " AND COALESCE("
            "  (SELECT MAX(e.created_at) FROM task_events e"
            "   WHERE e.task_id = t.id),"
            "  t.completed_at, t.created_at, 0"
            " ) < ?)",
            (cutoff,),
        )
    return int(cur.rowcount or 0)


def _notify_cursor(
    conn: sqlite3.Connection, task_id: str, platform: str, chat_id: str, thread_id: Optional[str],
) -> Optional[int]:
    """``last_event_id`` of one subscription row, or ``None`` when unsubscribed."""
    row = conn.execute(
        "SELECT last_event_id FROM kanban_notify_subs " + _SUB_KEY_WHERE,
        _sub_key(task_id, platform, chat_id, thread_id),
    ).fetchone()
    return None if row is None else int(row["last_event_id"])


def unseen_events_for_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    kinds: Optional[Iterable[str]] = None,
) -> tuple[int, list[Event]]:
    """Return ``(new_cursor, events)`` with ``id > last_event_id``. The cursor
    is NOT advanced here; call :func:`advance_notify_cursor` after delivery.
    """
    cursor = _notify_cursor(conn, task_id, platform, chat_id, thread_id)
    if cursor is None:
        return 0, []
    kind_list = list(kinds) if kinds else None
    q = (
        "SELECT * FROM task_events WHERE task_id = ? AND id > ? "
        + ("AND kind IN (" + ",".join("?" * len(kind_list)) + ") " if kind_list else "")
        + "ORDER BY id ASC"
    )
    params: list[Any] = [task_id, cursor]
    if kind_list:
        params.extend(kind_list)
    rows = conn.execute(q, params).fetchall()
    out = [_kb.Event.from_row(r) for r in rows]
    max_id = max([cursor, *(int(r["id"]) for r in rows)])
    return max_id, out


def claim_unseen_events_for_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    kinds: Optional[Iterable[str]] = None,
) -> tuple[int, int, list[Event]]:
    """Atomically claim unseen events for one subscription.

    Returns ``(old_cursor, new_cursor, events)``; when events are returned the
    row's ``last_event_id`` has already been advanced inside ``BEGIN IMMEDIATE``,
    so concurrent gateway watchers on the same board DB serialize on SQLite's
    writer lock and only the first claims a given event range. Callers send the
    events, then leave the cursor or call :func:`rewind_notify_cursor` on
    delivery failure.
    """
    with _kb.write_txn(conn):
        old_cursor = _notify_cursor(conn, task_id, platform, chat_id, thread_id)
        if old_cursor is None:
            return 0, 0, []
        new_cursor, events = unseen_events_for_sub(
            conn, task_id=task_id, platform=platform, chat_id=chat_id,
            thread_id=thread_id, kinds=kinds,
        )
        if not events:
            return old_cursor, old_cursor, []
        _cas_cursor(conn, _sub_key(task_id, platform, chat_id, thread_id), new_cursor, old_cursor)
        return old_cursor, new_cursor, events


def _cas_cursor(conn: sqlite3.Connection, key: tuple, new_cursor: int, expected: int) -> sqlite3.Cursor:
    """Move ``last_event_id`` only if it still equals ``expected``."""
    return conn.execute(
        "UPDATE kanban_notify_subs SET last_event_id = ? " + _SUB_KEY_WHERE + " AND last_event_id = ?",
        (int(new_cursor), *key, int(expected)),
    )


def advance_notify_cursor(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    new_cursor: int,
) -> None:
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = ? " + _SUB_KEY_WHERE,
            (int(new_cursor), *_sub_key(task_id, platform, chat_id, thread_id)),
        )


def record_notify_ping(
    conn: sqlite3.Connection, *, task_id: str, platform: str, chat_id: str,
    thread_id: Optional[str] = None, event_id: int,
) -> None:
    """Checkpoint a sent ping independently of the retryable wake cursor."""
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE kanban_notify_subs SET last_ping_event_id = MAX(last_ping_event_id, ?) "
            + _SUB_KEY_WHERE,
            (int(event_id), *_sub_key(task_id, platform, chat_id, thread_id)),
        )


def rewind_notify_cursor(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    claimed_cursor: int,
    old_cursor: int,
) -> bool:
    """Undo a claim when delivery fails. The CAS guard only rewinds if no later
    notifier advanced the row, so retries never clobber newer progress.
    """
    with _kb.write_txn(conn):
        cur = _cas_cursor(conn, _sub_key(task_id, platform, chat_id, thread_id), old_cursor, claimed_cursor)
    return cur.rowcount > 0

# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb  # noqa: E402
from hermes_cli import kanban_db_connect as _kbc  # noqa: E402
