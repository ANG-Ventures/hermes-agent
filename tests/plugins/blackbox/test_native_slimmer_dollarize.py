"""PRD #1.5 Phase 3 — dollarization + multi-model partial reconciliation (NET-NEW)."""

from __future__ import annotations

from plugins.blackbox.native_slimmer_dollarize import dollarize_rollup, price_saved_tokens
from plugins.blackbox.native_slimmer_schema import (
    build_native_slimmer_event,
    RAW_SOURCE_TOOL_RESULT_RETURNED,
)


def _row(action: str, *, model, provider, saved_sq_bytes: int, key: str) -> dict:
    ev = build_native_slimmer_event(
        mode="active_lossless" if action == "replace" else "shadow",
        action=action,
        tool_name="web_extract",
        session_id="s",
        tool_call_id="c",
        artifact_id="a",
        raw_sha256="h",
        raw_source=RAW_SOURCE_TOOL_RESULT_RETURNED,
        original_bytes=20000 + saved_sq_bytes,
        emitted_bytes=20000,
        classification_reason="large",
        status_quo_baseline_bytes=20000 + saved_sq_bytes,
    )
    ev["savings_key"] = key
    ev["model"] = model
    ev["provider"] = provider
    ev["base_url"] = None
    return ev


def test_price_metered_model_estimated():
    usd, status = price_saved_tokens(1_000_000, model="gpt-4o", provider="openai")
    assert usd is not None and usd > 0
    assert status == "estimated"  # synthetic input-only usage is an estimate, never "actual"


def test_price_unknown_model():
    usd, status = price_saved_tokens(1_000_000, model="totally-fake-xyz", provider="nobody")
    assert usd is None and status == "unknown"


def test_price_no_model_unknown():
    usd, status = price_saved_tokens(1_000_000, model=None)
    assert usd is None and status == "unknown"


def test_rollup_splits_saved_vs_would_save():
    rows = [
        _row("replace", model="gpt-4o", provider="openai", saved_sq_bytes=400000, key="r1"),
        _row("would_replace", model="gpt-4o", provider="openai", saved_sq_bytes=800000, key="w1"),
    ]
    out = dollarize_rollup(rows)
    assert out["saved"]["event_count"] == 1
    assert out["would_save"]["event_count"] == 1
    # never conflated: distinct token totals
    assert out["saved"]["saved_vs_status_quo_tokens_est"] != out["would_save"]["saved_vs_status_quo_tokens_est"]
    assert out["saved"]["saved_usd_vs_status_quo"] > 0
    assert out["saved"]["price_status"] == "estimated"


def test_multimodel_partial_reconcile():
    # one priced model + one off-table model in the SAME day → partial, not whole-day "—"
    rows = [
        _row("replace", model="gpt-4o", provider="openai", saved_sq_bytes=400000, key="ok"),
        _row("replace", model="fake-model-xyz", provider="nobody", saved_sq_bytes=400000, key="bad"),
    ]
    out = dollarize_rollup(rows)
    saved = out["saved"]
    assert saved["event_count"] == 2
    assert saved["saved_usd_vs_status_quo"] is not None  # NOT whole-day "—"
    assert saved["saved_usd_vs_status_quo"] > 0          # the priced row counted
    assert saved["price_status"] == "partial"
    assert saved["unpriced_count"] == 1


def test_all_unknown_renders_dash_not_zero():
    rows = [_row("replace", model="fake-xyz", provider="nobody", saved_sq_bytes=400000, key="x")]
    out = dollarize_rollup(rows)
    assert out["saved"]["saved_usd_vs_status_quo"] is None  # "—", not 0
    assert out["saved"]["price_status"] == "unknown"


def test_dedupe_by_savings_key():
    rows = [
        _row("replace", model="gpt-4o", provider="openai", saved_sq_bytes=400000, key="dup"),
        _row("replace", model="gpt-4o", provider="openai", saved_sq_bytes=400000, key="dup"),
    ]
    out = dollarize_rollup(rows)
    assert out["saved"]["event_count"] == 1  # counted once


def test_empty_day_zero_not_crash():
    out = dollarize_rollup([])
    assert out["saved"]["event_count"] == 0
    assert out["would_save"]["event_count"] == 0
    assert out["saved"]["saved_usd_vs_status_quo"] == 0.0
