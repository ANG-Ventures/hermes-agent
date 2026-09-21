"""Local index of text we've sent via ``sendRichMessage`` (Bot API 10.1).

Telegram does NOT echo a rich message's content back in ``reply_to_message``
when a user replies to it (verified: ``.text``/``.caption`` empty,
``.api_kwargs`` None). So replies to the launchd briefings / any rich send
arrive with no quotable text and the agent is blind to what was referenced.

Fix: remember ``message_id -> text`` at send time, look it up by
``reply_to_id`` on inbound. This module is the single source of truth for that
index.

Best-effort and dependency-free: every operation swallows errors and degrades
to a no-op / ``None`` so it can never break a send or an inbound message.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from typing import Optional

_MAX_ENTRIES = 1000
_MAX_TEXT_CHARS = 2000

# ``record`` is a read-modify-write of one JSON file.  On the event loop it was
# implicitly serialized by the loop itself; ``record_async`` runs it in the
# default executor, so two concurrent sends can now genuinely interleave and
# lose an entry.  Serialize the critical section instead of relying on that.
_WRITE_LOCK = threading.Lock()


def _store_path() -> str:
    # Resolve via get_hermes_home() so the active profile override is honored.
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    return os.path.join(str(home), "state", "rich_sent_index.json")


def _key(chat_id, message_id) -> str:
    return f"{chat_id}:{message_id}"


def record(chat_id, message_id, text: Optional[str]) -> None:
    """Persist ``text`` for ``(chat_id, message_id)``. No-op on any failure."""
    if not text or message_id is None or chat_id is None:
        return
    path = _store_path()
    try:
        with _WRITE_LOCK:
            _record_locked(path, chat_id, message_id, text)
    except Exception:
        return


async def record_async(chat_id, message_id, text: Optional[str]) -> None:
    """Off-loop :func:`record`.

    The write is a read-modify-write plus an ``os.replace``: on a network
    filesystem or a stalled disk that blocks the calling thread for as long as
    the rename takes, and on the event loop that stalls EVERY session.  Callers
    on a coroutine must use this; ``record`` stays for synchronous callers.
    """
    if not text or message_id is None or chat_id is None:
        return
    try:
        await asyncio.to_thread(record, chat_id, message_id, text)
    except Exception:
        return


def _record_locked(path: str, chat_id, message_id, text: str) -> None:
    """The read-modify-write half of :func:`record`. Caller holds ``_WRITE_LOCK``."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except (FileNotFoundError, ValueError):
        data = {}
    data[_key(chat_id, message_id)] = {
        "t": text[:_MAX_TEXT_CHARS],
        "ts": int(time.time()),
    }
    # Trim oldest by timestamp when over cap.
    if len(data) > _MAX_ENTRIES:
        for k, _ in sorted(
            data.items(), key=lambda kv: kv[1].get("ts", 0)
        )[: len(data) - _MAX_ENTRIES]:
            data.pop(k, None)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)  # atomic; tolerates concurrent writers racing


def lookup(chat_id, message_id) -> Optional[str]:
    """Return stored text for ``(chat_id, message_id)`` or ``None``."""
    if message_id is None or chat_id is None:
        return None
    try:
        with open(_store_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        entry = data.get(_key(chat_id, message_id))
        if isinstance(entry, dict):
            return entry.get("t") or None
    except (FileNotFoundError, ValueError, AttributeError):
        return None
    return None
