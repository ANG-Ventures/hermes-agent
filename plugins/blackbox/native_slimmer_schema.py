"""Blackbox schema helpers for native-content-slimmer telemetry.

This module is deliberately dependency-light and side-effect free. Native
content slimming can build validated telemetry rows without importing the
Blackbox SQLite store, so telemetry failures stay isolated from the hook path.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import sqlite3
from typing import Any, Iterable, Mapping

COMPRESSOR_NATIVE_SLIMMER = "native-slimmer"
RAW_BYTES_SOURCE_NATIVE_EXACT = "native-exact"
TOKENIZER_LABEL_UTF8_BYTES_DIV_4 = "heuristic:utf8-bytes/4"
TOKEN_ESTIMATE_KIND = "estimate"

RAW_SOURCE_PRE_TRUNCATION_TERMINAL = "pre-truncation-terminal"
RAW_SOURCE_POST_RTK = "post-rtk"
RAW_SOURCE_TOOL_RESULT_RETURNED = "tool-result-returned"
RAW_SOURCE_TOOL_CONTRACT_BOUNDED = "tool-contract-bounded"
VALID_RAW_SOURCES = frozenset(
    {
        RAW_SOURCE_PRE_TRUNCATION_TERMINAL,
        RAW_SOURCE_POST_RTK,
        RAW_SOURCE_TOOL_RESULT_RETURNED,
        RAW_SOURCE_TOOL_CONTRACT_BOUNDED,
    }
)

VALID_NATIVE_SLIMMER_MODES = frozenset({"shadow", "active_lossless"})
VALID_NATIVE_SLIMMER_ACTIONS = frozenset({"would_replace", "replace"})
STRATEGY_MIGRATION_COLUMNS: Mapping[str, str] = {
    "strategy": "TEXT",
    "view_bytes": "INT",
    "lossy_view": "INT",
    "expansions_triggered": "INT",
}


@dataclass(frozen=True)
class NativeSlimmerTelemetryEvent:
    """One native-slimmer savings event ready for Blackbox persistence."""

    compressor: str
    mode: str
    action: str
    tool_name: str
    session_id: str
    tool_call_id: str
    artifact_id: str
    raw_sha256: str
    raw_source: str
    raw_bytes_source: str
    original_bytes: int
    emitted_bytes: int
    saved_bytes: int
    status_quo_bytes: int
    saved_vs_raw_bytes: int
    saved_vs_status_quo_bytes: int
    original_tokens_est: int
    status_quo_tokens_est: int
    emitted_tokens_est: int
    saved_tokens_est: int
    saved_vs_raw_tokens_est: int
    saved_vs_status_quo_tokens_est: int
    tokenizer_label: str
    token_estimate_kind: str
    lossy: bool
    classification_reason: str
    savings_key: str
    task_id: str = ""
    turn_id: str = ""
    api_request_id: str = ""
    tool_status: str = "success"
    strategy: str | None = None
    view_bytes: int | None = None
    lossy_view: bool | None = None
    expansions_triggered: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ensure_strategy_columns(
    conn: sqlite3.Connection,
    *,
    table: str = "native_slimmer_savings",
) -> None:
    """Apply PRD-5's additive strategy-column migration to a savings table."""

    existing = {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for column, column_type in STRATEGY_MIGRATION_COLUMNS.items():
        if column in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
    conn.commit()


def estimate_tokens_from_bytes(byte_count: int) -> int:
    """Return the house UTF-8 bytes/4 token estimate, rounded up.

    The result is an estimate, not billed provider usage. Callers must carry
    ``TOKENIZER_LABEL_UTF8_BYTES_DIV_4`` beside the value so downstream reports
    do not mistake it for a provider tokenizer or real usage row.
    """

    count = max(0, int(byte_count or 0))
    return (count + 3) // 4


def build_native_slimmer_event(
    *,
    mode: str,
    action: str,
    tool_name: str,
    session_id: str,
    tool_call_id: str,
    artifact_id: str,
    raw_sha256: str,
    raw_source: str,
    original_bytes: int,
    emitted_bytes: int,
    classification_reason: str,
    lossy: bool = False,
    task_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
    tool_status: str = "success",
    tokenizer_label: str = TOKENIZER_LABEL_UTF8_BYTES_DIV_4,
    status_quo_baseline_bytes: int | None = None,
    strategy: str | None = None,
    view_bytes: int | None = None,
    lossy_view: bool | None = None,
    expansions_triggered: int | None = None,
) -> dict[str, Any]:
    """Build a validated native-slimmer telemetry event dict.

    Savings are computed as the single native boundary delta for this exact
    artifact identity. ``saved_bytes`` is the status-quo delta when a caller
    supplies a pre-existing truncation baseline (for example terminal output),
    while ``saved_vs_raw_bytes`` preserves the full raw delta for diagnostics.
    Repeated rows carry the same ``savings_key`` so rollups can mechanically
    dedupe them.
    """

    mode = str(mode or "")
    if mode not in VALID_NATIVE_SLIMMER_MODES:
        raise ValueError(f"invalid native slimmer mode: {mode!r}")
    action = str(action or "")
    if action not in VALID_NATIVE_SLIMMER_ACTIONS:
        raise ValueError(f"invalid native slimmer action: {action!r}")
    raw_source = str(raw_source or "")
    if raw_source not in VALID_RAW_SOURCES:
        raise ValueError(f"invalid native slimmer raw_source: {raw_source!r}")

    original = max(0, int(original_bytes or 0))
    emitted = max(0, int(emitted_bytes or 0))
    if status_quo_baseline_bytes is None:
        status_quo = original
    else:
        status_quo = min(original, max(0, int(status_quo_baseline_bytes or 0)))
    saved_vs_raw = max(0, original - emitted)
    saved_vs_status_quo = max(0, status_quo - emitted)
    saved = saved_vs_status_quo
    original_tokens = estimate_tokens_from_bytes(original)
    status_quo_tokens = estimate_tokens_from_bytes(status_quo)
    emitted_tokens = estimate_tokens_from_bytes(emitted)
    saved_vs_raw_tokens = max(0, original_tokens - emitted_tokens)
    saved_vs_status_quo_tokens = max(0, status_quo_tokens - emitted_tokens)
    saved_tokens = saved_vs_status_quo_tokens
    artifact = str(artifact_id or "")
    session = str(session_id or "")
    call = str(tool_call_id or "")
    digest = str(raw_sha256 or "")

    event = NativeSlimmerTelemetryEvent(
        compressor=COMPRESSOR_NATIVE_SLIMMER,
        mode=mode,
        action=action,
        tool_name=str(tool_name or ""),
        session_id=session,
        tool_call_id=call,
        artifact_id=artifact,
        raw_sha256=digest,
        raw_source=raw_source,
        raw_bytes_source=RAW_BYTES_SOURCE_NATIVE_EXACT,
        original_bytes=original,
        emitted_bytes=emitted,
        saved_bytes=saved,
        status_quo_bytes=status_quo,
        saved_vs_raw_bytes=saved_vs_raw,
        saved_vs_status_quo_bytes=saved_vs_status_quo,
        original_tokens_est=original_tokens,
        status_quo_tokens_est=status_quo_tokens,
        emitted_tokens_est=emitted_tokens,
        saved_tokens_est=saved_tokens,
        saved_vs_raw_tokens_est=saved_vs_raw_tokens,
        saved_vs_status_quo_tokens_est=saved_vs_status_quo_tokens,
        tokenizer_label=str(tokenizer_label or TOKENIZER_LABEL_UTF8_BYTES_DIV_4),
        token_estimate_kind=TOKEN_ESTIMATE_KIND,
        lossy=bool(lossy),
        classification_reason=str(classification_reason or ""),
        savings_key="|".join([session, call, digest, artifact]),
        task_id=str(task_id or ""),
        turn_id=str(turn_id or ""),
        api_request_id=str(api_request_id or ""),
        tool_status=str(tool_status or "success"),
        strategy=strategy if strategy is None else str(strategy),
        view_bytes=None if view_bytes is None else max(0, int(view_bytes or 0)),
        lossy_view=None if lossy_view is None else bool(lossy_view),
        expansions_triggered=None if expansions_triggered is None else max(0, int(expansions_triggered or 0)),
    )
    return event.to_dict()


def rollup_native_slimmer_events(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Roll up native-slimmer rows without double-counting repeats.

    Duplicate rows for the same ``savings_key`` are counted once. This keeps a
    retry, idempotent artifact reuse, or an active+shadow duplicate from
    inflating the headline savings number.
    """

    seen: set[str] = set()
    saved_bytes = 0
    saved_tokens = 0
    by_raw_source: dict[str, dict[str, int]] = {}
    labels: set[str] = set()

    for event in events:
        if event.get("compressor") != COMPRESSOR_NATIVE_SLIMMER:
            continue
        key = str(event.get("savings_key") or "")
        if not key:
            key = "|".join(
                str(event.get(part) or "")
                for part in ("session_id", "tool_call_id", "raw_sha256", "artifact_id")
            )
        if key in seen:
            continue
        seen.add(key)

        row_saved_bytes = max(0, int(event.get("saved_bytes") or 0))
        row_saved_tokens = max(0, int(event.get("saved_tokens_est") or 0))
        saved_bytes += row_saved_bytes
        saved_tokens += row_saved_tokens
        label = str(event.get("tokenizer_label") or "")
        if label:
            labels.add(label)
        raw_source = str(event.get("raw_source") or "unknown")
        bucket = by_raw_source.setdefault(raw_source, {"events": 0, "saved_bytes": 0, "saved_tokens_est": 0})
        bucket["events"] += 1
        bucket["saved_bytes"] += row_saved_bytes
        bucket["saved_tokens_est"] += row_saved_tokens

    return {
        "compressor": COMPRESSOR_NATIVE_SLIMMER,
        "event_count": len(seen),
        "saved_bytes": saved_bytes,
        "saved_tokens_est": saved_tokens,
        "token_estimate_kind": TOKEN_ESTIMATE_KIND,
        "tokenizer_label": next(iter(labels)) if len(labels) == 1 else ("mixed" if labels else ""),
        "by_raw_source": by_raw_source,
    }
