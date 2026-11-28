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
first use per session (so it survives a gateway restart) and the check runs on one background
thread, so a delivery never waits on it.
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
    """Per-session high-water mark of delivered platform ids, kept on ONE worker thread.

    Submissions are FIFO on a single thread, so the marks need no lock and a delivery never blocks.
    The mark is seeded from the session store (read-only connection) on first use per session."""

    def __init__(self) -> None:
        self._marks: Dict[str, int] = {}
        self._pool: Optional[ThreadPoolExecutor] = None
        self._pool_lock = threading.Lock()

    def submit(self, session_key: Any, session_id: Optional[str], platform: str, msg_id: str,
               released: bool, db_path: Optional[str]) -> None:
        with self._pool_lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inbox-reorder")
            pool = self._pool
        try:
            pool.submit(self.check, str(session_key), session_id, platform, msg_id, released, db_path)
        except RuntimeError:   # interpreter shutdown: the pool no longer takes work
            pass

    def check(self, session_key: str, session_id: Optional[str], platform: str, msg_id: str,
              released: bool, db_path: Optional[str]) -> bool:
        """Runs on the worker thread. True when *msg_id* is lower than one already delivered."""
        current = int(msg_id)
        source = "memory"
        mark = self._marks.get(session_key)
        if mark is None:
            source = "store"
            mark = seed_high_water(db_path, session_id)
        detected = mark is not None and current < mark
        if detected:
            logger.warning(
                "inbox_reorder_detected session=%s platform=%s msg_id=%s high_water=%s "
                "held_release=%s high_water_source=%s",
                session_key, platform, current, mark, released, source,
            )
        self._marks[session_key] = current if mark is None else max(mark, current)
        return detected

    def wait_idle(self) -> None:
        """Tests: block until every submitted check has run."""
        with self._pool_lock:
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
