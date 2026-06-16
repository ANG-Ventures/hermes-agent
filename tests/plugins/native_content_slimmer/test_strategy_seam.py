from __future__ import annotations

import shutil
import sqlite3
import time
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

import plugins.native_content_slimmer.classifier as classifier_module
from hermes_constants import get_hermes_home
from plugins.blackbox import native_slimmer_store as nss
from plugins.blackbox.native_slimmer_digest import render_digest
from plugins.blackbox.native_slimmer_dollarize import dollarize_rollup
from plugins.blackbox.native_slimmer_schema import (
    RAW_SOURCE_TOOL_RESULT_RETURNED,
    build_native_slimmer_event,
    ensure_strategy_columns,
)
from plugins.native_content_slimmer.classifier import (
    COMPRESS_OFFLOAD,
    LOSSLESS_OFFLOAD,
    PASS_THROUGH,
    classify_tool_result,
    deterministic_preview,
)
from plugins.native_content_slimmer.config import NativeContentSlimmerConfig
from plugins.native_content_slimmer.breaker import ExpansionRateCircuitBreaker
from plugins.native_content_slimmer.hook import NativeContentSlimmerHooks
from plugins.native_content_slimmer.marker import parse_marker, verify_marker_auth
from plugins.native_content_slimmer.store import ArtifactStore, raw_byte_len, sha256_text
from plugins.native_content_slimmer.strategies import registry
from plugins.native_content_slimmer.strategies.base import CompressedView, run_with_timeout_guard


class RecordingCompressor:
    def __init__(self, view_text: str | None = None, *, raise_error: bool = False) -> None:
        self.view_text = view_text
        self.raise_error = raise_error
        self.calls: list[tuple[str, dict[str, object]]] = []

    def compress(self, raw: str, *, params: Mapping[str, object]) -> CompressedView | None:
        self.calls.append((raw, dict(params)))
        if self.raise_error:
            raise RuntimeError("compressor exploded")
        if self.view_text is None:
            return None
        return CompressedView(
            view_text=self.view_text,
            view_bytes=raw_byte_len(self.view_text),
            strategy_name="fake_compact",
        )


@pytest.fixture(autouse=True)
def empty_strategy_registry() -> Iterator[None]:
    registry.clear_registry_for_tests()
    yield
    registry.clear_registry_for_tests()


def _large_payload(label: str = "seam") -> str:
    return f"{label}-HEAD\n" + (f"{label}-middle evidence line\n" * 900) + f"{label}-TAIL\n"


def _active_hooks(tmp_path: Path) -> NativeContentSlimmerHooks:
    return NativeContentSlimmerHooks(
        NativeContentSlimmerConfig(
            enabled=True,
            mode="active_lossless",
            compression_mode="active",
            min_bytes=100,
            preview_bytes=120,
        ),
        store=ArtifactStore(tmp_path / "artifacts"),
        secret=b"strategy-seam-test-secret",
        breaker=_healthy_breaker(("web_extract", "text", "fake_compact")),
    )


def _healthy_breaker(lane: tuple[str, str, str]) -> ExpansionRateCircuitBreaker:
    breaker = ExpansionRateCircuitBreaker()
    for _ in range(20):
        breaker.record_result(lane, expanded=False)
    return breaker


def _register(compressor: RecordingCompressor) -> None:
    registry.register_compressor(
        tool_name="web_extract",
        content_class="text",
        compressor=compressor,
        eval_run_id="eval-pass-fixture",
        threshold="GO",
        strategy_name="fake_compact",
    )


def test_empty_registry_keeps_classifier_and_marker_on_lossless_offload(tmp_path: Path) -> None:
    raw = _large_payload("empty")

    classified = classify_tool_result(
        tool_name="web_extract",
        result=raw,
        status="success",
        min_bytes=100,
        preview_bytes=120,
    )

    assert registry.select_compressor(tool_name="web_extract", content_class="text") is None
    assert classified.outcome == LOSSLESS_OFFLOAD
    assert classified.reason == "eligible_lossless_offload"
    assert classified.recommended_strategy is None
    assert classified.preview == deterministic_preview(raw, preview_bytes=120)

    replacement = _active_hooks(tmp_path).transform_tool_result(
        tool_name="web_extract",
        result=raw,
        status="success",
        session_id="sess-empty-registry",
        tool_call_id="call-empty-registry",
    )

    assert replacement is not None
    parsed = parse_marker(replacement)
    assert parsed is not None
    assert "strategy" not in parsed.fields
    assert "lossy_view" not in parsed.fields
    assert "recoverable" not in parsed.fields
    assert parsed.preview == deterministic_preview(raw, preview_bytes=120).strip("\n")


