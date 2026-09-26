"""SQLite persistence for blackbox per-turn telemetry."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from agent.redact import redact_sensitive_text
from agent.usage_pricing import CanonicalUsage, get_pricing_entry, resolve_billing_route
from hermes_constants import get_hermes_home
from plugins.blackbox.record import TurnRecord, tools_summary

logger = logging.getLogger(__name__)


def _db_path() -> Path:
    return get_hermes_home() / "blackbox" / "turns.db"


def scrub_and_truncate(text: Any, n: int = 2000) -> str:
    """Redact secrets before truncating persisted text previews."""
    if text is None:
        return ""
    scrubbed = redact_sensitive_text(str(text), force=True)
    scrubbed = scrubbed[:n]
    # Lone UTF-16 surrogates (relay-split emoji halves that reached the
    # transcript) make sqlite3's UTF-8 encode raise inside conn.execute(),
    # and insert_turn's fail-open catch then silently DROPS the whole turn
    # record. Splice pairs / floor orphans so telemetry always persists.
    try:
        scrubbed.encode("utf-8")
    except UnicodeEncodeError:
        from agent.message_sanitization import _splice_surrogates

        scrubbed = _splice_surrogates(scrubbed)
    return scrubbed


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS turns (
            turn_id TEXT PRIMARY KEY,
            parent_turn_id TEXT,
            is_subagent INT,
            depth INT,
            ts_start REAL,
            ts_end REAL,
            profile TEXT,
            provider TEXT,
            model TEXT,
            platform TEXT,
            chat_id TEXT,
            chat_name TEXT,
            api_calls INT,
            tools TEXT,
            input_tokens INT,
            output_tokens INT,
            output_tokens_unknown INT DEFAULT 0,
            cache_read INT,
            cache_write INT,
            cache_write_5m INT,
            cache_write_1h INT,
            gap_prev_turn_s REAL,
            first_call_cache_miss INT,
            idle_compaction_fired INT,
            compaction_tokens_before INT,
            compaction_tokens_after INT,
            compaction_cost_usd REAL,
            reasoning INT,
            context_used INT,
            context_length INT,
            last_cache_read INT,
            last_cache_write INT,
            last_uncached INT,
            last_call_prompt_unknown INT DEFAULT 0,
            comp_sys_tokens INT,
            comp_tool_schema_tokens INT,
            comp_history_tokens INT,
            comp_history_message_count INT,
            comp_tool_result_tokens INT,
            comp_tool_arg_tokens INT,
            comp_tool_result_count INT,
            comp_skills_tokens INT,
            comp_skills_count INT,
            comp_framing_tokens INT,
            comp_calls_json TEXT,
            cost_usd REAL,
            cost_status TEXT,
            cost_uncached_usd REAL,
            cost_cache_read_usd REAL,
            cost_cache_write_usd REAL,
            cost_output_usd REAL,
            interrupted INT,
            alerted INT DEFAULT 0,
            user_text TEXT,
            final_text TEXT,
            cli_invocation_id TEXT,
            served_subs_json TEXT,
            attribution TEXT,
            terminal_error TEXT
        );

        CREATE TABLE IF NOT EXISTS turn_tool_calls (
            turn_id TEXT,
            seq INT,
            name TEXT,
            args_preview TEXT,
            result_preview TEXT,
            PRIMARY KEY(turn_id, seq)
        );

        CREATE TABLE IF NOT EXISTS last_turn (
            platform TEXT,
            chat_id TEXT,
            turn_id TEXT,
            PRIMARY KEY(platform, chat_id)
        );

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        -- New-model pricing sentinel ledger (card t_2e382a4b). One row per
        -- (model, provider) that recorded an unpriced turn while absent from
        -- the pricing snapshot. The PRIMARY KEY *is* the dedup: the sentinel
        -- uses INSERT OR IGNORE and treats rowcount == 1 as "first sighting,
        -- alert now", so the alert fires exactly once per model no matter how
        -- many unpriced turns follow.
        CREATE TABLE IF NOT EXISTS seen_unpriced_models (
            model TEXT,
            provider TEXT,
            first_seen TEXT,
            alerted_at TEXT,
            PRIMARY KEY(model, provider)
        );

        -- Per-API-call attribution ledger (PRD-subs-ace §5.2, card C1). One row
        -- per upstream completion call inside a turn; `turns` keeps the totals.
        -- Column set here IS the contract insert_api_call binds against (I3:
        -- guarded-additive — this is a NEW table, no existing consumer sees it).
        -- No FK pragma is enabled on the live store, so retention is the
        -- explicit parent-turn cascade in sweep(), not ON DELETE CASCADE.
        CREATE TABLE IF NOT EXISTS turn_api_calls (
            turn_id TEXT NOT NULL,
            seq INT NOT NULL,
            ts REAL,
            provider TEXT,
            sub_key TEXT,
            model TEXT,
            input_tokens INT,
            output_tokens INT,
            cache_read INT,
            cache_write INT,
            cache_write_5m INT,
            cache_write_1h INT,
            cache_ttl_requested TEXT,
            lane_family TEXT,
            reasoning INT,
            attribution TEXT,
            http_status INT,
            relay_synthetic INT NOT NULL DEFAULT 0,
            route_id TEXT,
            PRIMARY KEY(turn_id, seq)
        );

        -- Rolling-window reads (hermes_cli/kanban_budget.py's per-tick spend
        -- sum, daily-journal, /tokens) all filter on a ts_start/ts_end lower
        -- bound. Without this they SCAN the whole table, and `turns` rows are
        -- overflow-heavy (user_text/final_text previews): the 836 MB fleet
        -- ledger stores ~5k rows across ~20k overflow pages, so a "5k-row
        -- scan" is really an 80 MB read. Measured cold (macOS `purge` between
        -- trials, 10 real fleet ledgers, 24h window): 6.13 s SCAN -> 1.40 s
        -- SEARCH, identical 653 rows.
        -- (turns indexes are created AFTER the additive column migration below,
        -- guarded on the indexed columns existing -- see _ensure_turn_indexes.)
        CREATE INDEX IF NOT EXISTS idx_blackbox_api_calls_ts
            ON turn_api_calls(ts);
        CREATE INDEX IF NOT EXISTS idx_blackbox_api_calls_sub
            ON turn_api_calls(sub_key);

        -- Conversation prefix-stability guard (card t_c07124ab). One row per
        -- session holding the fingerprint of the LAST request sent on it
        -- (hashes + byte sizes only, never text); the next request of the
        -- same session is compared against it and then replaces it. Keyed on
        -- the session so the check survives agent-cache eviction and gateway
        -- restarts (a cross-process comparison is tagged, see `context`).
        -- `alerted_at` is the once-per-session page stamp (transition state).
        CREATE TABLE IF NOT EXISTS prefix_sessions (
            session_key TEXT PRIMARY KEY,
            turn_id TEXT,
            seq INT,
            ts REAL,
            pid INT,
            api_mode TEXT,
            model TEXT,
            cache_read INT,
            fingerprint_json TEXT,
            updated_at REAL,
            alerted_at REAL
        );

        -- One row per violated segment per request pair. `kind` is
        -- 'mutation' (a historical index changed in place — the class that
        -- collapses every cache read to the static prefix) or 'shrink'
        -- (history got shorter). `context` is NULL for an unexplained
        -- mutation; a tagged one carries why it is expected
        -- ('compaction:<trigger>', 'process_restart', 'api_mode_change',
        -- 'model_change') and is excluded from alerting, not allowlisted.
        -- `alerted` = 1 on the one row per session that paged #alerts.
        CREATE TABLE IF NOT EXISTS prefix_mutations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            session_key TEXT,
            turn_id TEXT,
            seq INT,
            prev_turn_id TEXT,
            prev_seq INT,
            prev_ts REAL,
            provider TEXT,
            lane_family TEXT,
            model TEXT,
            segment TEXT,
            kind TEXT,
            first_divergent_index INT,
            bytes_before INT,
            bytes_after INT,
            messages_before INT,
            messages_after INT,
            context TEXT,
            allowlisted INT NOT NULL DEFAULT 0,
            allowlist_reason TEXT,
            cache_read_before INT,
            cache_read_after INT,
            alerted INT NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_blackbox_prefix_mutations_ts
            ON prefix_mutations(ts);
        CREATE INDEX IF NOT EXISTS idx_blackbox_prefix_mutations_session
            ON prefix_mutations(session_key);
        """
    )
    # Additive migration for DBs created before the last-call cache split
    # columns existed. CREATE TABLE IF NOT EXISTS won't add columns to an
    # existing table, so ALTER each missing one. Guarded per-column so a
    # partially-migrated DB (or a second writer that already added them)
    # never raises "duplicate column name".
    _existing = {row[1] for row in conn.execute("PRAGMA table_info(turns)").fetchall()}
    for col, kind in (
        ("cache_write_5m", "INT"), ("cache_write_1h", "INT"),
        ("gap_prev_turn_s", "REAL"), ("first_call_cache_miss", "INT"),
        ("idle_compaction_fired", "INT"), ("compaction_tokens_before", "INT"),
        ("compaction_tokens_after", "INT"), ("compaction_cost_usd", "REAL"),
    ):
        if col not in _existing:
            try:
                conn.execute(f"ALTER TABLE turns ADD COLUMN {col} {kind}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
    _api_existing = {row[1] for row in conn.execute("PRAGMA table_info(turn_api_calls)")}
    for col, kind in (("cache_write_5m", "INT"), ("cache_write_1h", "INT"),
                      ("cache_ttl_requested", "TEXT"), ("lane_family", "TEXT")):
        if col not in _api_existing:
            try:
                conn.execute(f"ALTER TABLE turn_api_calls ADD COLUMN {col} {kind}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
    for _col in ("last_cache_read", "last_cache_write", "last_uncached"):
        if _col not in _existing:
            try:
                conn.execute(f"ALTER TABLE turns ADD COLUMN {_col} INT")
            except sqlite3.OperationalError:
                pass  # raced with another writer; column now exists
    # Last-CALL prompt discriminator (r6 finding 9). Deliberately NULLable with
    # no DEFAULT on the migration path: NULL means "this row predates the
    # column, so its final-call provenance was never recorded", and the
    # renderer falls back to the absorbing turn-level flag for those rows —
    # exactly the behaviour they have today. A DEFAULT 0 here would instead
    # assert "the final call WAS measured" about every historical row,
    # including genuinely unmeasured ones, and render their placeholder zeros
    # as real window numbers. New rows always bind an explicit 0/1.
    if "last_call_prompt_unknown" not in _existing:
        try:
            conn.execute("ALTER TABLE turns ADD COLUMN last_call_prompt_unknown INT")
        except sqlite3.OperationalError:
            pass  # raced with another writer; column now exists
    # Request-composition columns (fixed vs non-fixed breakdown of the final
    # call). Same guarded additive pattern. INT for the token buckets, TEXT for
    # the per-call composition JSON blob.
    for _col in (
        "comp_sys_tokens", "comp_tool_schema_tokens", "comp_history_tokens",
        "comp_history_message_count",
        "comp_tool_result_tokens", "comp_tool_arg_tokens", "comp_tool_result_count",
        "comp_skills_tokens", "comp_framing_tokens",
        "comp_skills_count",
    ):
        if _col not in _existing:
            try:
                conn.execute(f"ALTER TABLE turns ADD COLUMN {_col} INT")
            except sqlite3.OperationalError:
                pass
    if "comp_calls_json" not in _existing:
        try:
            conn.execute("ALTER TABLE turns ADD COLUMN comp_calls_json TEXT")
        except sqlite3.OperationalError:
            pass
    # Per-class cost columns (SPEC-C). REAL, nullable. Same guarded additive
    # pattern so a DB created before these existed gains them on next open, and
    # a partially-migrated / concurrently-written DB never raises.
    for _col in (
        "cost_uncached_usd", "cost_cache_read_usd",
        "cost_cache_write_usd", "cost_output_usd",
    ):
        if _col not in _existing:
            try:
                conn.execute(f"ALTER TABLE turns ADD COLUMN {_col} REAL")
            except sqlite3.OperationalError:
                pass
    # Depth column (session nesting: 0=parent, 1=child, 2=grandchild, ...).
    # INT, nullable. Guarded per-column migration (D1: only swallow "duplicate
    # column" OperationalError, re-raise lock/corruption/other errors).
    if "depth" not in _existing:
        try:
            conn.execute("ALTER TABLE turns ADD COLUMN depth INT")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
    # CLI correlation column (Phase 3: ccusage <-> Hermes correlation). TEXT,
    # nullable. Populated only when spawning a CLI subprocess; enables
    # deduplication in tokens.ace. Same guarded additive pattern.
    if "cli_invocation_id" not in _existing:
        try:
            conn.execute("ALTER TABLE turns ADD COLUMN cli_invocation_id TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
    # Per-sub attribution rollup columns (PRD-subs-ace §5.2, card C1). Both
    # nullable TEXT; old rows stay NULL and nothing backfills them (I3).
    # `served_subs_json` is the cheap per-turn display rollup
    # ({"sub-vps-7": 3, ...}); `attribution` is the turn's dominant provenance.
    # ALTER is NOT idempotent, hence the PRAGMA-guarded per-column pattern.
    # `terminal_error` (t_6c09f0c2): NULL for a turn that ended normally; the
    # turn_exit_reason for one that ended failed (fallback chain exhausted,
    # raised, ...). Lets turn-level surfaces count failed turns.
    for _col in ("served_subs_json", "attribution", "terminal_error"):
        if _col not in _existing:
            try:
                conn.execute(f"ALTER TABLE turns ADD COLUMN {_col} TEXT")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
    # UNKNOWN != 0 discriminator columns. New rows default measured, but rows
    # that PRE-DATE this schema can be ambiguous: old Hermes collapsed an
    # omitted provider usage payload into integer zeroes and had no
    # discriminator with which to distinguish that from a measured zero.
    #
    # The latch is therefore restricted to unpriced rows whose token counts are
    # ALL ZERO — the only rows that are actually ambiguous. A blanket
    # "unpriced" latch was wrong on both ends: `usage_unknown` is not a
    # pricing-private column, it is the shared DISPLAY discriminator
    # (`agent.usage_pricing.prompt_tokens_unknown`, `plugins/blackbox/
    # last_turn.py`, `plugins/blackbox/card.py` all branch on it), and a row is
    # routinely NULL-cost because pricing REFUSED (no catalog entry for the
    # route) while carrying perfectly good provider-measured counts. Latching
    # those rewrote real measurements as "unknown" on every user-facing card,
    # irreversibly. Already-priced history is left untouched either way.
    unknown_columns = {
        "output_tokens_unknown",
        "input_tokens_unknown",
        "cache_read_tokens_unknown",
        "cache_write_tokens_unknown",
        "usage_unknown",
    }
    migrating_legacy_unknown_schema = not unknown_columns <= _existing
    if "output_tokens_unknown" not in _existing:
        try:
            conn.execute(
                "ALTER TABLE turns ADD COLUMN output_tokens_unknown INT DEFAULT 0"
            )
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
    for column in ("input_tokens_unknown", "cache_read_tokens_unknown",
                   "cache_write_tokens_unknown", "usage_unknown"):
        if column not in _existing:
            try:
                conn.execute(f"ALTER TABLE turns ADD COLUMN {column} INT DEFAULT 0")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
    if migrating_legacy_unknown_schema:
        # Narrowed once more (r6 finding 6). "unpriced AND all counts zero" is
        # still not a purely ambiguous population: a turn interrupted before
        # any API call fired, a blackbox-off turn, or one whose first call
        # failed genuinely consumed zero tokens and is a MEASURED zero. Latching
        # those was irreversible and had two costs: `reprice_unpriced` now
        # short-circuits on any unknown flag, so they could never again heal to
        # `priced_zero` and were reported `still_unknown` on every future sweep;
        # and every consumer that branches on `usage_unknown` rendered them
        # `unknown in + unknown out` forever.
        #
        # `cost_status` is the discriminator the old schema DID carry: a row the
        # pre-UNKNOWN code already labelled 'unknown' is one where that code
        # could not account the call, which is exactly the ambiguous population.
        # A row with any other status (including NULL, i.e. never accounted)
        # keeps its measured zero. Already-priced history is untouched either
        # way.
        #
        # The all-zero-counts guard may only name token columns this DB actually
        # has. `turns` is created with them, but a table that already existed
        # never gains them (CREATE TABLE IF NOT EXISTS is a no-op and no ALTER
        # adds them), so a sufficiently old DB can reach here without e.g.
        # `cache_write` — naming it unconditionally aborts the whole migration
        # with "no such column". When a count column is absent the row cannot
        # carry a measurement in it, which is exactly the zero the guard tests
        # for, so omitting it from the sum preserves the condition's meaning.
        _count_cols = [
            c for c in ("input_tokens", "output_tokens", "cache_read", "cache_write")
            if c in _existing
        ]
        _all_zero = (
            " + ".join(f"COALESCE({c}, 0)" for c in _count_cols) + " = 0"
            if _count_cols
            else "1 = 1"
        )
        _status_guard = (
            " AND cost_status = 'unknown'" if "cost_status" in _existing else ""
        )
        # All five columns, not just the aggregate (r6 round-4 finding 8). A
        # migrated row IS the shape `CanonicalUsage.fully_unknown()` describes —
        # the provider measured NO bucket — and that classmethod's docstring
        # states why the aggregate alone is insufficient: consumers read these
        # flags NARROWLY. `plugins/blackbox/card.py::_tokens_out_line` and the
        # thin `/usage` card gate the output line on `output_tokens_unknown`
        # ALONE, and `prompt_tokens_unknown` ORs only the three input flags.
        # Setting `usage_unknown` by itself therefore left every migrated row
        # still rendering `0 out` as a measurement on exactly the surfaces this
        # latch exists to correct.
        #
        # Same `_existing` guard as the counts above: an old DB that never
        # gained a column cannot be updated on it, and naming it would abort the
        # whole migration with "no such column". Note `_existing` is the
        # PRE-ALTER snapshot, so it CANNOT be used here — in the migration case
        # it is precisely the set that lacks these columns. The ALTERs above run
        # unconditionally for all five and re-raise anything other than
        # "duplicate column", so reaching this line means all five exist.
        _set_clause = ", ".join(f"{c} = 1" for c in sorted(unknown_columns))
        conn.execute(
            f"UPDATE turns SET {_set_clause} "
            "WHERE cost_usd IS NULL "
            "AND cost_uncached_usd IS NULL AND cost_cache_read_usd IS NULL "
            "AND cost_cache_write_usd IS NULL AND cost_output_usd IS NULL"
            f"{_status_guard} "
            f"AND {_all_zero}"
        )
    _ensure_turn_indexes(conn)
    conn.commit()


# Indexes on `turns` must be created AFTER the additive column migration and
# only when every indexed column exists: `CREATE INDEX IF NOT EXISTS` inside the
# schema executescript raised `no such column: ts_start` on any legacy ledger
# whose `turns` table predates that column (CREATE TABLE IF NOT EXISTS skips the
# table, the index then references a column the table never gained), aborting
# the whole script BEFORE the guarded ALTERs ran -- a pre-#905 ledger could not
# be opened at all (t_71ae3a75). A column the table lacks simply gets no index.
_TURN_INDEXES = (
    ("idx_blackbox_turns_chat_end", ("platform", "chat_id", "ts_end")),
    ("idx_blackbox_turns_chat_start", ("chat_id", "ts_start")),
    ("idx_blackbox_turns_cost", ("cost_usd",)),
    ("idx_blackbox_turns_ts_start", ("ts_start",)),
    # The skill-stats miner (skills-dashboard launchd, hourly) opens every
    # ledger with `SELECT DISTINCT profile FROM turns`. Without an index on
    # profile that is a full SCAN of the overflow-heavy table: measured 107 s on
    # the 1.5 GB fleet ledger under I/O load (2026-09-23), past the miner's
    # 180 s budget, so the Stats tab silently served last-good data.
    ("idx_blackbox_turns_profile", ("profile",)),
)


def _ensure_turn_indexes(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(turns)").fetchall()}
    for name, cols in _TURN_INDEXES:
        if all(c in existing for c in cols):
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS {name} ON turns({', '.join(cols)})"
            )


def _int(value: Any) -> int:
    return int(value or 0)


def _int_or_none(value: Any) -> int | None:
    """Preserve NULL for columns that are genuinely absent (old rows / no data).

    Unlike ``_int`` (which coerces None→0), this keeps None as SQL NULL so a
    missing last-call split reads back as None and the renderer can fall back to
    the plain Context line instead of showing a misleading ``0`` split.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_int(value: Any) -> int:
    return 1 if bool(value) else 0


def _cost_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def lane_family(provider: str) -> str:
    p = str(provider or "").strip().lower()
    for prefixes, family in (
        (("claude-apx", "claude-apr", "claude-api-proxy"), "apx/apr"),
        (("claude-bpx", "claude-bpr", "claude-bridge"), "bpx/bpr"),
        (("claude-cpx", "claude-cpr"), "cpx/cpr"),
        (("xai",), "xai"), (("openrouter",), "openrouter"),
    ):
        if p.startswith(prefixes):
            return family
    return "codex" if p == "openai-codex" else "other"


# Auxiliary-model calls (compression, title_generation, vision, web_extract, ...)
# are ledgered with ``attribution='aux:<task>'`` and ``lane_family='aux'``. Their
# tokens are NOT part of the turn's main-model totals, so every reader that
# reconciles against the turn or measures the main lane's cache behaviour must
# exclude this family (card t_39628ae3).
AUX_LANE_FAMILY = "aux"
AUX_ATTRIBUTION_PREFIX = "aux:"
_NOT_AUX = "COALESCE(lane_family, '') != 'aux'"


def is_aux_attribution(attribution: Any) -> bool:
    return (isinstance(attribution, str) and attribution.startswith(AUX_ATTRIBUTION_PREFIX)
            and len(attribution) > len(AUX_ATTRIBUTION_PREFIX))


def _refresh_cache_monitoring(conn: sqlite3.Connection, turn_id: str) -> None:
    """Reconcile calls even when they arrive after the turn row."""
    # Main lane only: an aux call (lane_family='aux') is a different model on a
    # different prompt; letting it be the "first call" or add to the write tiers
    # would misreport the main conversation's cache behaviour.
    conn.execute(f"""
        UPDATE turns SET
            cache_write_5m = (SELECT SUM(cache_write_5m) FROM turn_api_calls
                              WHERE turn_id = turns.turn_id AND {_NOT_AUX}),
            cache_write_1h = (SELECT SUM(cache_write_1h) FROM turn_api_calls
                              WHERE turn_id = turns.turn_id AND {_NOT_AUX}),
            first_call_cache_miss = (
                SELECT CASE WHEN input_tokens IS NULL OR cache_read IS NULL
                                      OR cache_write IS NULL THEN NULL
                            WHEN input_tokens + cache_read + cache_write <= 0 THEN NULL
                            -- Read-only lanes (xAI, codex, OpenAI-shaped usage with
                            -- only prompt_tokens_details.cached_tokens) never report
                            -- a write, so the write rule below is structurally 0
                            -- there. A cold first call is a read under half the
                            -- prompt. Anthropic lanes keep the write rule.
                            WHEN lane_family IS NOT NULL
                                 AND lane_family NOT IN ('apx/apr', 'bpx/bpr', 'cpx/cpr')
                                 AND cache_write = 0 THEN
                                CASE WHEN cache_read * 2 < input_tokens + cache_read
                                     THEN 1 ELSE 0 END
                            WHEN cache_write * 5 >= 4 *
                                 (input_tokens + cache_read + cache_write) THEN 1
                            ELSE 0 END
                FROM turn_api_calls WHERE turn_id = turns.turn_id AND {_NOT_AUX}
                  -- First SUCCESSFUL call: a 429/5xx/timeout attempt carries
                  -- zero usage and would hide the cold write on the retry.
                  AND (http_status IS NULL OR http_status BETWEEN 200 AND 299)
                  AND COALESCE(input_tokens, 0) + COALESCE(cache_read, 0)
                      + COALESCE(cache_write, 0) > 0
                ORDER BY seq LIMIT 1)
        WHERE turn_id = ?
    """, (turn_id,))


def backfill_cache_monitoring() -> None:
    """Explicit historical fill; never infer cache tiers or compaction cost."""
    with _connect() as conn:
        for family, prefixes in (
            ("apx/apr", ("claude-apx", "claude-apr", "claude-api-proxy")),
            ("bpx/bpr", ("claude-bpx", "claude-bpr", "claude-bridge")),
            ("cpx/cpr", ("claude-cpx", "claude-cpr")),
            ("codex", ("openai-codex",)), ("xai", ("xai",)),
            ("openrouter", ("openrouter",)),
        ):
            for prefix in prefixes:
                conn.execute("UPDATE turn_api_calls SET lane_family=? "
                             "WHERE lane_family IS NULL AND lower(provider) LIKE ?",
                             (family, prefix + "%"))
        conn.execute("UPDATE turn_api_calls SET lane_family='other' WHERE lane_family IS NULL")
        conn.execute("""
            UPDATE turns SET gap_prev_turn_s = ts_start - (
                SELECT prev.ts_end FROM turns AS prev
                WHERE prev.chat_id = turns.chat_id AND prev.chat_id != ''
                  AND (prev.ts_start < turns.ts_start OR
                       (prev.ts_start = turns.ts_start AND prev.turn_id < turns.turn_id))
                ORDER BY prev.ts_start DESC, prev.turn_id DESC LIMIT 1)
            WHERE gap_prev_turn_s IS NULL AND chat_id != ''
        """)
        for (turn_id,) in conn.execute(
            "SELECT DISTINCT turn_id FROM turn_api_calls WHERE turn_id IN "
            "(SELECT turn_id FROM turns WHERE first_call_cache_miss IS NULL)"
        ).fetchall():
            _refresh_cache_monitoring(conn, turn_id)


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    data["is_subagent"] = bool(data.get("is_subagent"))
    data["interrupted"] = bool(data.get("interrupted"))
    data["alerted"] = bool(data.get("alerted"))
    data["cache_read_tokens"] = data.pop("cache_read")
    data["cache_write_tokens"] = data.pop("cache_write")
    data["reasoning_tokens"] = data.pop("reasoning")
    # Last-call cache split — preserve None (old rows predate these columns).
    # Expose under the _tokens names for renderer consistency, keeping the raw
    # column keys too so direct SELECT * consumers (e.g. /context) still match.
    data["last_cache_read_tokens"] = data.get("last_cache_read")
    data["last_cache_write_tokens"] = data.get("last_cache_write")
    data["last_uncached_tokens"] = data.get("last_uncached")
    try:
        data["tools"] = json.loads(data.get("tools") or "[]")
    except json.JSONDecodeError:
        data["tools"] = []
    data["tools_summary"] = tools_summary(data["tools"])
    return data


# Columns insert_turn owns. The ORDER here IS the bind order of the value
# tuple below — keep the two in lockstep. Columns NOT in this tuple
# (served_subs_json, attribution) are not owned by TurnRecord, so they are
# excluded from the upsert's DO UPDATE: a re-finalize must not erase the
# call-ledger rollup or another writer's attribution. This is an UPSERT and
# not INSERT OR REPLACE — REPLACE deletes the whole row first, NULLing every
# column absent from the insert list.
_INSERT_TURN_COLUMNS = (
    "turn_id", "parent_turn_id", "is_subagent", "depth", "ts_start", "ts_end",
    "profile", "provider", "model", "platform", "chat_id", "chat_name",
    "api_calls", "tools", "input_tokens", "output_tokens", "cache_read",
    "cache_write", "reasoning", "context_used", "context_length",
    "idle_compaction_fired", "compaction_tokens_before",
    "compaction_tokens_after", "compaction_cost_usd",
    "last_cache_read", "last_cache_write", "last_uncached",
    "last_call_prompt_unknown",
    "comp_sys_tokens", "comp_tool_schema_tokens", "comp_history_tokens",
    "comp_history_message_count",
    "comp_tool_result_tokens", "comp_tool_arg_tokens", "comp_tool_result_count",
    "comp_skills_tokens", "comp_framing_tokens",
    "comp_skills_count",
    "comp_calls_json",
    "cost_usd", "cost_status",
    "cost_uncached_usd", "cost_cache_read_usd",
    "cost_cache_write_usd", "cost_output_usd",
    "interrupted", "alerted", "user_text",
    "final_text", "cli_invocation_id",
    "output_tokens_unknown",
    "input_tokens_unknown", "cache_read_tokens_unknown",
    "cache_write_tokens_unknown", "usage_unknown",
    "terminal_error",
)

_INSERT_TURN_SQL = (
    "INSERT INTO turns (" + ", ".join(_INSERT_TURN_COLUMNS) + ") VALUES ("
    + ", ".join("?" for _ in _INSERT_TURN_COLUMNS) + ") "
    "ON CONFLICT(turn_id) DO UPDATE SET "
    + ", ".join(
        f"{col} = excluded.{col}"
        for col in _INSERT_TURN_COLUMNS
        if col != "turn_id"
    )
)


def _refresh_served_subs(conn: sqlite3.Connection, turn_id: str) -> None:
    """Aggregate known per-call subscription keys for an already stored turn."""
    rows = conn.execute(
        """SELECT sub_key, COUNT(*) FROM turn_api_calls
           WHERE turn_id = ? AND sub_key IS NOT NULL AND sub_key != ''
           GROUP BY sub_key ORDER BY sub_key""",
        (turn_id,),
    ).fetchall()
    if rows:
        conn.execute(
            "UPDATE turns SET served_subs_json = ? WHERE turn_id = ?",
            (json.dumps({sub: count for sub, count in rows}), turn_id),
        )

def insert_turn(record: TurnRecord) -> None:
    """Persist one turn. Telemetry failures are logged but never raised."""
    try:
        with _connect() as conn:
            conn.execute(
                _INSERT_TURN_SQL,
                (
                    record.turn_id,
                    record.parent_turn_id,
                    _bool_int(record.is_subagent),
                    _int_or_none(record.depth),
                    float(record.ts_start or 0.0),
                    float(record.ts_end or 0.0),
                    record.profile or "",
                    record.provider or "",
                    record.model or "",
                    record.platform or "",
                    record.chat_id or "",
                    record.chat_name or "",
                    _int(record.api_calls),
                    json.dumps(list(record.tools or [])),
                    _int(record.input_tokens),
                    _int(record.output_tokens),
                    _int(record.cache_read_tokens),
                    _int(record.cache_write_tokens),
                    _int(record.reasoning_tokens),
                    _int(record.context_used),
                    _int(record.context_length),
                    None if record.idle_compaction_fired is None else _bool_int(record.idle_compaction_fired),
                    _int_or_none(record.compaction_tokens_before),
                    _int_or_none(record.compaction_tokens_after),
                    _cost_float(record.compaction_cost_usd),
                    _int_or_none(record.last_cache_read_tokens),
                    _int_or_none(record.last_cache_write_tokens),
                    _int_or_none(record.last_uncached_tokens),
                    _bool_int(record.last_call_prompt_unknown),
                    _int_or_none(record.comp_sys_tokens),
                    _int_or_none(record.comp_tool_schema_tokens),
                    _int_or_none(record.comp_history_tokens),
                    _int_or_none(record.comp_history_message_count),
                    _int_or_none(record.comp_tool_result_tokens),
                    _int_or_none(record.comp_tool_arg_tokens),
                    _int_or_none(record.comp_tool_result_count),
                    _int_or_none(record.comp_skills_tokens),
                    _int_or_none(record.comp_framing_tokens),
                    _int_or_none(record.comp_skills_count),
                    record.comp_calls_json,
                    _cost_float(record.cost_usd),
                    record.cost_status or "unknown",
                    _cost_float(record.cost_uncached_usd),
                    _cost_float(record.cost_cache_read_usd),
                    _cost_float(record.cost_cache_write_usd),
                    _cost_float(record.cost_output_usd),
                    _bool_int(record.interrupted),
                    _bool_int(record.alerted),
                    scrub_and_truncate(record.user_text),
                    scrub_and_truncate(record.final_text),
                    record.cli_invocation_id,
                    _bool_int(record.output_tokens_unknown),
                    _bool_int(record.input_tokens_unknown),
                    _bool_int(record.cache_read_tokens_unknown),
                    _bool_int(record.cache_write_tokens_unknown),
                    _bool_int(record.usage_unknown),
                    scrub_and_truncate(record.terminal_error, 300)
                    if record.terminal_error
                    else None,
                ),
            )
            _refresh_served_subs(conn, record.turn_id)
            conn.execute("DELETE FROM turn_tool_calls WHERE turn_id = ?", (record.turn_id,))
            for seq, call in enumerate(record.tool_calls or []):
                conn.execute(
                    """
                    INSERT INTO turn_tool_calls (
                        turn_id, seq, name, args_preview, result_preview
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        record.turn_id,
                        seq,
                        str(call.get("name", "")),
                        scrub_and_truncate(call.get("args_preview", "")),
                        scrub_and_truncate(call.get("result_preview", "")),
                    ),
                )
            conn.execute(
                """
                INSERT INTO last_turn(platform, chat_id, turn_id)
                VALUES (?, ?, ?)
                ON CONFLICT(platform, chat_id) DO UPDATE SET turn_id = excluded.turn_id
                """,
                (record.platform or "", record.chat_id or "", record.turn_id),
            )
            if record.chat_id:
                conn.execute("""
                    UPDATE turns SET gap_prev_turn_s = ts_start - (
                        SELECT prev.ts_end FROM turns AS prev
                        WHERE prev.chat_id = turns.chat_id
                          AND (prev.ts_start < turns.ts_start OR
                               (prev.ts_start = turns.ts_start AND prev.turn_id < turns.turn_id))
                        ORDER BY prev.ts_start DESC, prev.turn_id DESC LIMIT 1)
                    WHERE turn_id = ?
                """, (record.turn_id,))
            _refresh_cache_monitoring(conn, record.turn_id)
    except Exception:
        logger.warning("blackbox telemetry insert failed", exc_info=True)


def insert_api_call(
    turn_id: str, seq: int, *, ts: float, provider: str, model: str,
    usage: CanonicalUsage, sub_key: str | None, attribution: str,
    http_status: int | None = None, relay_synthetic: bool = False,
    route_id: str | None = None,
    cache_write_5m: int | None = None,
    cache_write_1h: int | None = None,
    cache_ttl_requested: str | None = None,
) -> None:
    """Append one call, including zero-usage failures, without changing turn totals.

    The accumulator owns sequence allocation. Duplicate keys and invalid
    provenance raise rather than silently replacing or dropping ledger rows.
    Calls may arrive before their parent turn is finalized.
    """
    aux = is_aux_attribution(attribution)
    if not aux and attribution not in ("wire", "pinned", "inferred", "external"):
        raise ValueError(f"Invalid API-call attribution: {attribution!r}")
    # SQLite permits NULL in a non-INTEGER PRIMARY KEY column, so the composite
    # key alone does not stop a NULL turn_id/seq row (and NULLs never collide,
    # so duplicates accumulate unnoticed). Reject at the boundary too — the DDL
    # NOT NULLs guard existing DBs, this guards the caller's intent.
    if turn_id is None or seq is None:
        raise ValueError(
            f"turn_api_calls key parts must not be None (turn_id={turn_id!r}, seq={seq!r})"
        )
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO turn_api_calls (
                turn_id, seq, ts, provider, sub_key, model, input_tokens,
                output_tokens, cache_read, cache_write, reasoning, attribution,
                http_status, relay_synthetic, route_id, cache_write_5m,
                cache_write_1h, cache_ttl_requested, lane_family
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (turn_id, seq, ts, provider, sub_key, model, usage.input_tokens,
             usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens,
             usage.reasoning_tokens, attribution, http_status,
             _bool_int(relay_synthetic), route_id, cache_write_5m,
             cache_write_1h, cache_ttl_requested,
             AUX_LANE_FAMILY if aux else lane_family(provider)),
        )
        _refresh_cache_monitoring(conn, turn_id)
        if conn.execute("SELECT 1 FROM turns WHERE turn_id = ?", (turn_id,)).fetchone():
            _refresh_served_subs(conn, turn_id)


def mark_alerted(turn_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE turns SET alerted = 1 WHERE turn_id = ? AND alerted = 0",
            (turn_id,),
        )
        return cur.rowcount == 1


# ---------------------------------------------------------------------------
# Conversation prefix-stability guard (card t_c07124ab).
#
# A prefix-cached provider serves a cache hit only when the leading bytes of a
# request equal an earlier request's. Within one session the harness must keep
# the system prompt, the tool schemas and every already-sent message
# byte-stable between consecutive requests. ``record_prefix_check`` compares
# the fingerprint of each outbound request with the previous request of the
# same session, persists one ``prefix_mutations`` row per violated segment and
# reports whether this is the session's FIRST unexplained mutation — the
# transition the caller pages on, exactly once per session.
# ---------------------------------------------------------------------------

PREFIX_ALERT_MIN_SPACING_S = 15 * 60
_PREFIX_ALERT_META_KEY = "prefix_guard_last_alert_ts"


def _prefix_context(prev: sqlite3.Row, *, pid: int, api_mode: str, model: str,
                    reset: str | None) -> str | None:
    """Why a rewrite is EXPECTED for this pair, or None (unexplained).

    Compaction is the one sanctioned history rewrite and is tagged by the
    harness event that performed it (``reset``), so it is excluded rather
    than allowlisted. A cross-process pair (gateway restart rebuilt the
    request from the transcript) and a model / API-mode switch (different
    tool schema, different system prompt) are recorded but not paged.
    """
    if reset:
        return reset
    if prev["pid"] is not None and int(prev["pid"]) != int(pid):
        return "process_restart"
    if (prev["api_mode"] or "") != (api_mode or ""):
        return "api_mode_change"
    if (prev["model"] or "") != (model or ""):
        return "model_change"
    return None


def record_prefix_check(
    *, session_key: str, turn_id: str, seq: int, ts: float, pid: int,
    provider: str, model: str, api_mode: str, fingerprint: dict[str, Any],
    cache_read: int | None, reset: str | None = None,
    allowlist: Any = None,
) -> dict[str, Any]:
    """Compare one request with the session's previous request and persist.

    Returns ``{"violations": [...], "alert": bool, "suppressed": int}``.
    ``alert`` is True only on the transition from "this session has never
    paged" to "it has an unexplained, non-allowlisted mutation", and only
    when the profile-wide spacing floor allows another page; ``suppressed``
    counts pages held back by that floor since the last one went out.
    The previous fingerprint is replaced by this one in the same transaction,
    so a mutation is reported once, at the request that introduced it.
    """
    from plugins.blackbox import prefix_guard

    violations: list[dict[str, Any]] = []
    pageable: list[dict[str, Any]] = []
    alert = False
    suppressed = 0
    previous: dict[str, Any] | None = None
    with _connect() as conn:
        prev = conn.execute(
            "SELECT * FROM prefix_sessions WHERE session_key = ?", (session_key,)
        ).fetchone()
        if prev is not None and prev["fingerprint_json"]:
            try:
                prev_fp = json.loads(prev["fingerprint_json"])
            except (TypeError, ValueError):
                prev_fp = None
            if isinstance(prev_fp, dict):
                previous = {
                    "turn_id": prev["turn_id"], "seq": prev["seq"], "ts": prev["ts"],
                    "messages": len(prev_fp.get("messages") or []),
                    "cache_read": prev["cache_read"],
                }
                context = _prefix_context(
                    prev, pid=pid, api_mode=api_mode, model=model, reset=reset,
                )
                if context is None and prefix_guard.native_checkpoint_changed(prev_fp, fingerprint):
                    context = "compaction:native"
                for diff in prefix_guard.compare(prev_fp, fingerprint):
                    reason = prefix_guard.allowlist_reason(
                        allowlist, segment=diff["segment"], session_key=session_key,
                        now=ts,
                    )
                    row = {
                        **diff,
                        "context": context,
                        "allowlisted": 1 if reason else 0,
                        "allowlist_reason": reason,
                    }
                    violations.append(row)
                    conn.execute(
                        """
                        INSERT INTO prefix_mutations (
                            ts, session_key, turn_id, seq, prev_turn_id, prev_seq,
                            prev_ts, provider, lane_family, model, segment, kind,
                            first_divergent_index, bytes_before, bytes_after,
                            messages_before, messages_after, context, allowlisted,
                            allowlist_reason, cache_read_before, cache_read_after
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ts, session_key, turn_id, seq, prev["turn_id"], prev["seq"],
                            prev["ts"], provider, lane_family(provider), model,
                            diff["segment"], diff["kind"], diff["first_divergent_index"],
                            diff["bytes_before"], diff["bytes_after"],
                            len(prev_fp.get("messages") or []),
                            len(fingerprint.get("messages") or []),
                            context, row["allowlisted"], reason,
                            prev["cache_read"], cache_read,
                        ),
                    )
                pageable = [
                    v for v in violations
                    if v["kind"] == prefix_guard.KIND_MUTATION
                    and v["context"] is None and not v["allowlisted"]
                ]
                if pageable and prev["alerted_at"] is None:
                    last = conn.execute(
                        "SELECT value FROM meta WHERE key = ?", (_PREFIX_ALERT_META_KEY,)
                    ).fetchone()
                    last_ts = float(last["value"]) if last and last["value"] else None
                    held = conn.execute(
                        "SELECT value FROM meta WHERE key = 'prefix_guard_suppressed'"
                    ).fetchone()
                    suppressed = int(held["value"]) if held and held["value"] else 0
                    if last_ts is None or ts - last_ts >= PREFIX_ALERT_MIN_SPACING_S:
                        alert = True
                        conn.execute(
                            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                            (_PREFIX_ALERT_META_KEY, repr(float(ts))),
                        )
                        conn.execute(
                            "INSERT OR REPLACE INTO meta(key, value) "
                            "VALUES ('prefix_guard_suppressed', '0')"
                        )
                        conn.execute(
                            "UPDATE prefix_mutations SET alerted = 1 WHERE id = ("
                            "SELECT MAX(id) FROM prefix_mutations WHERE session_key = ?"
                            " AND context IS NULL AND allowlisted = 0 AND kind = ?)",
                            (session_key, prefix_guard.KIND_MUTATION),
                        )
                    else:
                        suppressed += 1
                        conn.execute(
                            "INSERT OR REPLACE INTO meta(key, value) "
                            "VALUES ('prefix_guard_suppressed', ?)",
                            (str(suppressed),),
                        )
        alerted_at = prev["alerted_at"] if prev is not None else None
        if pageable and alerted_at is None:
            # A held-back page still consumes the session's single alert slot:
            # the mutation is on record, the next one in this session is noise.
            alerted_at = ts
        conn.execute(
            """
            INSERT OR REPLACE INTO prefix_sessions (
                session_key, turn_id, seq, ts, pid, api_mode, model, cache_read,
                fingerprint_json, updated_at, alerted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_key, turn_id, seq, ts, pid, api_mode, model, cache_read,
                json.dumps(fingerprint, separators=(",", ":")), time.time(), alerted_at,
            ),
        )
    return {"violations": violations, "alert": alert, "suppressed": suppressed,
            "previous": previous}


# ---------------------------------------------------------------------------
# New-model pricing sentinel ledger (card t_2e382a4b).
#
# The store holds NO rate table (INV-1) and makes NO pricing decision here — it
# only records that the caller (plugins.blackbox.sentinel) judged a turn to be
# unpriced for a model absent from the pricing snapshot. The (model, provider)
# PRIMARY KEY is the dedup mechanism: `note_unpriced_model` returns True ONLY
# on the transition from unseen → seen, which is what gates the one-shot alert.
# ---------------------------------------------------------------------------


def note_unpriced_model(model: str, provider: str, *, first_seen: str | None = None) -> bool:
    """Record a first sighting of an unpriced model. True iff it was NEW.

    Uses ``INSERT OR IGNORE`` so a repeat sighting is a cheap no-op and can
    never overwrite the original ``first_seen`` / ``alerted_at``. The boolean
    return is the exactly-once alert gate, mirroring ``mark_alerted``.
    """
    stamp = first_seen or _iso_now()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO seen_unpriced_models (
                model, provider, first_seen, alerted_at
            ) VALUES (?, ?, ?, NULL)
            """,
            (str(model or ""), str(provider or ""), stamp),
        )
        return cur.rowcount == 1


def mark_unpriced_alerted(model: str, provider: str, *, alerted_at: str | None = None) -> bool:
    """Stamp ``alerted_at`` after a sentinel alert is successfully delivered.

    Only stamps a row whose ``alerted_at`` is still NULL, so a retry or an
    out-of-band writer can't rewrite the original notification time.
    """
    stamp = alerted_at or _iso_now()
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE seen_unpriced_models SET alerted_at = ?
            WHERE model = ? AND provider = ? AND alerted_at IS NULL
            """,
            (stamp, str(model or ""), str(provider or "")),
        )
        return cur.rowcount == 1


def list_unpriced_models() -> list[dict[str, Any]]:
    """All ledger rows, oldest sighting first (operator/diagnostic read)."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT model, provider, first_seen, alerted_at
            FROM seen_unpriced_models ORDER BY first_seen ASC, model ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# M2 — reprice/backfill pass (SPEC 2026-06-30 §5C). Heals turns recorded with
# cost_usd NULL once a price becomes resolvable (a new snapshot entry, or M1
# now reaching it), and classifies zero-token turns as priced_zero. Dry-run by
# default; only --apply mutates. The store holds NO rate table (INV-1): the
# caller injects `pricing_fn` (the real compute_turn_cost in prod).
# ---------------------------------------------------------------------------

# Routes whose price is a PURE function of the stored (provider, model) + tokens
# and linear per-token. A live-catalog route (official_models_api) prices against
# TODAY's catalog, so repricing a historical row there would be wrong — leave it
# NULL (INV-9 / RC-A). subscription_included is pure ($0).
_PURE_BILLING_MODES = frozenset({"official_docs_snapshot", "subscription_included"})
_ZERO_PERCLASS = {"uncached": 0.0, "cache_read": 0.0, "cache_write": 0.0, "output": 0.0}


def reprice_unpriced(pricing_fn, *, apply: bool = False, limit: int | None = None) -> dict:
    """Re-price turns whose cost_usd is NULL. Returns the pinned status schema
    ``{scanned, repriced, zeroed, still_unknown}`` (RC-F).

    - Zero-token rows → priced_zero, regardless of route (nothing to misprice).
    - Real-token rows → priced ONLY when the route is pure/static (INV-9) and the
      resolved entry carries no non-linear per-request term (RC-B); otherwise the
      row is left NULL and counted in ``still_unknown``.
    - Only rows with cost_usd AND all four per-class cost columns NULL are touched
      (RC-C), so a partial/aborted prior write is never clobbered.
    - ``apply=False`` (default) computes the plan and writes nothing (D-8).
    - ``apply=True`` writes a forward-only rollback manifest of the affected
      turn_ids before mutating (D-7), commits in one transaction, and re-checks
      the NULL guard in the UPDATE WHERE so a concurrently-priced row can't be
      overwritten (INV-8).
    """
    conn = _connect()
    try:
        conn.execute("PRAGMA busy_timeout=5000")  # INV-8: wait, don't error, on a live writer
        sel = (
            "SELECT turn_id, model, provider, "
            "COALESCE(input_tokens,0) AS i, COALESCE(output_tokens,0) AS o, "
            "COALESCE(cache_read,0) AS cr, COALESCE(cache_write,0) AS cw, "
            "COALESCE(output_tokens_unknown,0) AS ou, "
            "COALESCE(input_tokens_unknown,0) AS iu, "
            "COALESCE(cache_read_tokens_unknown,0) AS cru, "
            "COALESCE(cache_write_tokens_unknown,0) AS cwu, "
            "COALESCE(usage_unknown,0) AS uu "
            "FROM turns WHERE cost_usd IS NULL "
            "AND cost_uncached_usd IS NULL AND cost_cache_read_usd IS NULL "
            "AND cost_cache_write_usd IS NULL AND cost_output_usd IS NULL"
        )
        if limit:
            sel += f" LIMIT {int(limit)}"
        rows = conn.execute(sel).fetchall()
        scanned = len(rows)

        # (turn_id, cost, status, perclass, is_zero)
        candidates: list[tuple[str, float, str, dict, bool]] = []
        for r in rows:
            route = resolve_billing_route(r["model"], provider=r["provider"])
            usage_unknown = any(bool(r[key]) for key in ("ou", "iu", "cru", "cwu", "uu"))
            if usage_unknown:
                # Missing counts cannot be repriced from their integer-zero
                # placeholders. The one exception is a route whose marginal
                # cost is $0 independently of token counts. Either way, keep
                # the row inside ``scanned`` so unresolved rows contribute to
                # ``still_unknown`` instead of disappearing from the report.
                if route.billing_mode == "subscription_included":
                    candidates.append(
                        (r["turn_id"], 0.0, "included", dict(_ZERO_PERCLASS), False)
                    )
                continue
            total = r["i"] + r["o"] + r["cr"] + r["cw"]
            if total == 0:
                # Zero-token → costless → priced_zero, regardless of route.
                candidates.append((r["turn_id"], 0.0, "priced_zero", dict(_ZERO_PERCLASS), True))
                continue
            # Real-token: route-purity gate (INV-9 / RC-A).
            entry = get_pricing_entry(r["model"], provider=r["provider"])
            if route.billing_mode not in _PURE_BILLING_MODES:
                # A notional relay (openai-codex → official_models_api) consults the
                # curated snapshot BEFORE any live catalog (#650). When the resolved
                # entry came from the snapshot, the price is the same pure function
                # of (model, tokens) as an official_docs_snapshot route — repricing a
                # historical row from it is correct. Only a LIVE-catalog entry (or no
                # entry) keeps the row NULL (INV-9 / RC-A).
                if entry is None or getattr(entry, "source", None) != "official_docs_snapshot":
                    continue  # live-catalog / unknown-mode route → still_unknown
            # Non-linear per-request term can't be reconstructed from summed
            # tokens (RC-B): refuse rather than misprice.
            if entry is not None and getattr(entry, "request_cost", None) is not None:
                continue
            tokens = {
                "input_tokens": r["i"],
                "output_tokens": r["o"],
                "cache_read_tokens": r["cr"],
                "cache_write_tokens": r["cw"],
            }
            cost, status, perclass = pricing_fn(r["model"], r["provider"], tokens)
            if cost is None or status not in ("estimated", "actual", "included", "priced_zero"):
                continue  # genuinely unknown → leave NULL
            candidates.append((r["turn_id"], float(cost), status, perclass, False))

        repriced = sum(1 for c in candidates if not c[4])
        zeroed = sum(1 for c in candidates if c[4])
        result = {
            "scanned": scanned,
            "repriced": repriced,
            "zeroed": zeroed,
            "still_unknown": scanned - len(candidates),
        }

        if apply and candidates:
            # Apply inside one transaction, capturing the turn_ids that ACTUALLY
            # updated (rowcount == 1). A row a concurrent writer priced between
            # SELECT and UPDATE fails the NULL guard → rowcount 0 → excluded, so
            # the manifest never claims a row it didn't change.
            committed_ids: list[str] = []
            with conn:  # single transaction
                for turn_id, cost, status, perclass, _is_zero in candidates:
                    cur = conn.execute(
                        "UPDATE turns SET cost_usd = ?, cost_status = ?, "
                        "cost_uncached_usd = ?, cost_cache_read_usd = ?, "
                        "cost_cache_write_usd = ?, cost_output_usd = ? "
                        "WHERE turn_id = ? AND cost_usd IS NULL "
                        "AND cost_uncached_usd IS NULL AND cost_cache_read_usd IS NULL "
                        "AND cost_cache_write_usd IS NULL AND cost_output_usd IS NULL",
                        (
                            cost,
                            status,
                            perclass.get("uncached"),
                            perclass.get("cache_read"),
                            perclass.get("cache_write"),
                            perclass.get("output"),
                            turn_id,
                        ),
                    )
                    if cur.rowcount == 1:
                        committed_ids.append(turn_id)
            # Write the forward-only rollback manifest ONLY after the transaction
            # committed, and only for rows that actually changed (D-7). A unique
            # nonce prevents same-second back-to-back runs from clobbering each
            # other's manifest.
            if committed_ids:
                nonce = uuid.uuid4().hex[:8]
                manifest = _db_path().parent / f"reprice-run-{time.strftime('%Y%m%d-%H%M%S')}-{nonce}.json"
                manifest.write_text(json.dumps(committed_ids), encoding="utf-8")
        return result
    finally:
        conn.close()


def get_turn(turn_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM turns WHERE turn_id = ?", (turn_id,)).fetchone()
    return _row_to_dict(row)


def get_tool_calls(turn_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT seq, name, args_preview, result_preview
            FROM turn_tool_calls
            WHERE turn_id = ?
            ORDER BY seq
            """,
            (turn_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_last_turn(platform: str, chat_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT t.*
            FROM last_turn lt
            JOIN turns t ON t.turn_id = lt.turn_id
            WHERE lt.platform = ? AND lt.chat_id = ?
            """,
            (platform or "", chat_id or ""),
        ).fetchone()
    return _row_to_dict(row)


def session_rollup(platform: str, chat_id: str, limit: int = 50) -> dict[str, Any]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT turn_id, cost_usd, is_subagent
            FROM turns
            WHERE platform = ? AND chat_id = ?
            ORDER BY ts_end DESC
            LIMIT ?
            """,
            (platform or "", chat_id or "", int(limit)),
        ).fetchall()
    costs = [float(row["cost_usd"] or 0.0) for row in rows]
    total = sum(costs)
    max_row = max(rows, key=lambda row: float(row["cost_usd"] or 0.0), default=None)
    # Split main vs subagent turns so the caller can show an honest breakdown
    # (the total already INCLUDES subagent spend — they are real rows here).
    sub_rows = [r for r in rows if int(r["is_subagent"] or 0) == 1]
    sub_total = sum(float(r["cost_usd"] or 0.0) for r in sub_rows)
    return {
        "total_usd": total,
        "count": len(rows),
        "avg_usd": total / len(rows) if rows else 0.0,
        "max_turn": dict(max_row) if max_row else None,
        "subagent_count": len(sub_rows),
        "subagent_usd": sub_total,
    }


def top_turns(n: int = 5, since_days: int = 30) -> list[dict[str, Any]]:
    cutoff = time.time() - (int(since_days) * 86400)
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM turns
            WHERE ts_end >= ?
            ORDER BY cost_usd DESC
            LIMIT ?
            """,
            (cutoff, int(n)),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def subagent_rollup(platform: str, chat_id: str, limit: int = 200) -> dict[str, Any]:
    """Aggregate subagent turns spawned within a channel (platform + chat_id).

    Subagent turns are recorded as their own rows with ``is_subagent = 1`` and
    carry the PARENT's channel identity (``platform``/``chat_id`` are stamped
    from ``_blackbox_parent_platform``/``_blackbox_parent_chat_id`` by
    delegate_tool). They are NOT reliably linkable to a single parent *turn* —
    ``parent_turn_id`` holds the parent's session KEY, and parent turns don't
    store their own session key — so we roll up by channel, the same axis every
    other /cost view (session/latest/turn) resolves on.

    Returns counts + summed cost/tokens across the channel's subagent turns,
    plus how many are unpriced (cost_usd IS NULL) so the caller can show an
    honest "+N unpriced" note instead of silently undercounting.
    """
    if not platform or not chat_id:
        return {"count": 0, "total_usd": 0.0, "unpriced": 0,
                "input_tokens": 0, "output_tokens": 0, "models": []}
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT cost_usd, input_tokens, output_tokens, cache_read, model
            FROM turns
            WHERE is_subagent = 1 AND platform = ? AND chat_id = ?
            ORDER BY ts_end DESC
            LIMIT ?
            """,
            (platform or "", chat_id or "", int(limit)),
        ).fetchall()
    total = 0.0
    unpriced = 0
    in_tok = 0
    out_tok = 0
    models: list[str] = []
    for r in rows:
        c = r["cost_usd"]
        if c is None:
            unpriced += 1
        else:
            total += float(c or 0.0)
        in_tok += int(r["input_tokens"] or 0)
        out_tok += int(r["output_tokens"] or 0)
        m = r["model"]
        if m and m not in models:
            models.append(str(m))
    return {
        "count": len(rows),
        "total_usd": total,
        "unpriced": unpriced,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "models": models,
    }


