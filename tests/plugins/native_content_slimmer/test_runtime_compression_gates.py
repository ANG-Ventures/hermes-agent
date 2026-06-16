from __future__ import annotations

import sqlite3
from pathlib import Path

from plugins.blackbox import native_slimmer_store as nss
from plugins.native_content_slimmer.breaker import ExpansionRateCircuitBreaker
from plugins.native_content_slimmer.classifier import COMPRESS_OFFLOAD, LOSSLESS_OFFLOAD, classify_tool_result
from plugins.native_content_slimmer.config import NativeContentSlimmerConfig
from plugins.native_content_slimmer.hook import NativeContentSlimmerHooks
from plugins.native_content_slimmer.marker import parse_marker
from plugins.native_content_slimmer.store import ArtifactStore
from plugins.native_content_slimmer.strategies import registry


def _json_raw() -> str:
    return '[{"State":"unhealthy","Command":"' + ('x' * 20_000) + '"}]'


def _log_raw() -> str:
    return ("2026-01-01T00:00:00Z INFO ok 1\n" * 500) + "FATAL keep me\n"


def _diff_raw() -> str:
    return "diff --git a/a b/a\n@@ -1,20 +1,20 @@\n" + (" context\n" * 50) + "-old\n+new\n" + (" context\n" * 50)


def _grep_raw() -> str:
    return "\n".join(f"src/f.py:{idx}: value={idx % 3}" for idx in range(300))


def _healthy_breaker(lane: tuple[str, str, str]) -> ExpansionRateCircuitBreaker:
    breaker = ExpansionRateCircuitBreaker()
    for _ in range(20):
        breaker.record_result(lane, expanded=False)
    return breaker


def _hooks(tmp_path: Path, cfg: NativeContentSlimmerConfig, *, breaker: ExpansionRateCircuitBreaker | None = None) -> NativeContentSlimmerHooks:
    return NativeContentSlimmerHooks(
        cfg,
        store=ArtifactStore(tmp_path / "artifacts"),
        secret=b"runtime-gates-test-secret",
        breaker=breaker,
    )


def test_fresh_runtime_registry_populates_eval_gated_lanes() -> None:
    registry.clear_registry_for_tests()

    lanes = {(lane.tool_name, lane.content_class, lane.strategy_name) for lane in registry.registered_lanes()}

    assert ("web_extract", "json", "json_compact") in lanes
    assert ("terminal", "log", "log_dedup") in lanes
    assert ("terminal", "diff", "diff_collapse") in lanes
    assert ("terminal", "grep", "grep_cluster") in lanes
    assert all(lane.eval_run_id and lane.threshold for lane in registry.registered_lanes())


def test_classifier_reaches_json_log_diff_and_grep_compressor_lanes() -> None:
    cases = [
        ("web_extract", _json_raw(), "", "json", "json_compact"),
        ("terminal", _log_raw(), "", "log", "log_dedup"),
        ("terminal", _diff_raw(), "git diff", "diff", "diff_collapse"),
        ("terminal", _grep_raw(), "", "grep", "grep_cluster"),
    ]

    for tool_name, raw, command, content_class, strategy in cases:
        classified = classify_tool_result(
            tool_name=tool_name,
            result=raw,
            min_bytes=100,
            command=command,
            compression_enabled=True,
        )
        assert classified.content_class == content_class
        assert classified.outcome == COMPRESS_OFFLOAD
        assert classified.recommended_strategy == strategy


