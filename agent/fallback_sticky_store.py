"""Session-scoped sticky-fallback store (same-family fallback spec §4.2).

There is no pre-existing session-scoped runtime store: today's cooldown is
``agent._rate_limited_until``, a monotonic attribute on the cached agent that
is lost whenever the agent is evicted or rebuilt. This module is the store the
spec asks for:

* a lock-guarded map keyed by ``(lineage root, primary provider, primary
  model)``, LRU-bounded at 5,000 entries;
* write-through to a ``fallback_sticky`` table in the blackbox ``turns.db``
  (best-effort, I3: a failed write never raises into a turn);
* a single read accessor, :func:`get` / :meth:`StickyStore.get` — map first,
  then the table on a map miss, so a rebuilt agent after a gateway restart
  still sees its state. A DB read failure on a miss raises
  :class:`StoreUnreadable` so the caller can start on the primary, log and
  count ``store_unreadable`` (§4.2, pass-10 RC-4).

Times are wall-clock epoch seconds, never ``time.monotonic()``, so ``until``
survives an agent rebuild and a process restart.

The session id that feeds the key comes from the live ``agent.session_id``
(see :func:`lineage_root_for_agent`), never a contextvar or ``os.environ``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

logger = logging.getLogger(__name__)

MAX_ENTRIES = 5000
AUTH_MARK_TTL_S = 24 * 3600


class StoreUnreadable(RuntimeError):
    """Map miss and the ``fallback_sticky`` table could not be read."""


class StickyKey(NamedTuple):
    lineage_root: str
    primary_provider: str
    primary_model: str

    @classmethod
    def build(cls, lineage_root: str, provider: str, model: str) -> "StickyKey":
        return cls(
            str(lineage_root or ""),
            str(provider or "").strip().lower(),
            str(model or "").strip(),
        )


@dataclasses.dataclass
class StickyState:
    """``_sticky`` (§4.2) plus the §4.3 primary-side inputs."""

    primary_provider: str = ""
    primary_model: str = ""
    fallback_provider: str = ""
    fallback_model: str = ""
    fallback_index: Optional[int] = None
    cls: str = ""
    until_epoch: float = 0.0
    n: int = 0
    active: bool = False
    returned_at: Optional[float] = None
    return_branch: Optional[str] = None
    entered_at: Optional[float] = None
    last_fallback_call_epoch: Optional[float] = None
    last_fallback_session_id: Optional[str] = None
    turns_on_fallback: int = 0
    ff_disabled_until: float = 0.0
    last_primary_call_epoch: Optional[float] = None
    last_primary_seat: Optional[str] = None
    last_failure_epoch: Optional[float] = None

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "StickyState":
        data = json.loads(raw)
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def default_db_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "blackbox" / "turns.db"


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS fallback_sticky ("
    " lineage_root TEXT NOT NULL, primary_provider TEXT NOT NULL,"
    " primary_model TEXT NOT NULL, state_json TEXT NOT NULL,"
    " until_epoch REAL, cls TEXT, updated_at REAL,"
    " PRIMARY KEY (lineage_root, primary_provider, primary_model))",
    "CREATE TABLE IF NOT EXISTS fallback_auth_marks ("
    " token_fp TEXT PRIMARY KEY, marked_at REAL NOT NULL)",
)


class StickyStore:
    """Lock-guarded LRU map with best-effort sqlite write-through."""

    def __init__(self, db_path: Optional[Path] = None,
                 max_entries: int = MAX_ENTRIES) -> None:
        self._db_path = Path(db_path) if db_path is not None else None
        self._max = int(max_entries)
        self._lock = threading.Lock()
        self._map: "OrderedDict[StickyKey, StickyState]" = OrderedDict()
        self._auth: Dict[str, float] = {}
        self.store_unreadable_count = 0

    # ── sqlite ──────────────────────────────────────────────────────────
    def _path(self) -> Path:
        return self._db_path if self._db_path is not None else default_db_path()

    def _connect(self) -> sqlite3.Connection:
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=2.0)
        for stmt in _SCHEMA:
            conn.execute(stmt)
        return conn

    def _write_through(self, key: StickyKey, state: StickyState, now: float) -> None:
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO fallback_sticky (lineage_root,"
                    " primary_provider, primary_model, state_json, until_epoch,"
                    " cls, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (key.lineage_root, key.primary_provider, key.primary_model,
                     state.to_json(), state.until_epoch, state.cls, now),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — I3
            logger.warning("fallback_sticky write-through failed (best-effort)",
                           exc_info=True)

    def _read_db(self, key: StickyKey) -> Optional[StickyState]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT state_json FROM fallback_sticky WHERE lineage_root=?"
                " AND primary_provider=? AND primary_model=?",
                (key.lineage_root, key.primary_provider, key.primary_model),
            ).fetchone()
        finally:
            conn.close()
        return StickyState.from_json(row[0]) if row else None

    # ── map ─────────────────────────────────────────────────────────────
    def _remember(self, key: StickyKey, state: StickyState) -> None:
        self._map[key] = state
        self._map.move_to_end(key)
        while len(self._map) > self._max:
            self._map.popitem(last=False)

    def get(self, key: StickyKey) -> Optional[StickyState]:
        """THE read accessor (§4.2 pass-9 RC-5). Returns a copy.

        Raises :class:`StoreUnreadable` when the map misses and the table
        cannot be read.
        """
        with self._lock:
            state = self._map.get(key)
            if state is not None:
                self._map.move_to_end(key)
                return dataclasses.replace(state)
        try:
            state = self._read_db(key)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.store_unreadable_count += 1
            logger.warning("fallback_sticky store unreadable for lineage root %s: %s",
                           key.lineage_root, exc)
            raise StoreUnreadable(str(exc)) from exc
        if state is None:
            return None
        with self._lock:
            self._remember(key, state)
        return dataclasses.replace(state)

    def put(self, key: StickyKey, state: StickyState, now: float) -> None:
        with self._lock:
            self._remember(key, dataclasses.replace(state))
        self._write_through(key, state, now)

    def evict_memory(self) -> None:
        """Drop the in-process map (simulates a gateway restart in tests)."""
        with self._lock:
            self._map.clear()
            self._auth.clear()

    def purge(self) -> List[Tuple[str, str, str, str, float]]:
        """Rollback purge (§4.2): log every row, then delete this profile's table."""
        purged: List[Tuple[str, str, str, str, float]] = []
        with self._lock:
            self._map.clear()
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT lineage_root, primary_provider, primary_model, cls,"
                    " until_epoch FROM fallback_sticky").fetchall()
                for r in rows:
                    logger.info("fallback_sticky purge: key=%s/%s/%s class=%s until=%s",
                                r[0], r[1], r[2], r[3], r[4])
                    purged.append(tuple(r))
                conn.execute("DELETE FROM fallback_sticky")
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            logger.warning("fallback_sticky purge failed", exc_info=True)
        return purged

    # ── auth marks (per token fingerprint, §4.1 auth row) ──────────────
    def mark_auth(self, token_fp: str, now: float) -> None:
        with self._lock:
            self._auth[token_fp] = now
        try:
            conn = self._connect()
            try:
                conn.execute("INSERT OR REPLACE INTO fallback_auth_marks VALUES (?,?)",
                             (token_fp, now))
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            logger.warning("fallback auth mark write failed (best-effort)", exc_info=True)

    def auth_marked(self, token_fp: str, now: float) -> bool:
        if not token_fp:
            return False
        with self._lock:
            at = self._auth.get(token_fp)
        if at is None:
            try:
                conn = self._connect()
                try:
                    row = conn.execute(
                        "SELECT marked_at FROM fallback_auth_marks WHERE token_fp=?",
                        (token_fp,)).fetchone()
                finally:
                    conn.close()
                at = float(row[0]) if row else None
            except Exception:  # noqa: BLE001
                at = None
        return at is not None and now - at < AUTH_MARK_TTL_S


_DEFAULT: Optional[StickyStore] = None
_DEFAULT_LOCK = threading.Lock()


def default_store() -> StickyStore:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = StickyStore()
        return _DEFAULT


def get(key: StickyKey, store: Optional[StickyStore] = None) -> Optional[StickyState]:
    """Module-level accessor; the only sanctioned read of sticky state."""
    return (store or default_store()).get(key)


def lineage_root_for_agent(agent: Any) -> str:
    """Compression-lineage root of the LIVE ``agent.session_id``.

    Falls back to the physical id when no session DB is attached or the walk
    fails. Never reads the session-id contextvar or ``os.environ``.
    """
    sid = str(getattr(agent, "session_id", None) or "")
    if not sid:
        return ""
    try:
        from agent.prompt_cache_scope import _lineage_root

        return _lineage_root(sid, getattr(agent, "_session_db", None)) or sid
    except Exception:  # noqa: BLE001
        return sid

