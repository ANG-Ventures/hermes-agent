"""Persisted auto-handle state (Boomerang v2 P3a, spec §5.4 "Retry state", D-13).

``<HERMES_HOME>/gateway/kanban_auto_handle.json`` holds ``retries[task_id]``: how many
automatic retries a crash-class wake has already spent on a card. ``wake_disposition``
reads it (one retry, then surface); the dispatcher (P3b) bumps it when it dispatches a
crash retry. It survives a gateway restart, and it resets when the card reaches
``completed``/``archived`` (or when Ace surfaces-and-acts on it: :meth:`reset`).

Writes go through write-temp + ``os.replace`` under a process-wide lock: both wake paths
run on threads of the ONE gateway process that owns this ``HERMES_HOME`` (spec §5.4,
single-gateway assumption). Unknown top-level keys are preserved so later phases can add
to the file without a migration.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

STATE_FILENAME = "kanban_auto_handle.json"
# Events after which a card's retry budget starts over.
RESET_KINDS = frozenset({"completed", "archived"})

_LOCK = threading.Lock()


def state_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "gateway" / STATE_FILENAME


class AutoHandleState:
    """Retry counters keyed by task id, persisted on every change."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else state_path()

    # --- file I/O (caller holds _LOCK) ---------------------------------------

    def _read(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".auto-handle-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _retries_of(data: dict) -> dict:
        raw = data.get("retries")
        if not isinstance(raw, dict):
            return {}
        out = {}
        for k, v in raw.items():
            try:
                out[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
        return out

    # --- public API ------------------------------------------------------------

    def retries(self, task_id: str) -> int:
        with _LOCK:
            return self._retries_of(self._read()).get(str(task_id), 0)

    def bump_retry(self, task_id: str) -> int:
        """Record one more automatic retry for ``task_id``; returns the new count."""
        with _LOCK:
            data = self._read()
            retries = self._retries_of(data)
            retries[str(task_id)] = retries.get(str(task_id), 0) + 1
            data["retries"] = retries
            self._write(data)
            return retries[str(task_id)]

    def reset(self, task_id: str) -> bool:
        """Drop ``task_id``'s counter; True when one existed."""
        with _LOCK:
            data = self._read()
            retries = self._retries_of(data)
            if str(task_id) not in retries:
                return False
            del retries[str(task_id)]
            data["retries"] = retries
            self._write(data)
            return True

    def observe_event(self, task_id: str, kind: str) -> bool:
        """Reset the counter when ``kind`` ends the card's retry budget (completed/archived)."""
        if str(kind or "") in RESET_KINDS:
            return self.reset(task_id)
        return False


# --- session-level /autohandle (D-15) -------------------------------------------

#: ``SessionEntry.metadata`` key the ``/autohandle`` command writes (True / False; absent =
#: unset). Read at wake time so cards homed in the session follow a later toggle.
SESSION_METADATA_KEY = "auto_handle"


def session_setting(session_store, session_id: str):
    """The home session's ``/autohandle`` value (True / False / None), looked up by the card's
    ``tasks.session_id`` in the gateway's session store. None when the session is not live
    here or never set it."""
    if not session_id or session_store is None:
        return None
    try:
        entry = session_store.lookup_by_session_id(session_id)
    except Exception as exc:
        logger.debug("kanban auto-handle: session lookup %s failed: %s", session_id, exc)
        return None
    if entry is None:
        return None
    value = (getattr(entry, "metadata", None) or {}).get(SESSION_METADATA_KEY)
    return value if isinstance(value, bool) else None


def board_default(board: Optional[str] = None):
    """The board-level default (``board.json`` ``auto_handle``: true/false), None when unset."""
    try:
        from hermes_cli import kanban_db as _kb

        value = _kb.read_board_metadata(board).get("auto_handle")
    except Exception:
        return None
    return value if isinstance(value, bool) else None
