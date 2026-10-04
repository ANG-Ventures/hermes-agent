"""Outbox for route-change notices the platform adapter failed to deliver.

A fallback hop the user never saw is the worst outcome of the announce path
(t_b2e9bb23: a host network drop failed over K3 -> Opus, the ``🔄 Model
fallback`` line hit ``adapter_send_failed``, and the session ran on the
fallback for 90 min with no visible notice). A failed route-change send is
queued here per chat and flushed on the next turn in that chat, which is the
first moment the platform is known to be reachable again (an inbound message
just arrived).

* Dedupe by notice text per chat: the same transition renders the same line,
  matching the per-transition announce dedupe upstream.
* Bounded: at most ``MAX_PER_CHAT`` notices per chat, oldest dropped.
* Persisted to ``<HERMES_HOME>/gateway_route_notice_outbox.json`` so a
  gateway restart between the drop and the next turn does not lose it.
* Best-effort throughout: an outbox failure never raises into a turn.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_PER_CHAT = 5
MAX_AGE_S = 24 * 3600.0
FILE_NAME = "gateway_route_notice_outbox.json"


def chat_key(platform: Any, chat_id: Any, metadata: Optional[Dict[str, Any]] = None) -> str:
    plat = getattr(platform, "value", platform)
    thread = ""
    if isinstance(metadata, dict):
        thread = str(metadata.get("thread_id") or "")
    return f"{plat or ''}|{chat_id or ''}|{thread}"


def _json_safe(metadata: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(metadata, dict):
        return None
    try:
        return json.loads(json.dumps(metadata))
    except (TypeError, ValueError):
        return None


def delayed_text(message: str, dropped_at: float) -> str:
    stamp = time.strftime("%H:%M %Z", time.localtime(dropped_at)).strip()
    return f"{message} (delayed: undelivered at {stamp})"


class RouteNoticeOutbox:
    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._data: Optional[Dict[str, List[Dict[str, Any]]]] = None

    def _file(self) -> Path:
        if self._path is not None:
            return self._path
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()) / FILE_NAME

    def _load(self) -> Dict[str, List[Dict[str, Any]]]:
        # No in-memory cache: the file is the state and is re-read on every
        # call, so the outbox follows the live home (profiles, test homes).
        data: Dict[str, List[Dict[str, Any]]] = {}
        try:
            raw = json.loads(self._file().read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                data = {str(k): [e for e in v if isinstance(e, dict)]
                        for k, v in raw.items() if isinstance(v, list)}
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001
            logger.warning("route notice outbox unreadable; starting empty", exc_info=True)
        self._data = data
        return data

    def _save(self) -> None:
        try:
            path = self._file()
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {k: v for k, v in (self._data or {}).items() if v}
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".route_outbox.")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, path)
        except Exception:  # noqa: BLE001
            logger.warning("route notice outbox write failed (best-effort)", exc_info=True)

    def enqueue(self, key: str, message: str, metadata: Optional[Dict[str, Any]] = None,
                *, event_type: str = "info", now: Optional[float] = None) -> bool:
        """Queue one undelivered notice. Returns False for a duplicate."""
        if not key or not message:
            return False
        now = time.time() if now is None else now
        with self._lock:
            data = self._load()
            items = data.setdefault(key, [])
            if any(e.get("message") == message for e in items):
                return False
            items.append({"message": str(message), "event_type": str(event_type),
                          "metadata": _json_safe(metadata), "dropped_at": now})
            del items[:-MAX_PER_CHAT]
            self._save()
        logger.info("route notice queued for redelivery: chat=%s pending=%d", key, len(items))
        return True

    def take(self, key: str, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Pop every pending notice for ``key`` (expired ones are dropped)."""
        now = time.time() if now is None else now
        with self._lock:
            data = self._load()
            items = data.pop(key, None) or []
            if items:
                self._save()
        return [e for e in items if now - float(e.get("dropped_at") or 0) <= MAX_AGE_S]

    def pending(self, key: str) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._load().get(key) or [])

    def restore(self, key: str, entry: Dict[str, Any]) -> None:
        """Put back an entry whose redelivery failed (keeps its drop time)."""
        with self._lock:
            items = self._load().setdefault(key, [])
            if not any(e.get("message") == entry.get("message") for e in items):
                items.insert(0, entry)
                del items[:-MAX_PER_CHAT]
            self._save()


_DEFAULT: Optional[RouteNoticeOutbox] = None
_DEFAULT_LOCK = threading.Lock()


def default_outbox() -> RouteNoticeOutbox:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = RouteNoticeOutbox()
        return _DEFAULT