# Minimum age a PARENTLESS turn_api_calls row must reach before the sweeper may
# delete it, independent of the retention window. Call rows are written as the
# API calls happen; the parent turns row only appears at finalize, so a row with
# no parent is indistinguishable from an in-flight turn. 24h is far longer than
# any turn can plausibly run, so the ledger of a live turn is never harvested
# even when retention is configured aggressively short.
_ORPHAN_GRACE_S = 86400


def sweep(retention_days: int, max_deletes: int = 10000) -> int:
    today = time.strftime("%Y-%m-%d", time.gmtime())
    cutoff = time.time() - (int(retention_days) * 86400)
    max_deletes = max(0, int(max_deletes))
    if max_deletes == 0:
        return 0

    with _connect() as conn:
        last_sweep = conn.execute(
            "SELECT value FROM meta WHERE key = 'last_sweep_date'"
        ).fetchone()
        if last_sweep and last_sweep["value"] == today:
            return 0

        rows = conn.execute(
            """
            SELECT turn_id
            FROM turns
            WHERE ts_end < ?
            ORDER BY ts_end
            LIMIT ?
            """,
            (cutoff, max_deletes),
        ).fetchall()
        turn_ids = [row["turn_id"] for row in rows]
        if turn_ids:
            placeholders = ",".join("?" for _ in turn_ids)
            conn.execute(
                f"DELETE FROM turn_tool_calls WHERE turn_id IN ({placeholders})",
                turn_ids,
            )
            conn.execute(
                f"DELETE FROM turn_api_calls WHERE turn_id IN ({placeholders})",
                turn_ids,
            )
            conn.execute(
                f"DELETE FROM turns WHERE turn_id IN ({placeholders})",
                turn_ids,
            )
            conn.execute(
                f"DELETE FROM last_turn WHERE turn_id IN ({placeholders})",
                turn_ids,
            )
        # Parentless call rows: a call is appended as it happens, but its parent
        # turn only lands at finalize. A crash/interrupt between the two leaves
        # an orphan that the parent-keyed cascade above can never reach, so it
        # would outlive retention forever. Sweep those on their own ts, in the
        # same transaction, bounded by the same max_deletes budget.
        #
        # The retention cutoff alone is NOT a safe predicate here: "no parent
        # row yet" is the normal state of an in-flight turn, so a turn still
        # running past retention — or any short retention setting — would have
        # its call ledger deleted out from under it before finalize. Require the
        # row to be past BOTH retention and an independent grace period, so a
        # live turn is never harvested.
        orphan_cutoff = time.time() - max(_ORPHAN_GRACE_S, int(retention_days) * 86400)
        conn.execute(
            """
            DELETE FROM turn_api_calls
            WHERE rowid IN (
                SELECT rowid FROM turn_api_calls
                WHERE ts < ?
                  AND turn_id NOT IN (SELECT turn_id FROM turns)
                ORDER BY ts
                LIMIT ?
            )
            """,
            (orphan_cutoff, max_deletes),
        )
        deleted = len(turn_ids)
        # Atomic: deletes + sentinel commit together so a crash can't leave the
        # rows deleted without the sentinel (or vice-versa). The sentinel is
        # written BEFORE the single commit; sqlite3's context manager commits on
        # clean exit and rolls back on exception.
        conn.execute(
            """
            INSERT INTO meta(key, value)
            VALUES ('last_sweep_date', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (today,),
        )
        conn.commit()
        return deleted


def debug_stats() -> dict[str, Any]:
    """Operational snapshot for the /cost debug command.

    Surfaces the on-disk DB path, whether it exists, row/side-table counts,
    alerted count, oldest/newest turn timestamps, and the last sweep date.
    Read-only; never raises — returns an ``error`` key on failure so the
    debug command can show *why* telemetry looks empty.
    """
    path = _db_path()
    out: dict[str, Any] = {
        "db_path": str(path),
        "db_exists": path.exists(),
        "db_size_bytes": path.stat().st_size if path.exists() else 0,
    }
    try:
        with _connect() as conn:
            out["turns"] = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
            out["tool_calls"] = conn.execute(
                "SELECT COUNT(*) FROM turn_tool_calls"
            ).fetchone()[0]
            out["alerted"] = conn.execute(
                "SELECT COUNT(*) FROM turns WHERE alerted = 1"
            ).fetchone()[0]
            out["subagent_turns"] = conn.execute(
                "SELECT COUNT(*) FROM turns WHERE is_subagent = 1"
            ).fetchone()[0]
            row = conn.execute(
                "SELECT MIN(ts_end), MAX(ts_end) FROM turns"
            ).fetchone()
            out["oldest_ts"] = row[0]
            out["newest_ts"] = row[1]
            sweep_row = conn.execute(
                "SELECT value FROM meta WHERE key = 'last_sweep_date'"
            ).fetchone()
            out["last_sweep_date"] = sweep_row["value"] if sweep_row else None
    except Exception as exc:  # pragma: no cover - defensive
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out
