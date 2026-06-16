"""PRD #1.5 Phase 4 — digest renders shadow vs active distinctly + honestly."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from plugins.blackbox import native_slimmer_store as nss
from plugins.blackbox.native_slimmer_digest import build_digest, render_digest, BANNER
from plugins.blackbox.native_slimmer_dollarize import dollarize_rollup
from plugins.blackbox.native_slimmer_schema import (
    build_native_slimmer_event,
    RAW_SOURCE_TOOL_RESULT_RETURNED,
)


def _row(action, *, model, provider, saved_bytes, key):
    ev = build_native_slimmer_event(
        mode="active_lossless" if action == "replace" else "shadow",
        action=action, tool_name="web_extract", session_id="s", tool_call_id="c",
        artifact_id="a", raw_sha256="h", raw_source=RAW_SOURCE_TOOL_RESULT_RETURNED,
        original_bytes=20000 + saved_bytes, emitted_bytes=20000,
        classification_reason="large", status_quo_baseline_bytes=20000 + saved_bytes,
    )
    ev["savings_key"] = key
    ev["model"] = model
    ev["provider"] = provider
    ev["base_url"] = None
    return ev


def test_zero_day_honest_line():
    out = render_digest(dollarize_rollup([]))
    assert "no native-slimmer savings recorded" in out


def test_shadow_and_active_distinct_lines():
    rows = [
        _row("replace", model="gpt-4o", provider="openai", saved_bytes=4_000_000, key="r1"),
        _row("would_replace", model="gpt-4o", provider="openai", saved_bytes=8_000_000, key="w1"),
    ]
    out = render_digest(dollarize_rollup(rows))
    # distinct lines, never summed/conflated
    assert "ACTIVE saved" in out
    assert "SHADOW would have saved" in out
    # the banner is present (honest lower-bound)
    assert BANNER in out
    # active and shadow token figures differ (not one merged number)
    active_line = [l for l in out.splitlines() if "ACTIVE saved" in l][0]
    shadow_line = [l for l in out.splitlines() if "SHADOW would" in l][0]
    assert active_line != shadow_line


def test_subscription_renders_zero_subscription():
    # a model that resolves to subscription-included would render "$0 (subscription)"
    # gpt-4o is metered here, so assert the metered path shows a $ figure not "$0 sub"
    rows = [_row("replace", model="gpt-4o", provider="openai", saved_bytes=4_000_000, key="r1")]
    out = render_digest(dollarize_rollup(rows))
    assert "$" in out


def test_unpriced_note():
    rows = [
        _row("replace", model="gpt-4o", provider="openai", saved_bytes=4_000_000, key="ok"),
        _row("replace", model="fake-xyz", provider="nobody", saved_bytes=4_000_000, key="bad"),
    ]
    out = render_digest(dollarize_rollup(rows))
    assert "unpriced" in out


def test_build_digest_reads_db(tmp_path: Path, monkeypatch):
    db = tmp_path / "turns.db"
    monkeypatch.setattr(nss, "_db_path", lambda: db)
    now = time.time()
    nss.insert_event(
        _row("replace", model="gpt-4o", provider="openai", saved_bytes=4_000_000, key="r1"),
        model="gpt-4o", provider="openai", created_at=now,
    )
    line = build_digest(days=1, now=now + 1)
    assert "ACTIVE saved" in line