def test_compress_offload_calls_selected_compressor_and_marks_recoverable(tmp_path: Path) -> None:
    raw = _large_payload("compress")
    view_text = "COMPRESSED VIEW\nkeep the important bits"
    compressor = RecordingCompressor(view_text)
    _register(compressor)

    classified = classify_tool_result(
        tool_name="web_extract",
        result=raw,
        status="success",
        min_bytes=100,
        preview_bytes=120,
        compression_enabled=True,
    )
    assert classified.outcome == COMPRESS_OFFLOAD
    assert classified.reason == "eligible_compress_offload"
    assert classified.recommended_strategy == "fake_compact"
    assert classified.preview is None

    hooks = _active_hooks(tmp_path)
    replacement = hooks.transform_tool_result(
        tool_name="web_extract",
        result=raw,
        status="success",
        session_id="sess-compress",
        tool_call_id="call-compress",
    )

    assert compressor.calls == [(raw, {})]
    assert replacement is not None
    parsed = parse_marker(replacement)
    assert parsed is not None
    assert parsed.preview == view_text
    assert parsed.fields["strategy"] == "fake_compact"
    assert parsed.fields["lossy_view"] == "true"
    assert parsed.fields["recoverable"] == "true"
    assert int(parsed.fields["view_bytes"]) == raw_byte_len(view_text)
    verification = verify_marker_auth(replacement, secret=b"strategy-seam-test-secret", ledger=hooks.ledger)
    assert verification.ok is True

    record = hooks.store.read_record(parsed.fields["id"], session_id="sess-compress")
    assert record["raw_text"] == raw
    assert record["raw_sha256"] == sha256_text(raw)
    assert record["lossy"] is True
    assert record["strategy"] == "fake_compact"
    assert record["view_bytes"] == raw_byte_len(view_text)
    assert record["lossy_view"] is True
    assert record["recoverable"] is True


def test_compress_marker_rehydrates_with_strategy_fields(tmp_path: Path) -> None:
    raw = _large_payload("durable-compress")
    store = ArtifactStore(tmp_path / "artifacts")
    compressor = RecordingCompressor("DURABLE COMPRESSED VIEW")
    _register(compressor)
    first_runtime = NativeContentSlimmerHooks(
        NativeContentSlimmerConfig(
            enabled=True,
            mode="active_lossless",
            compression_mode="active",
            min_bytes=100,
            preview_bytes=120,
        ),
        store=store,
        secret=b"strategy-seam-test-secret",
        breaker=_healthy_breaker(("web_extract", "text", "fake_compact")),
    )

    marker = first_runtime.transform_tool_result(
        tool_name="web_extract",
        result=raw,
        status="success",
        session_id="sess-durable-compress",
        tool_call_id="call-durable-compress",
    )
    assert marker is not None

    second_runtime = NativeContentSlimmerHooks(
        NativeContentSlimmerConfig(
            enabled=True,
            mode="active_lossless",
            compression_mode="active",
            min_bytes=100,
            preview_bytes=120,
        ),
        store=store,
        secret=b"strategy-seam-test-secret",
        breaker=_healthy_breaker(("web_extract", "text", "fake_compact")),
    )
    reused = second_runtime.transform_tool_result(
        tool_name="web_extract",
        result=raw,
        status="success",
        session_id="sess-durable-compress",
        tool_call_id="call-durable-compress",
    )

    assert reused == marker
    parsed = parse_marker(reused or "")
    assert parsed is not None
    assert parsed.fields["strategy"] == "fake_compact"
    assert parsed.fields["lossy_view"] == "true"


