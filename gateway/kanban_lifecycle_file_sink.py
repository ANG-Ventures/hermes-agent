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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

FILE_PLATFORM = "file"
FAMILY = "digest:kanban-lifecycle"


def resolve_path(chat_id: str) -> Path:
    p = Path(os.path.expanduser(str(chat_id).strip()))
    if not p.is_absolute():
        from hermes_constants import get_hermes_home

        p = get_hermes_home() / p
    return p


class LifecycleFileSink:
    """Adapter-shaped appender. ``send`` returning None = delivered (the notifier's contract)."""

    async def send(self, chat_id: str, text: str, metadata: Optional[dict] = None, **_: Any) -> None:
        # An OSError raises into the caller: the digest keeps the batch, a direct send
        # counts as a failed delivery, exactly like a failed chat send.
        path = resolve_path(chat_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "family": FAMILY, "text": text}
        data = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
        # One write(2) on an O_APPEND fd: gateways in other processes may append to the same
        # file, and a buffered text write can split a record and interleave it with theirs.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            n = os.write(fd, data)
        finally:
            os.close(fd)
        if n != len(data):
            raise OSError(f"short append to {path}: {n}/{len(data)} bytes")
