"""Local index of what we've sent (and, for WhatsApp, received) keyed by ``(chat_id, message_id)``.

Telegram does NOT echo a rich message's content back in ``reply_to_message`` (``.text``/``.caption``
empty, ``.api_kwargs`` None), and WhatsApp quotes carry only the quoted message's id (Cloud API) or a
thumbnail stub (Baileys) — never the original bytes. So a reply to something we sent arrives with no
quotable text and no way to re-fetch a quoted attachment. We remember ``message_id -> text`` and
``message_id -> [(local_path, mime)]`` at send/receive time and look them up by ``reply_to_id`` on
inbound. Best-effort and dependency-free: every operation swallows errors and degrades to a no-op /
``None`` / ``[]`` so it can never break a send or an inbound message.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Optional
from utils import atomic_json_write

_MAX_ENTRIES = 1000
_MAX_PENDING_WRITES = 1000
_MAX_TEXT_CHARS = 2000

# The synchronous API and the dedicated writer thread both touch the same file.
_WRITE_LOCK = threading.Lock()

# Queue drained by a single dedicated daemon thread.  A DEDICATED thread, not
# ``asyncio.to_thread``: that runs on the shared default executor, so a stalled
# index write would consume capacity every other ``to_thread`` caller needs,
# and a saturated executor would hold adapter progress behind unrelated work.
# Repeated writes for the same message coalesce while the writer is busy.
_PENDING_CONDITION = threading.Condition()
_PENDING_WRITES: "OrderedDict[tuple[str, str], tuple[str, Any, Any, dict]]" = OrderedDict()
_RECENT_WRITES: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
_WRITER_STARTED = False


def _store_path() -> str:
    from hermes_constants import get_hermes_home  # honors the active profile override
    return os.path.join(str(get_hermes_home()), "state", "rich_sent_index.json")


def _key(chat_id, message_id) -> str:
    return f"{chat_id}:{message_id}"


def _load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _update(chat_id, message_id, fields: dict) -> None:
    """Merge ``fields`` into the ``(chat_id, message_id)`` entry. No-op on any failure."""
    try:
        with _WRITE_LOCK:
            _update_locked(_store_path(), chat_id, message_id, fields)
    except Exception:
        return


def _update_locked(path: str, chat_id, message_id, fields: dict) -> None:
    """The read-modify-write half of :func:`_update`. Caller holds ``_WRITE_LOCK``."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = _load(path)
    key = _key(chat_id, message_id)
    entry = data.get(key)
    entry = entry if isinstance(entry, dict) else {}
    data[key] = {**entry, **fields, "ts": int(time.time())}
    if len(data) > _MAX_ENTRIES:  # trim oldest by timestamp
        for k, _ in sorted(data.items(), key=lambda kv: kv[1].get("ts", 0))[: len(data) - _MAX_ENTRIES]:
            data.pop(k, None)
    atomic_json_write(path, data, indent=None)  # atomic; tolerates concurrent writers racing


async def _update_async(chat_id, message_id, fields: dict) -> None:
    """Queue a best-effort write without holding adapter progress.

    ``atomic_json_write`` ends in an ``os.replace``, and the whole
    read-modify-write runs inline on whatever thread calls it. Every caller
    below is an ``async def`` on the send/inbound path, so on the running loop
    this stalls the entire gateway -- every other adapter's polling, every
    in-flight turn, every heartbeat -- for the duration of a filesystem write
    that nothing is waiting on. The index is best-effort by construction, so
    the write is queued and the caller proceeds.
    """
    try:
        _enqueue_write(_store_path(), chat_id, message_id, fields)
    except Exception:
        return


def _enqueue_write(path: str, chat_id, message_id, fields: dict) -> None:
    global _WRITER_STARTED

    pending_key = (path, _key(chat_id, message_id))
    with _PENDING_CONDITION:
        queued = _PENDING_WRITES.get(pending_key)
        merged = {**(queued[3] if queued else {}), **fields}
        _PENDING_WRITES[pending_key] = (path, chat_id, message_id, merged)
        _PENDING_WRITES.move_to_end(pending_key)
        # Serve reads that arrive before the writer catches up: an inbound
        # reply can reference a message we only just sent.
        _RECENT_WRITES[pending_key] = {**_RECENT_WRITES.get(pending_key, {}), **fields}
        _RECENT_WRITES.move_to_end(pending_key)
        while len(_PENDING_WRITES) > _MAX_PENDING_WRITES:
            _PENDING_WRITES.popitem(last=False)
        while len(_RECENT_WRITES) > _MAX_ENTRIES:
            _RECENT_WRITES.popitem(last=False)
        if not _WRITER_STARTED:
            threading.Thread(
                target=_writer_loop, name="rich-sent-store-writer", daemon=True
            ).start()
            _WRITER_STARTED = True
        _PENDING_CONDITION.notify()


def _writer_loop() -> None:
    while True:
        with _PENDING_CONDITION:
            while not _PENDING_WRITES:
                _PENDING_CONDITION.wait()
            _, item = _PENDING_WRITES.popitem(last=False)
        path, chat_id, message_id, fields = item
        try:
            with _WRITE_LOCK:
                _update_locked(path, chat_id, message_id, fields)
        except Exception:
            pass


def record(chat_id, message_id, text: Optional[str]) -> None:
    """Persist ``text`` for ``(chat_id, message_id)``. No-op on any failure."""
    if not text or message_id is None or chat_id is None:
        return
    _update(chat_id, message_id, {"t": text[:_MAX_TEXT_CHARS]})


async def record_async(chat_id, message_id, text: Optional[str]) -> None:
    """Off-loop :func:`record` for coroutine callers."""
    if not text or message_id is None or chat_id is None:
        return
    await _update_async(chat_id, message_id, {"t": text[:_MAX_TEXT_CHARS]})


def record_media(chat_id, message_id, media: list[tuple[str, str]]) -> None:
    """Persist local attachment ``(path, mime)`` pairs for ``(chat_id, message_id)``."""
    if not media or message_id is None or chat_id is None:
        return
    _update(chat_id, message_id, {"m": _media_fields(media)})


async def record_media_async(chat_id, message_id, media: list[tuple[str, str]]) -> None:
    """Off-loop :func:`record_media` for coroutine callers."""
    if not media or message_id is None or chat_id is None:
        return
    await _update_async(chat_id, message_id, {"m": _media_fields(media)})


def _media_fields(media: list[tuple[str, str]]) -> list[list[str]]:
    return [[str(p), str(mt or "")] for p, mt in media if p]


def _entry(chat_id, message_id) -> dict:
    if message_id is None or chat_id is None:
        return {}
    path = _store_path()
    key = _key(chat_id, message_id)
    # A queued write has not reached disk yet; an inbound reply can reference a
    # message we only just sent, so the pending value wins over the stored one.
    with _PENDING_CONDITION:
        recent = dict(_RECENT_WRITES.get((path, key)) or {})
    stored = _load(path).get(key)
    stored = stored if isinstance(stored, dict) else {}
    return {**stored, **recent}


def lookup(chat_id, message_id) -> Optional[str]:
    """Return stored text for ``(chat_id, message_id)`` or ``None``."""
    return _entry(chat_id, message_id).get("t") or None


def lookup_media(chat_id, message_id) -> list[tuple[str, str]]:
    """Return stored ``(path, mime)`` pairs whose file still exists (attachments may be temp files)."""
    pairs = _entry(chat_id, message_id).get("m") or []
    return [(p, mt) for p, mt in pairs if isinstance(p, str) and os.path.isfile(p)]
