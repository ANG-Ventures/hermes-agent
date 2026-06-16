from __future__ import annotations

from plugins.blackbox.native_slimmer_digest import render_digest
from plugins.blackbox.native_slimmer_dollarize import dollarize_rollup
from plugins.blackbox.native_slimmer_schema import (
    RAW_SOURCE_TOOL_RESULT_RETURNED,
    build_native_slimmer_event,
)
from plugins.native_content_slimmer.breaker import ACTION_OFFLOAD


def _synthetic_compressed_row(i: int, *, expanded: bool) -> dict:
    # Small gross savings (100 tokens) plus a large recovered original (1000 tokens)
    # makes a 30% expansion lane net-negative.
    row = build_native_slimmer_event(
        mode="active_lossless",
        action="replace",
        tool_name="web_extract",
        session_id="s",
        tool_call_id=f"c{i}",
        artifact_id=f"a{i}",
        raw_sha256=f"h{i}",
        raw_source=RAW_SOURCE_TOOL_RESULT_RETURNED,
        original_bytes=4000,
        emitted_bytes=3600,
        classification_reason="compress_offload",
        status_quo_baseline_bytes=4000,
        lossy=True,
        strategy="json_compact",
        view_bytes=3600,
        lossy_view=True,
        expansions_triggered=1 if expanded else 0,
    )
    row["model"] = "gpt-4o"
    row["provider"] = "openai"
    row["base_url"] = None
    row["content_class"] = "json"
    return row


def test_high_expansion_lane_reads_net_negative_and_breaker_offloads() -> None:
    rows = [
        _synthetic_compressed_row(i, expanded=(i < 6))
        for i in range(20)
    ]

    rollup = dollarize_rollup(rows)
    saved = rollup["saved"]
    assert saved["event_count"] == 20
    assert saved["expansion_count"] == 6
    assert saved["saved_vs_status_quo_tokens_est"] < 0
    assert saved["saved_usd_vs_status_quo"] is not None
    assert saved["saved_usd_vs_status_quo"] < 0

    lanes = saved["lanes"]
    assert len(lanes) == 1
    lane = lanes[0]
    assert lane["sample_count"] == 20
    assert lane["expansion_rate"] == 0.30
    assert lane["breaker_action"] == ACTION_OFFLOAD
    assert lane["breaker_tripped"] is True
    assert lane["saved_vs_status_quo_tokens_est"] < 0

    digest = render_digest(rollup)
    assert "ACTIVE saved -" in digest
    assert "breaker=offload" in digest
    assert "expansions 6/20 (30%)" in digest