def test_compression_mode_off_disabled_strategy_and_cold_breaker_do_not_compress(tmp_path: Path) -> None:
    raw = _json_raw()

    off = _hooks(
        tmp_path / "off",
        NativeContentSlimmerConfig(enabled=True, mode="active_lossless", compression_mode="off", min_bytes=100, preview_bytes=120),
    )
    off_marker = parse_marker(off.transform_tool_result(tool_name="web_extract", result=raw, session_id="s-off", tool_call_id="c") or "")
    assert off_marker is not None
    assert "strategy" not in off_marker.fields
    assert off.telemetry_records[-1]["classification_reason"] == "eligible_lossless_offload"

    disabled = _hooks(
        tmp_path / "disabled",
        NativeContentSlimmerConfig(
            enabled=True,
            mode="active_lossless",
            compression_mode="active",
            compression_strategies={"json_compact": False},
            min_bytes=100,
            preview_bytes=120,
        ),
    )
    disabled_marker = parse_marker(disabled.transform_tool_result(tool_name="web_extract", result=raw, session_id="s-disabled", tool_call_id="c") or "")
    assert disabled_marker is not None
    assert "strategy" not in disabled_marker.fields
    assert disabled.telemetry_records[-1]["classification_reason"] == "eligible_lossless_offload"

    cold = _hooks(
        tmp_path / "cold",
        NativeContentSlimmerConfig(enabled=True, mode="active_lossless", compression_mode="active", min_bytes=100, preview_bytes=120),
    )
    cold_marker = parse_marker(cold.transform_tool_result(tool_name="web_extract", result=raw, session_id="s-cold", tool_call_id="c") or "")
    assert cold_marker is not None
    assert "strategy" not in cold_marker.fields
    assert cold.telemetry_records[-1]["classification_reason"] == "compression_breaker_cold_start"


def test_canary_compression_returns_marker_and_records_replace_not_shadow(tmp_path: Path) -> None:
    raw = _json_raw()
    canary = _hooks(
        tmp_path / "canary",
        NativeContentSlimmerConfig(
            enabled=True,
            mode="shadow",
            compression_mode="canary",
            compression_canary_percent=100.0,
            min_bytes=100,
            preview_bytes=120,
        ),
        breaker=_healthy_breaker(("web_extract", "json", "json_compact")),
    )

    marker = canary.transform_tool_result(
        tool_name="web_extract",
        result=raw,
        session_id="s-canary",
        tool_call_id="c",
    )

    parsed = parse_marker(marker or "")
    assert parsed is not None
    assert parsed.fields["strategy"] == "json_compact"
    row = canary.telemetry_records[-1]
    assert row["mode"] == "active_lossless"
    assert row["action"] == "replace"
    assert canary.shadow_records == []


def test_healthy_breaker_active_compression_and_shadow_rows(tmp_path: Path) -> None:
    raw = _json_raw()
    healthy = _healthy_breaker(("web_extract", "json", "json_compact"))
    active = _hooks(
        tmp_path / "active",
        NativeContentSlimmerConfig(enabled=True, mode="active_lossless", compression_mode="active", min_bytes=100, preview_bytes=120),
        breaker=healthy,
    )
    active_marker = parse_marker(active.transform_tool_result(tool_name="web_extract", result=raw, session_id="s-active", tool_call_id="c") or "")
    assert active_marker is not None
    assert active_marker.fields["strategy"] == "json_compact"

    shadow = _hooks(
        tmp_path / "shadow",
        NativeContentSlimmerConfig(enabled=True, mode="shadow", compression_mode="shadow", min_bytes=100, preview_bytes=120),
    )
    assert shadow.transform_tool_result(tool_name="web_extract", result=raw, session_id="s-shadow", tool_call_id="c") is None
    row = shadow.telemetry_records[-1]
    assert row["action"] == "would_replace"
    assert row["strategy"] == "json_compact"
    assert row["view_bytes"] < row["original_bytes"]

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    nss.ensure_schema(conn)
    nss.insert_event(row, created_at=1.0, conn=conn)
    persisted = dict(conn.execute(f"SELECT strategy, view_bytes, lossy_view, expansions_triggered FROM {nss.TABLE}").fetchone())
    assert persisted == {
        "strategy": "json_compact",
        "view_bytes": row["view_bytes"],
        "lossy_view": 1,
        "expansions_triggered": 0,
    }

    nss.record_expansion(
        session_id=row["session_id"],
        tool_call_id=row["tool_call_id"],
        raw_sha256=row["raw_sha256"],
        artifact_id=row["artifact_id"],
        conn=conn,
    )
    assert dict(conn.execute(f"SELECT expansions_triggered FROM {nss.TABLE}").fetchone()) == {"expansions_triggered": 1}
