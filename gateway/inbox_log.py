"""Where each incoming message from a person goes: one INFO line per disposition (card t_1eb0a176, Step 0).

Logging only. Nothing here changes what is delivered, queued, held, released, refused or in what
order. Every line carries ids, lengths and session keys, never message text.

Line shapes (``scripts/inbox_reconcile.py`` reads them back)::

    inbox_disposition disposition=<d> session=<key> platform=<p> msg_id=<id> depth=<n> path=<where> \
held_release=<bool> reorder_check=<ordered|skipped|->
    inbox_reorder_detected session=<key> platform=<p> msg_id=<id> high_water=<id> held_release=<bool> \
high_water_source=<memory|store>

``<d>`` is one of :data:`DISPOSITIONS`. ``steered`` and ``became-next-turn`` are deliveries to a turn
and feed the reorder check; ``queued`` and ``merged-into-head`` wait in the active queue; ``refused``
was not taken (queue at cap, or a drain that rejects). The held store keeps its own per-item lines
(``queue_mgmt item op=...``).

The reorder check compares a delivered message's platform id with the highest id already delivered
in that session. Discord snowflakes and Telegram per-chat ids are ordered integers; any other
platform logs ``reorder_check=skipped``. The high-water mark is seeded from the session store on
first use per session (so it survives a gateway restart); that one read runs on a background
thread, so a delivery never waits on SQLite, and every later check is in-memory arithmetic.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DISPOSITIONS = ("steered", "queued", "held", "merged-into-head", "refused", "became-next-turn")
DELIVERED = frozenset({"steered", "became-next-turn"})
ORDERED_ID_PLATFORMS = frozenset({"discord", "telegram"})

# The steer origin header keeps a steered message's id (its row has no platform_message_id).
_STEER_HEADER_ID = re.compile(r'"message_id": "?(\d+)"?')
_SEED_ROWS = 500   # newest user rows read once per session to seed the high-water mark


def _platform(event: Any) -> str:
    platform = getattr(getattr(event, "source", None), "platform", None)
    return str(getattr(platform, "value", platform) or "-")


def _msg_id(event: Any) -> Optional[str]:
    mid = getattr(event, "message_id", None)
    return str(mid) if mid not in (None, "") else None


def is_person(event: Any) -> bool:
    return event is not None and getattr(event, "internal", False) is not True


def comparable(platform: str, msg_id: Optional[str]) -> bool:
    return platform in ORDERED_ID_PLATFORMS and bool(msg_id) and msg_id.isdigit()


def log_disposition(disposition: str, session_key: Any, event: Any, *, depth: Optional[int] = None,
                    path: str = "-", runner: Any = None, session_id: Optional[str] = None) -> None:
    """One INFO line for *event* (a person's message only; internal wakes are skipped).

    A delivery (``steered`` / ``became-next-turn``) with a comparable id is handed to the reorder
    check, which needs *runner* (for the session store path) and *session_id*."""
    if not is_person(event):
        return
    platform = _platform(event)
    mid = _msg_id(event)
    released = getattr(event, "_queue_released", False) is True
    check = "-"
    if disposition in DELIVERED:
        check = "ordered" if comparable(platform, mid) else "skipped"
    logger.info(
        "inbox_disposition disposition=%s session=%s platform=%s msg_id=%s depth=%s path=%s "
        "held_release=%s reorder_check=%s",
        disposition, session_key, platform, mid or "-", "-" if depth is None else depth, path,
        released, check,
    )
    if check == "ordered":
        _TRACKER.submit(session_key, session_id, platform, str(mid), released, _db_path(runner))


def _db_path(runner: Any) -> Optional[str]:
    db = getattr(getattr(runner, "session_store", None), "_db", None)
    path = getattr(db, "db_path", None)
    return str(path) if path else None


class _ReorderTracker:
    """Per-session high-water mark of delivered platform ids.

    A session whose mark is known is checked inline (arithmetic and one log call, no I/O). Only the
    first delivery of a session in this process reads the session store, and that read runs on ONE
    worker thread so a delivery never waits on SQLite; deliveries of that session arriving meanwhile
    queue behind it on the same thread, so they are compared in arrival order."""

    def __init__(self) -> None:
        self._marks: Dict[str, int] = {}
        self._seeding: Dict[str, int] = {}   # session key -> checks queued on the worker
        self._lock = threading.Lock()
        self._pool: Optional[ThreadPoolExecutor] = None

    def submit(self, session_key: Any, session_id: Optional[str], platform: str, msg_id: str,
               released: bool, db_path: Optional[str]) -> None:
        key = str(session_key)
        with self._lock:
            needs_store = key not in self._marks and bool(db_path) and bool(session_id)
            if not needs_store and not self._seeding.get(key):
                self._compare(key, platform, int(msg_id), released, self._marks.get(key), "memory")
                return
            self._seeding[key] = self._seeding.get(key, 0) + 1
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inbox-reorder")
            pool = self._pool
        try:
            pool.submit(self._seeded_check, key, session_id, platform, int(msg_id), released, db_path)
        except RuntimeError:   # interpreter shutdown: the pool no longer takes work
            with self._lock:
                self._seeding.pop(key, None)

    def _seeded_check(self, key: str, session_id: Optional[str], platform: str, current: int,
                      released: bool, db_path: Optional[str]) -> None:
        """Worker thread: read the store for a session with no mark yet, then compare."""
        with self._lock:
            known = key in self._marks
        seeded = None if known else seed_high_water(db_path, session_id)
        with self._lock:
            mark, source = self._marks.get(key), "memory"
            if mark is None:
                mark, source = seeded, "store"
            self._compare(key, platform, current, released, mark, source)
            left = self._seeding.get(key, 1) - 1
            if left > 0:
                self._seeding[key] = left
            else:
                self._seeding.pop(key, None)

    def _compare(self, key: str, platform: str, current: int, released: bool, mark: Optional[int],
                 source: str) -> None:
        """Caller holds the lock. Logs when *current* is lower than an id already delivered."""
        if mark is not None and current < mark:
            logger.warning(
                "inbox_reorder_detected session=%s platform=%s msg_id=%s high_water=%s "
                "held_release=%s high_water_source=%s",
                key, platform, current, mark, released, source,
            )
        self._marks[key] = current if mark is None else max(mark, current)

    def wait_idle(self) -> None:
        """Tests: block until every check handed to the worker has run."""
        with self._lock:
            pool = self._pool
        if pool is not None:
            pool.submit(lambda: None).result(timeout=10)


def seed_high_water(db_path: Optional[str], session_id: Optional[str]) -> Optional[int]:
    """Highest numeric platform id among the session's newest user rows (the column, or the steer
    origin header for steered rows), read through a read-only connection. None when unknown."""
    if not db_path or not session_id:
        return None
    from hermes_state_messages import STEER_ORIGIN_HEADER
    best: Optional[int] = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            rows = conn.execute(
                "SELECT platform_message_id, content FROM messages WHERE session_id = ? AND role = 'user' "
                "ORDER BY id DESC LIMIT ?", (session_id, _SEED_ROWS)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.info("inbox_reorder_seed_failed session_id=%s", session_id)
        return None
    for pid, content in rows:
        ids = [str(pid)] if pid is not None else []
        if not ids and isinstance(content, str) and STEER_ORIGIN_HEADER in content[:4000]:
            ids = _STEER_HEADER_ID.findall(content[:4000])
        for value in ids:
            if value.isdigit():
                best = int(value) if best is None else max(best, int(value))
    return best


_TRACKER = _ReorderTracker()