@pytest.mark.parametrize("compressor", [RecordingCompressor(None), RecordingCompressor("ignored", raise_error=True)])
def test_compressor_none_or_exception_falls_back_to_deterministic_preview(
    tmp_path: Path,
    compressor: RecordingCompressor,
) -> None:
    raw = _large_payload("fallback")
    _register(compressor)

    replacement = _active_hooks(tmp_path).transform_tool_result(
        tool_name="web_extract",
        result=raw,
        status="success",
        session_id="sess-fallback",
        tool_call_id="call-fallback",
    )

    assert compressor.calls == [(raw, {})]
    assert replacement is not None
    parsed = parse_marker(replacement)
    assert parsed is not None
    assert parsed.preview == deterministic_preview(raw, preview_bytes=120).strip("\n")
    assert "strategy" not in parsed.fields
    assert "lossy_view" not in parsed.fields
    assert "recoverable" not in parsed.fields


def test_secret_check_runs_before_strategy_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode_on_lookup(**_: object) -> object:
        raise AssertionError("strategy lookup must not run before secret check")

    monkeypatch.setattr(classifier_module.strategy_registry, "select_compressor", explode_on_lookup)
    raw = ("Authorization: Bearer token-value-123456\n" * 20)

    classified = classifier_module.classify_tool_result(
        tool_name="web_extract",
        result=raw,
        status="success",
        min_bytes=1,
    )

    assert classified.outcome == PASS_THROUGH
    assert classified.reason == "secret_classified_no_store"
    assert classified.content_class == "secret"
    assert classified.secret_match == "bearer"


def test_unknown_content_class_denies_to_lossless_offload() -> None:
    compressor = RecordingCompressor("json view")
    registry.register_compressor(
        tool_name="web_extract",
        content_class="json",
        compressor=compressor,
        eval_run_id="eval-pass-fixture",
        threshold="GO",
    )

    assert registry.select_compressor(tool_name="web_extract", content_class="UNKNOWN") is None
    classified = classify_tool_result(
        tool_name="web_extract",
        result=_large_payload("text"),
        status="success",
        min_bytes=100,
        preview_bytes=120,
    )
    assert classified.outcome == LOSSLESS_OFFLOAD
    assert compressor.calls == []


def test_timeout_guard_aborts_to_none() -> None:
    class SlowCompressor:
        def compress(self, raw: str, *, params: Mapping[str, object]) -> CompressedView:
            time.sleep(0.1)
            return CompressedView(view_text=raw, view_bytes=raw_byte_len(raw), strategy_name="slow")

    assert run_with_timeout_guard(SlowCompressor(), "slow input", params={}, timeout_ms=1) is None


def _event(action: str, *, key: str = "s|c|sha|art") -> dict[str, object]:
    ev = build_native_slimmer_event(
        mode="active_lossless" if action == "replace" else "shadow",
        action=action,
        tool_name="web_extract",
        session_id="s",
        tool_call_id="c",
        artifact_id="art",
        raw_sha256="sha",
        raw_source=RAW_SOURCE_TOOL_RESULT_RETURNED,
        original_bytes=24_000,
        emitted_bytes=20_000,
        classification_reason="large",
        status_quo_baseline_bytes=24_000,
    )
    ev["savings_key"] = key
    return ev


def _copy_live_turns_db_or_synthetic(tmp_path: Path) -> Path:
    db = tmp_path / "turns.db"
    live = get_hermes_home() / "blackbox" / "turns.db"
    if live.exists():
        shutil.copy2(live, db)
        return db
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE turns (turn_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    return db


def test_strategy_columns_migrate_additively_and_digest_tolerates_null_strategy(tmp_path: Path) -> None:
    db = _copy_live_turns_db_or_synthetic(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        nss.ensure_schema(conn)
        now = time.time()
        nss.insert_event(_event("would_replace"), created_at=now, conn=conn)

        ensure_strategy_columns(conn)

        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({nss.TABLE})").fetchall()}
        assert {"strategy", "view_bytes", "lossy_view", "expansions_triggered"}.issubset(columns)
        rows = nss.fetch_between(now - 1, now + 1, conn=conn)
        assert len(rows) == 1
        assert rows[0]["strategy"] is None
        assert rows[0]["view_bytes"] is None
        assert rows[0]["lossy_view"] is None
        assert rows[0]["expansions_triggered"] is None

        digest = render_digest(dollarize_rollup(rows))
        assert "SHADOW would have saved" in digest
    finally:
        conn.close()
