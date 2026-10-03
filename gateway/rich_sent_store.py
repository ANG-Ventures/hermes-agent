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

import contextvars
import json
import os
import threading
import time
from collections import OrderedDict
from typing import Optional
from utils import atomic_json_write

_MAX_ENTRIES = 1000
_MAX_PENDING_WRITES = 1000
_MAX_TEXT_CHARS = 2000
# ``atomic_json_write`` makes each WRITE atomic, not the load/merge/save triple.
# The dedicated writer thread and the synchronous API both run ``_update``, so
# two concurrent callers (two inbound WhatsApp-Cloud messages, a Telegram send
# racing an edit) would otherwise each load the same pre-state and the later
# ``os.replace`` drops the other key.
_LOCK = threading.Lock()
# ``record_async`` / ``record_media_async`` never wait for the filesystem: the
# write is queued for ONE dedicated daemon thread (not asyncio's shared default
# executor, whose saturation by unrelated work would hold adapter progress) and
# repeated writes for the same message coalesce while the writer is busy.
# ``_RECENT_WRITES`` lets ``lookup`` see a queued entry before it lands on disk.
_PENDING_CONDITION = threading.Condition()
_PENDING_WRITES: OrderedDict[
    tuple[str, str], tuple[contextvars.Context, object, object, dict]
] = OrderedDict()
_RECENT_WRITES: OrderedDict[tuple[str, str], dict] = OrderedDict()
_WRITER_STARTED = False


def _store_path() -> str:
    from hermes_constants import get_hermes_home  # honors the active profile override
    return os.path.join(str(get_hermes_home()), "state", "rich_sent_index.json")


def _load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _update(chat_id, message_id, fields: dict) -> None:
    """Merge ``fields`` into the ``(chat_id, message_id)`` entry. No-op on any failure."""
    path = _store_path()
    with _LOCK:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            data = _load(path)
            key = f"{chat_id}:{message_id}"
            entry = data.get(key)
            entry = entry if isinstance(entry, dict) else {}
            data[key] = {**entry, **fields, "ts": int(time.time())}
            if len(data) > _MAX_ENTRIES:  # trim oldest by timestamp
                for k, _ in sorted(data.items(), key=lambda kv: kv[1].get("ts", 0))[: len(data) - _MAX_ENTRIES]:
                    data.pop(k, None)
            atomic_json_write(path, data, indent=None)  # see _LOCK
        except Exception:
            return


def record(chat_id, message_id, text: Optional[str]) -> None:
    """Persist ``text`` for ``(chat_id, message_id)``. No-op on any failure."""
    if not text or message_id is None or chat_id is None:
        return
    _update(chat_id, message_id, {"t": text[:_MAX_TEXT_CHARS]})


def record_media(chat_id, message_id, media: list[tuple[str, str]]) -> None:
    """Persist local attachment ``(path, mime)`` pairs for ``(chat_id, message_id)``."""
    if not media or message_id is None or chat_id is None:
        return
    _update(chat_id, message_id, {"m": [[str(p), str(mt or "")] for p, mt in media if p]})


async def record_async(chat_id, message_id, text: Optional[str]) -> None:
    """``record`` for coroutine callers: queue a best-effort write without holding
    adapter progress (see ``_PENDING_CONDITION``)."""
    if not text or message_id is None or chat_id is None:
        return
    try:
        _enqueue_write(_store_path(), chat_id, message_id, {"t": text[:_MAX_TEXT_CHARS]})
    except Exception:
        return


async def record_media_async(chat_id, message_id, media: list[tuple[str, str]]) -> None:
    """``record_media`` for coroutine callers; see ``record_async``."""
    if not media or message_id is None or chat_id is None:
        return
    try:
        _enqueue_write(_store_path(), chat_id, message_id,
                       {"m": [[str(p), str(mt or "")] for p, mt in media if p]})
    except Exception:
        return


def _enqueue_write(path: str, chat_id, message_id, fields: dict) -> None:
    global _WRITER_STARTED

    pending_key = (path, f"{chat_id}:{message_id}")
    # The writer thread replays the caller's context so ``_store_path`` resolves
    # the same profile home the enqueuing adapter ran under.
    ctx = contextvars.copy_context()
    with _PENDING_CONDITION:
        pending = _PENDING_WRITES.get(pending_key)
        merged = {**pending[3], **fields} if pending else dict(fields)
        _PENDING_WRITES[pending_key] = (ctx, chat_id, message_id, merged)
        _PENDING_WRITES.move_to_end(pending_key)
        _RECENT_WRITES[pending_key] = {**_RECENT_WRITES.get(pending_key, {}), **fields}
        _RECENT_WRITES.move_to_end(pending_key)
        while len(_PENDING_WRITES) > _MAX_PENDING_WRITES:
            _PENDING_WRITES.popitem(last=False)
        while len(_RECENT_WRITES) > _MAX_ENTRIES:
            _RECENT_WRITES.popitem(last=False)
        if not _WRITER_STARTED:
            threading.Thread(
                target=_writer_loop,
                name="rich-sent-store-writer",
                daemon=True,
            ).start()
            _WRITER_STARTED = True
        _PENDING_CONDITION.notify()


def _writer_loop() -> None:
    while True:
        with _PENDING_CONDITION:
            while not _PENDING_WRITES:
                _PENDING_CONDITION.wait()
            _, item = _PENDING_WRITES.popitem(last=False)
        ctx, chat_id, message_id, fields = item
        try:
            ctx.run(_update, chat_id, message_id, fields)
        except Exception:
            pass


def _entry(chat_id, message_id) -> dict:
    if message_id is None or chat_id is None:
        return {}
    path = _store_path()
    key = f"{chat_id}:{message_id}"
    with _PENDING_CONDITION:
        recent = _RECENT_WRITES.get((path, key))
    entry = _load(path).get(key)
    entry = entry if isinstance(entry, dict) else {}
    return {**entry, **recent} if recent else entry


def lookup(chat_id, message_id) -> Optional[str]:
    """Return stored text for ``(chat_id, message_id)`` or ``None``."""
    return _entry(chat_id, message_id).get("t") or None


def lookup_media(chat_id, message_id) -> list[tuple[str, str]]:
    """Return stored ``(path, mime)`` pairs whose file still exists (attachments may be temp files)."""
    pairs = _entry(chat_id, message_id).get("m") or []
    return [(p, mt) for p, mt in pairs if isinstance(p, str) and os.path.isfile(p)]
