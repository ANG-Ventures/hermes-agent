"""PRD #1.5 Phase 2 — BlackboxNativeSlimmerSink wired into the REAL hook.

Binds to the real ``transform_tool_result`` in active/shadow mode (pass-1 C-1),
not a buffer stand-in. Proves: (a) active persists a real row; (b) shadow persists
``would_replace`` with NULL turn_id; (c) a forced sink error rolls back to the
ORIGINAL uncompressed result with NO marker (telemetry-never-breaks +
no-unverified-marker), because the certified hook re-raises on emit failure.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from plugins.blackbox import native_slimmer_store as nss
from plugins.blackbox.native_slimmer_sink import BlackboxNativeSlimmerSink
from plugins.native_content_slimmer.config import NativeContentSlimmerConfig
from plugins.native_content_slimmer.hook import NativeContentSlimmerHooks
from plugins.native_content_slimmer.marker import MARKER_TOKEN
from plugins.native_content_slimmer.store import ArtifactStore


BIG = "ERROR boot failed at line 42\n" + ("filler line of log output\n" * 1200)


class _ConnBoundSink(BlackboxNativeSlimmerSink):
    """Sink that writes to an injected sqlite conn (so tests hit tmp, not live)."""

    def __init__(self, conn, **kw):
        super().__init__(**kw)
        self._conn = conn

    def emit(self, event):
        nss.insert_event(
            event,
            model=self.model,
            provider=self.provider,
            base_url=self.base_url,
            created_at=float(self._clock()),
            conn=self._conn,
        )
        self.records.append(dict(event))


def _hooks(tmp_path: Path, mode: str, sink) -> NativeContentSlimmerHooks:
    cfg = NativeContentSlimmerConfig(enabled=True, mode=mode, min_bytes=2000, preview_bytes=400)
    store = ArtifactStore(root=tmp_path / "artifacts")
    return NativeContentSlimmerHooks(cfg, store=store, telemetry=sink)


def _conn(tmp_path: Path):
    conn = sqlite3.connect(str(tmp_path / "turns.db"))
    conn.row_factory = sqlite3.Row
    nss.ensure_schema(conn)
    return conn


def test_active_persists_real_row_and_returns_marker(tmp_path: Path):
    conn = _conn(tmp_path)
    sink = _ConnBoundSink(conn, model="claude-opus-4-8", provider="claude-api-proxy")
    h = _hooks(tmp_path, "active_lossless", sink)
    out = h.transform_tool_result(
        tool_name="web_extract", result=BIG, status="success",
        session_id="sess1", tool_call_id="call1",
    )
    assert out is not None and MARKER_TOKEN in out  # active returns the marker
    rows = nss.fetch_between(0, time.time() + 1, conn=conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "replace"
    assert r["model"] == "claude-opus-4-8"
    assert r["saved_vs_status_quo_bytes"] > 0
    assert r["original_bytes"] > r["emitted_bytes"]


def test_shadow_persists_would_replace(tmp_path: Path):
    conn = _conn(tmp_path)
    sink = _ConnBoundSink(conn)
    h = _hooks(tmp_path, "shadow", sink)
    out = h.transform_tool_result(
        tool_name="web_extract", result=BIG, status="success",
        session_id="sess2", tool_call_id="call2",
    )
    assert out is None  # shadow does NOT replace the result in context
    rows = nss.fetch_between(0, time.time() + 1, conn=conn)
    assert len(rows) == 1
    assert rows[0]["action"] == "would_replace"
    assert rows[0]["turn_id"] is None  # shadow may precede the turn record


def test_sink_failure_rolls_back_to_original_no_marker(tmp_path: Path):
    """The load-bearing fail-open gate (pass-1 C-1): a sink that raises must leave
    the result as the ORIGINAL uncompressed payload, not a marker."""

    class _BoomSink:
        records: list = []

        def emit(self, event):
            raise RuntimeError("blackbox down")

    h = _hooks(tmp_path, "active_lossless", _BoomSink())
    out = h.transform_tool_result(
        tool_name="web_extract", result=BIG, status="success",
        session_id="sess3", tool_call_id="call3",
    )
    # Hook re-raises on emit failure → outer handler returns None → caller keeps
    # the original. The hook returns None (no replacement), NOT a marker.
    assert out is None
    assert MARKER_TOKEN not in (out or "")
    # and the failure was recorded (telemetry_emit_failed), turn not broken
    assert any("telemetry_emit_failed" in s for s in h.skip_reasons) or h.failures


def test_retention_prune_via_real_gc_cadence(tmp_path: Path, monkeypatch):
    """D-8 / pass-3 B-2: prune fires under the DEFAULT gc cadence, not a forced call.
    Drive >= artifact_gc_after_write_every real writes; old rows vanish.

    Point the store's _db_path at the tmp DB so BOTH the sink writes and the
    hook's prune (which opens its own conn) hit the same tmp turns.db — the
    realistic wiring, no global hacks.
    """

    db = tmp_path / "turns.db"
    monkeypatch.setattr(nss, "_db_path", lambda: db)
    # seed an old row through the real (now tmp-pointed) path
    from plugins.blackbox.native_slimmer_schema import (
        build_native_slimmer_event, RAW_SOURCE_TOOL_RESULT_RETURNED,
    )
    old = time.time() - 40 * 86400
    ev = build_native_slimmer_event(
        mode="shadow", action="would_replace", tool_name="web_extract",
        session_id="old", tool_call_id="old", artifact_id="old", raw_sha256="old",
        raw_source=RAW_SOURCE_TOOL_RESULT_RETURNED, original_bytes=9000, emitted_bytes=100,
        classification_reason="x",
    )
    ev["savings_key"] = "OLD"
    nss.insert_event(ev, created_at=old)  # own conn → tmp db via patched _db_path
    assert nss.count_rows() == 1

    cfg = NativeContentSlimmerConfig(
        enabled=True, mode="shadow", preview_bytes=400,
        artifact_gc_after_write_every=3, savings_retention_days=30,
    )
    store = ArtifactStore(tmp_path / "artifacts")
    # real Blackbox sink (writes via patched _db_path too)
    sink = BlackboxNativeSlimmerSink()
    h = NativeContentSlimmerHooks(cfg, store=store, secret=b"phase2-secret", telemetry=sink)

    for i in range(3):
        h.transform_tool_result(
            tool_name="web_extract", result=BIG, status="success",
            session_id=f"s{i}", tool_call_id=f"c{i}",
        )
    # the OLD row is pruned by the cadence; the 3 fresh rows remain
    keys = {r["savings_key"] for r in nss.fetch_between(0, time.time() + 1)}
    assert "OLD" not in keys
    assert len(keys) == 3

