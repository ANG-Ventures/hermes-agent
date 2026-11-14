"""``kanban.lifecycle_channel: "file:<path>"``: lifecycle lines go to a local file, not a chat.

t_89bfcd1f (Ace 2026-11-14): the "kanban lifecycle" digest in Discord #logs is agent
bookkeeping with no human action, and only human-relevant items belong on Discord. With a
``file:`` channel every routed line / digest is appended to ``<path>`` as ONE JSON line
``{"ts", "family", "text"}`` (``text`` = exactly what the chat post would have carried). A
relative path resolves under HERMES_HOME (``file:state/kanban-lifecycle-digest.jsonl``).

The sink stands in for a platform adapter: ``_kanban_route_adapter("file", ...)`` returns it,
so routing, digest batching, dedupe, fallback and the subscriber's wake are unchanged.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

FILE_PLATFORM = "file"
FAMILY = "digest:kanban-lifecycle"
_LOCK = threading.Lock()


def resolve_path(chat_id: str) -> Path:
    p = Path(os.path.expanduser(str(chat_id).strip()))
    if not p.is_absolute():
        from hermes_constants import get_hermes_home

        p = get_hermes_home() / p
    return p


class LifecycleFileSink:
    """Adapter-shaped appender. ``send`` returning None = delivered (the notifier's contract)."""

    def _append(self, chat_id: str, text: str) -> None:
        path = resolve_path(chat_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "family": FAMILY, "text": text}
        with _LOCK, open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    async def send(self, chat_id: str, text: str, metadata: Optional[dict] = None, **_: Any) -> None:
        # An OSError raises into the caller: the digest keeps the batch, a direct send
        # counts as a failed delivery, exactly like a failed chat send.
        self._append(chat_id, text)
        return None


_SINK = LifecycleFileSink()


def sink() -> LifecycleFileSink:
    return _SINK
