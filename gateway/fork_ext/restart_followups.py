"""Carry user follow-ups across a gateway restart instead of dropping them (fork).

Incident 2026-09-23 03:59-04:31 (Apollo). An in-band restart deferred ``stop()``
for the full 1800 s after-turn cap. While ``_draining`` was set, every session
whose turn finished dequeued the user's queued follow-up and then hit::

    Discarding pending follow-up for session <key> during gateway restart

four times. The user had been told "queued for the next turn after it comes
back"; nothing carried the text across the restart, so four sessions went
silent until he re-sent by hand.

This module is the durable carrier: one JSON file per follow-up under
``<home>/gateway/restart_followups/``, written by the draining process and
loaded by the next boot and acknowledged only after adapter acceptance, which
feeds each record into the existing startup-restore inbound queue. A failed
boot or interrupted replay leaves unaccepted records on disk. Best-effort: a
spool failure is logged LOUDLY by the caller, never raised into the turn.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_SPOOL_RELATIVE = ("gateway", "restart_followups")
# A follow-up older than this at boot is stale: the user has long since moved
# on (or re-sent it), and replaying it would answer a question nobody is
# waiting on. Kept, not deleted, so an operator can still inspect it.
MAX_REPLAY_AGE_S = 6 * 3600.0


def spool_dir(home: Optional[Path] = None) -> Path:
    if home is None:
        from gateway.fork_ext.unclean_restart_notice import _process_home

        home = _process_home()
    return Path(home).joinpath(*_SPOOL_RELATIVE)


# MessageEvent fields deliberately NOT carried across a restart. Everything
# else on the dataclass is persisted by ``event_fields`` and restored by
# ``event_kwargs``, so a replayed event keeps its identity (Argus r6: a plugin
# injection with allow_gateway_control=False came back as a user slash
# command; internal continuations came back user-authored; photos came back
# as caption-only text).
NOT_CARRIED_FIELDS = {
    "text": "stored as the record's top-level text",
    "source": "stored as the record's top-level source",
    "raw_message": "platform SDK object; dies with the process",
    "suppress_public_echo": "bound to an open platform interaction that dies with the process",
    "deferred_reply_text": "bound to an open platform interaction that dies with the process",
}


def event_fields(event: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Serialise every carried MessageEvent field.

    Returns ``(fields, None)`` or ``(None, field_name)`` naming the first field
    that cannot be stored durably; the caller must then refuse to spool the
    event (and log it) rather than replay a different event.
    """
    import dataclasses
    from datetime import datetime

    out: Dict[str, Any] = {}
    for f in dataclasses.fields(event):
        if f.name in NOT_CARRIED_FIELDS:
            continue
        value = getattr(event, f.name, None)
        if f.name == "message_type":
            value = getattr(value, "value", value)
        elif isinstance(value, datetime):
            value = value.isoformat()
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            return None, f.name
        out[f.name] = value
    return out, None


def event_kwargs(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Inverse of ``event_fields``: MessageEvent constructor kwargs."""
    import dataclasses
    from datetime import datetime

    from gateway.platforms.base import MessageEvent, MessageType

    known = {f.name for f in dataclasses.fields(MessageEvent)} - set(NOT_CARRIED_FIELDS)
    kwargs: Dict[str, Any] = {}
    for name, value in fields.items():
        if name not in known:
            logger.warning("restart follow-up field %r is unknown to this build; dropped", name)
            continue
        if name == "message_type":
            value = MessageType(value)
        elif name == "timestamp" and isinstance(value, str):
            value = datetime.fromisoformat(value)
        kwargs[name] = value
    return kwargs


def spool_followup(
    session_key: str,
    text: str,
    source: Dict[str, Any],
    *,
    reason: str = "restart",
    home: Optional[Path] = None,
    now: Optional[float] = None,
    event: Optional[Dict[str, Any]] = None,
) -> Optional[Path]:
    """Durably record ONE follow-up. Returns the file path, or None on failure.

    ``event`` is the ``event_fields`` dict of the parked MessageEvent; without
    it the record replays as a plain user text message (version 1 shape).
    """
    if not session_key or not isinstance(text, str):
        return None
    has_media = bool(isinstance(event, dict) and event.get("media_urls"))
    if not text.strip() and not has_media:
        return None
    if not isinstance(source, dict) or not source.get("platform") or not source.get("chat_id"):
        return None
    ts = time.time() if now is None else float(now)
    record = {
        "version": 2 if event is not None else 1,
        "session_key": session_key,
        "text": text,
        "source": source,
        "reason": reason,
        "ts": ts,
        "pid": os.getpid(),
    }
    if event is not None:
        record["event"] = event
    try:
        directory = spool_dir(home)
        directory.mkdir(parents=True, exist_ok=True)
        # Time-ordered name so boot replays in arrival order (FIFO per chat).
        name = f"{int(ts * 1e6):020d}-{uuid.uuid4().hex[:8]}.json"
        final = directory / name
        tmp = directory / (name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(record, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)
        return final
    except Exception:
        logger.debug("restart follow-up spool write failed", exc_info=True)
        return None


def take_followups(
    home: Optional[Path] = None, *, now: Optional[float] = None
) -> Tuple[List[Dict[str, Any]], int]:
    """Load follow-ups (oldest first) without deleting unaccepted messages.

    Returns ``(records, stale_count)``. Each record carries an internal
    ``_spool_path`` for acknowledgement after adapter acceptance. Stale /
    malformed files are left on disk (renamed ``*.stale`` / ``*.bad``).
    """
    records: List[Dict[str, Any]] = []
    stale = 0
    try:
        directory = spool_dir(home)
        files = sorted(directory.glob("*.json"))
    except Exception:
        return records, stale
    current = time.time() if now is None else float(now)
    for path in files:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(record, dict)
                or not record.get("session_key")
                or not isinstance(record.get("text"), str)
                or not isinstance(record.get("source"), dict)
            ):
                path.rename(path.with_suffix(".bad"))
                continue
            age = current - float(record.get("ts", 0.0))
            if age > MAX_REPLAY_AGE_S:
                stale += 1
                path.rename(path.with_suffix(".stale"))
                continue
            record["_spool_path"] = str(path)
            records.append(record)
        except Exception:
            logger.debug("restart follow-up spool read failed for %s", path, exc_info=True)
    return records, stale


def acknowledge_followup(path: str) -> bool:
    """Remove a spooled record only after its adapter accepted the replay."""
    try:
        Path(path).unlink()
        return True
    except OSError:
        logger.warning("restart follow-up acknowledgement failed for %s", path, exc_info=True)
        return False
