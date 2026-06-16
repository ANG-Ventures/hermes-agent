"""Persistence for native-content-slimmer savings events (PRD #1.5).

A dedicated side table in the per-profile ``turns.db``. Native-slimmer events are
keyed by ``savings_key`` (= ``session_id|tool_call_id|raw_sha256|artifact_id``),
fire in shadow mode where no turn row exists yet, and carry dual-baseline +
mode/action + per-row pricing fields that do not map onto the rtk-oriented
``turn_tool_calls`` columns — hence a side table, not new columns there.

Design invariants (PRD #1.5):
- **Single UPSERT conflict clause** keyed on ``savings_key`` (D-1/D-9). There is NO
  ``INSERT OR IGNORE`` anywhere; an incoming ``replace`` (realized) supersedes the
  full savings/byte/token/model columns of a prior ``would_replace`` row, while
  ``created_at`` is preserved as FIRST-SEEN so a late flip never re-buckets the day.
- **Additive, guarded migration** (``CREATE TABLE IF NOT EXISTS``) mirroring
  ``blackbox/store.py::_ensure_schema``; a second run is a no-op; an old DB without
  the table gains it cleanly.
- **Fail-open**: this module never raises into the agent loop; the sink wrapper
  (``BlackboxNativeSlimmerSink``) decides propagation per the certified
  rollback-on-emit-failure contract.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from hermes_constants import get_hermes_home
from plugins.blackbox.native_slimmer_schema import ensure_strategy_columns

TABLE = "native_slimmer_savings"

# Columns superseded on an ON CONFLICT … DO UPDATE when the incoming row is a
# realized ``replace`` (D-9). created_at is intentionally excluded (first-seen).
_SUPERSEDE_COLS = (
    "action",
    "mode",
    "model",
    "provider",
    "base_url",
    "tool_name",
    "raw_source",
    "turn_id",
    "original_bytes",
    "emitted_bytes",
    "status_quo_bytes",
    "saved_vs_raw_bytes",
    "saved_vs_status_quo_bytes",
    "saved_vs_raw_tokens_est",
    "saved_vs_status_quo_tokens_est",
    "tokenizer_label",
    "token_estimate_kind",
    "lossy",
    "classification_reason",
    "strategy",
    "view_bytes",
    "lossy_view",
    "expansions_triggered",
)

# Full insert column order (savings_key + supersedable + created_at).
_INSERT_COLS = ("savings_key",) + _SUPERSEDE_COLS + ("created_at",)


def _db_path() -> Path:
    return get_hermes_home() / "blackbox" / "turns.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the savings side table if absent (additive, idempotent).

    Mirrors ``blackbox/store.py::_ensure_schema``: ``CREATE TABLE IF NOT EXISTS``
    so a second call is a no-op and an old DB gains the table without touching
    existing rows. UNIQUE on ``savings_key`` is the storage-layer dedupe.
    """

    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            savings_key TEXT PRIMARY KEY,
            action TEXT,
            mode TEXT,
            model TEXT,
            provider TEXT,
            base_url TEXT,
            tool_name TEXT,
            raw_source TEXT,
            session_id TEXT,
            tool_call_id TEXT,
            raw_sha256 TEXT,
            artifact_id TEXT,
            turn_id TEXT,
            original_bytes INT,
            emitted_bytes INT,
            status_quo_bytes INT,
            saved_vs_raw_bytes INT,
            saved_vs_status_quo_bytes INT,
            saved_vs_raw_tokens_est INT,
            saved_vs_status_quo_tokens_est INT,
            tokenizer_label TEXT,
            token_estimate_kind TEXT,
            lossy INT,
            classification_reason TEXT,
            strategy TEXT,
            view_bytes INT,
            lossy_view INT,
            expansions_triggered INT,
            created_at REAL
        );

        CREATE INDEX IF NOT EXISTS idx_native_savings_created
            ON {TABLE}(created_at);
        CREATE INDEX IF NOT EXISTS idx_native_savings_action
            ON {TABLE}(action, created_at);
        """
    )
    ensure_strategy_columns(conn, table=TABLE)
    conn.commit()


def _to_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def has_table(conn: sqlite3.Connection) -> bool:
    """Schema-parity guard (PRD #1 §7): is the table present on this DB?"""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone()
    return row is not None


def insert_event(
    event: Mapping[str, Any],
    *,
    model: str | None = None,
    provider: str | None = None,
    base_url: str | None = None,
    created_at: float,
    conn: sqlite3.Connection | None = None,
) -> None:
    """UPSERT one native-slimmer savings event (D-1/D-9 single conflict clause).

    ``model``/``provider``/``base_url`` are resolved by the sink at write time
    (they are not in the hook payload — Phase 0 (g)); pass-through here so the
    digest can dollarize per-row at the model that carried the tokens.

    On conflict, only a realized ``replace`` supersedes the prior row's savings/
    model columns; ``created_at`` is preserved (first-seen).
    """

    own = conn is None
    if own:
        conn = _connect()
    try:
        savings_key = str(event.get("savings_key") or "")
        if not savings_key:
            savings_key = "|".join(
                str(event.get(part) or "")
                for part in ("session_id", "tool_call_id", "raw_sha256", "artifact_id")
            )
        row = {
            "savings_key": savings_key,
            "action": str(event.get("action") or ""),
            "mode": str(event.get("mode") or ""),
            "model": str(model) if model else None,
            "provider": str(provider) if provider else None,
            "base_url": str(base_url) if base_url else None,
            "tool_name": str(event.get("tool_name") or ""),
            "raw_source": str(event.get("raw_source") or ""),
            "session_id": str(event.get("session_id") or ""),
            "tool_call_id": str(event.get("tool_call_id") or ""),
            "raw_sha256": str(event.get("raw_sha256") or ""),
            "artifact_id": str(event.get("artifact_id") or ""),
            "turn_id": str(event.get("turn_id") or "") or None,
            "original_bytes": _to_int(event.get("original_bytes")),
            "emitted_bytes": _to_int(event.get("emitted_bytes")),
            "status_quo_bytes": _to_int(event.get("status_quo_bytes")),
            "saved_vs_raw_bytes": _to_int(event.get("saved_vs_raw_bytes")),
            "saved_vs_status_quo_bytes": _to_int(event.get("saved_vs_status_quo_bytes")),
            "saved_vs_raw_tokens_est": _to_int(event.get("saved_vs_raw_tokens_est")),
            "saved_vs_status_quo_tokens_est": _to_int(event.get("saved_vs_status_quo_tokens_est")),
            "tokenizer_label": str(event.get("tokenizer_label") or ""),
            "token_estimate_kind": str(event.get("token_estimate_kind") or ""),
            "lossy": 1 if event.get("lossy") else 0,
            "classification_reason": str(event.get("classification_reason") or ""),
            "strategy": str(event.get("strategy") or "") or None,
            "view_bytes": _to_int(event.get("view_bytes")) if event.get("view_bytes") is not None else None,
            "lossy_view": None if event.get("lossy_view") is None else (1 if event.get("lossy_view") else 0),
            "expansions_triggered": None if event.get("expansions_triggered") is None else _to_int(event.get("expansions_triggered")),
            "created_at": float(created_at),
        }
        # The non-supersede identity columns (session/tool_call/sha/artifact) also
        # get inserted; include them in the INSERT but never in the UPDATE SET.
        insert_cols = list(_INSERT_COLS) + [
            "session_id",
            "tool_call_id",
            "raw_sha256",
            "artifact_id",
        ]
        placeholders = ", ".join("?" for _ in insert_cols)
        set_clause = ", ".join(f"{col}=excluded.{col}" for col in _SUPERSEDE_COLS)
        sql = (
            f"INSERT INTO {TABLE} ({', '.join(insert_cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(savings_key) DO UPDATE SET {set_clause} "
            f"WHERE excluded.action='replace'"
        )
        conn.execute(sql, [row[col] for col in insert_cols])
        conn.commit()
    finally:
        if own:
            conn.close()


def prune_older_than(epoch_seconds: float, *, conn: sqlite3.Connection | None = None) -> int:
    """Delete savings rows with ``created_at`` older than the cutoff (D-8 TTL).

    Best-effort: returns the row count deleted. Caller (the slimmer GC path)
    swallows errors so a prune failure never breaks a write.
    """

    own = conn is None
    if own:
        conn = _connect()
    try:
        cur = conn.execute(f"DELETE FROM {TABLE} WHERE created_at < ?", (float(epoch_seconds),))
        conn.commit()
        return cur.rowcount if cur.rowcount is not None else 0
    finally:
        if own:
            conn.close()


def fetch_between(
    start_epoch: float,
    end_epoch: float,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Read savings rows in [start, end) for the digest. Empty if table absent."""

    own = conn is None
    if own:
        conn = _connect()
    try:
        if not has_table(conn):
            return []
        rows = conn.execute(
            f"SELECT * FROM {TABLE} WHERE created_at >= ? AND created_at < ? ORDER BY created_at",
            (float(start_epoch), float(end_epoch)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def record_expansion(
    *,
    session_id: str,
    tool_call_id: str,
    raw_sha256: str,
    artifact_id: str,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Best-effort realized-expansion accounting for expand_artifact calls."""

    own = conn is None
    if own:
        conn = _connect()
    try:
        savings_key = "|".join(
            str(part or "") for part in (session_id, tool_call_id, raw_sha256, artifact_id)
        )
        conn.execute(
            f"UPDATE {TABLE} SET expansions_triggered = COALESCE(expansions_triggered, 0) + 1 "
            "WHERE savings_key = ?",
            (savings_key,),
        )
        conn.commit()
    finally:
        if own:
            conn.close()


def count_rows(conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    if own:
        conn = _connect()
    try:
        if not has_table(conn):
            return 0
        return int(conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0])
    finally:
        if own:
            conn.close()
