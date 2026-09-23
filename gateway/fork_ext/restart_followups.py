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


def spool_followup(
    session_key: str,
    text: str,
    source: Dict[str, Any],
    *,
    reason: str = "restart",
    home: Optional[Path] = None,
    now: Optional[float] = None,
) -> Optional[Path]:
    """Durably record ONE follow-up. Returns the file path, or None on failure."""
    if not session_key or not isinstance(text, str) or not text.strip():
        return None
    if not isinstance(source, dict) or not source.get("platform") or not source.get("chat_id"):
        return None
    ts = time.time() if now is None else float(now)
    record = {
        "version": 1,
        "session_key": session_key,
        "text": text,
        "source": source,
        "reason": reason,
        "ts": ts,
        "pid": os.getpid(),
    }
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
