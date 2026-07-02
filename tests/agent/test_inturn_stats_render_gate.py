"""P1 — announce-render gate for the in-turn compaction-stats block (2026-07-02 spec).

The in-turn stats build (build_inturn_stats + gross-error guard + COMPACTION_STATS_*
WARNINGs) used to run for EVERY LCM compress() call — including no-ops — while the
announce formatter default-denies non-rendering statuses (noop/idle/running/bypassed,
and conditional statuses whose post<pre render-condition fails). Result: degrade
markers (APPROX_ATTRIBUTION at ~100%, TAG_MISSING) fired for announces that never
render — pure log noise that polluted the compaction-stats watcher's daily report.

The gate: stats are built ONLY when the announce will render, reusing the formatter's
own allow-lists + render-condition (single source of truth). Scoped to the LCM branch —
the built-in compressor path keeps its current behavior (its gating is sid-rotation
logic and it may not expose _last_compression_status).

Spec: plans/2026-07-02_inturn-noop-gate-multipass-provenance-SPEC.md (P1 / D-1 / §5A).
"""
from __future__ import annotations

import ast
import inspect
import logging
import os
import re

import agent.conversation_compression as cc_mod
from agent.conversation_compression import (
    _ANNOUNCE_STATUS_CONDITIONAL,
    _ANNOUNCE_STATUS_UNCONDITIONAL,
    _format_compaction_announce,
    _inturn_stats_render_eligible,
    _warn_compaction_stats_once,
)


# ────────────────────────── the gate predicate itself ──────────────────────────

def test_gate_denies_noop_idle_running_bypassed():
    for status in ("noop", "idle", "running", "bypassed", None, "unknown-future"):
        assert not _inturn_stats_render_eligible(status, 100, 50), status


def test_gate_allows_unconditional_statuses():
    for status in _ANNOUNCE_STATUS_UNCONDITIONAL:
        # unconditional render regardless of token relation
        assert _inturn_stats_render_eligible(status, None, None), status
        assert _inturn_stats_render_eligible(status, 50, 100), status


def test_gate_conditional_requires_post_lt_pre():
    for status in _ANNOUNCE_STATUS_CONDITIONAL:
        assert _inturn_stats_render_eligible(status, 100, 50), status      # post < pre
        assert not _inturn_stats_render_eligible(status, 100, 100), status  # post == pre
        assert not _inturn_stats_render_eligible(status, 50, 100), status   # post > pre
        assert not _inturn_stats_render_eligible(status, None, 50), status  # missing pre
        assert not _inturn_stats_render_eligible(status, 100, None), status  # missing post
        assert not _inturn_stats_render_eligible(status, 0, 0), status       # falsy


def test_gate_and_formatter_agree_for_every_status():
    """Contract: the gate is render-eligibility — for every status the formatter
    would default-deny (LCM branch), the gate must deny; for every status it
    renders, the gate must allow. Uses the REAL formatter as the oracle."""
    statuses = (
        list(_ANNOUNCE_STATUS_UNCONDITIONAL)
        + list(_ANNOUNCE_STATUS_CONDITIONAL)
        + ["noop", "idle", "running", "bypassed", None]
    )
    for status in statuses:
        for pre, post in ((100, 50), (50, 100), (None, None)):
            rendered = _format_compaction_announce(
                engine_name="lcm",
                status=status,
                old_session_id="a",
                new_session_id="b",
                old_messages=10,
                new_messages=5,
                pre_tokens=pre,
                post_tokens=post,
                model="m",
                provider="p",
            )
            eligible = _inturn_stats_render_eligible(status, pre, post)
            assert (rendered is not None) == eligible, (status, pre, post)


def test_gate_uses_formatter_allowlist_objects():
    """Drift guard: the gate's source references the SAME module-level allow-list
    names the formatter uses (not copied literals)."""
    src = inspect.getsource(_inturn_stats_render_eligible)
    assert "_ANNOUNCE_STATUS_UNCONDITIONAL" in src
    assert "_ANNOUNCE_STATUS_CONDITIONAL" in src
    # and no hardcoded status literals that could drift
    for literal in ("compacted", "overflow_recovery", "degraded_fail_open", "sanitized"):
        assert f'"{literal}"' not in src, f"hardcoded status literal {literal!r} in gate"


def test_stats_gate_token_identity_and_lcm_scope():
    """Source-structure contract (D-1): at the call site the gate consumes the exact
    variables passed to _emit_compaction_announce as pre_tokens/post_tokens
    (_pre_request_est / _compressed_est), and the gate call sits inside the
    `_engine_name == "lcm"` scope (non-LCM never gated)."""
    src = inspect.getsource(cc_mod)
    m = re.search(
        r"_inturn_stats_eligible\s*=\s*\(\s*_engine_name\s*==\s*\"lcm\"\s*\)\s*and\s*"
        r"_inturn_stats_render_eligible\(\s*_status,\s*"
        r"locals\(\)\.get\(\"_pre_request_est\"\),\s*_compressed_est,?\s*\)",
        src,
    )
    assert m, "gate call-site must be LCM-scoped and consume _pre_request_est/_compressed_est"


# ────────────────────────── behavior through the announce block ──────────────────────────

class _FakeLCMCompressor:
    name = "lcm"

    def __init__(self, status):
        self._last_compression_status = status
        self.compression_count = 1
        self.protect_last_n = 4

    def _sanitize_active_context_messages(self, msgs, **kw):
        return list(msgs)


def _run_announce_block(monkeypatch, caplog, status, messages=None, compressed=None):
    """Drive the in-turn stats decision the way conversation_compression does:
    gate → (skip | build). Mirrors the call-site shape without a full Agent."""
    from agent.compaction_stats import build_inturn_stats
    from agent.model_metadata import estimate_messages_tokens_rough as _est

    class _Agent:
        session_id = "S-test"

    agent = _Agent()
    cc = _FakeLCMCompressor(status)
    messages = messages if messages is not None else [
        {"role": "user", "content": "u" * 200},
        {"role": "assistant", "content": "a" * 200},
    ] * 6
    compressed = compressed if compressed is not None else list(messages)

    eligible = cc_mod._inturn_stats_render_eligible(
        status, _est(messages), _est(compressed)
    ) if status in _ANNOUNCE_STATUS_CONDITIONAL else cc_mod._inturn_stats_render_eligible(
        status, 100, 50
    )
    stats = None
    with caplog.at_level(logging.WARNING):
        if eligible:
            cand = build_inturn_stats(
                messages=messages,
                compressed=compressed,
                estimator=_est,
                engine_is_lcm=True,
                sanitize=cc._sanitize_active_context_messages,
                fresh_tail_count=cc.protect_last_n,
                on_tag_missing=lambda: _warn_compaction_stats_once(
                    agent, "COMPACTION_STATS_TAG_MISSING in-turn"
                ),
            )
            ok, _ = cand.validate()
            stats = cand if ok else None
    return stats, caplog


def test_inturn_stats_skipped_on_noop(monkeypatch, caplog):
    stats, log = _run_announce_block(monkeypatch, caplog, "noop")
    assert stats is None
    assert "COMPACTION_STATS" not in log.text


def test_inturn_stats_built_on_compacted(monkeypatch, caplog):
    stats, _ = _run_announce_block(monkeypatch, caplog, "compacted")
    assert stats is not None  # regression: real compactions still build stats


# ────────────────────────── formatter stats=None tolerance ──────────────────────────

def test_formatter_tolerates_stats_none_on_render_eligible():
    line = _format_compaction_announce(
        engine_name="lcm",
        status="compacted",
        old_session_id="a",
        new_session_id="b",
        old_messages=100,
        new_messages=20,
        pre_tokens=50_000,
        post_tokens=9_000,
        model="m",
        provider="p",
        stats=None,
    )
    assert line, "render-eligible + stats=None must render the two-line form"
    assert "Messages:" in line or "→" in line


# ────────────────────────── D-4: marker self-identification ──────────────────────────

def test_marker_carries_session_and_src_flag(caplog):
    class _Agent:
        session_id = "20260702_120000_abcdef"

    with caplog.at_level(logging.WARNING):
        _warn_compaction_stats_once(_Agent(), "COMPACTION_STATS_TAG_MISSING in-turn")
    rec = [r for r in caplog.records if "COMPACTION_STATS_TAG_MISSING" in r.getMessage()]
    assert rec, "marker not emitted"
    msg = rec[0].getMessage()
    assert "session=20260702_120000_abcdef" in msg
    # running under pytest → PYTEST_CURRENT_TEST is set → src=test present
    assert os.environ.get("PYTEST_CURRENT_TEST")
    assert "src=test" in msg


def test_marker_session_dash_when_missing(caplog):
    class _Agent:
        session_id = None

    with caplog.at_level(logging.WARNING):
        _warn_compaction_stats_once(_Agent(), "COMPACTION_STATS_BUILD_FAILED in-turn")
    rec = [r for r in caplog.records if "COMPACTION_STATS_BUILD_FAILED" in r.getMessage()]
    assert rec and "session=-" in rec[0].getMessage()


def test_marker_throttle_key_unaffected_by_suffix(caplog):
    """The (cause, session) dedupe key must key on the ORIGINAL first-two tokens,
    not be defeated by the appended session/src fields."""
    class _Agent:
        session_id = "S1"

    a = _Agent()
    with caplog.at_level(logging.WARNING):
        _warn_compaction_stats_once(a, "COMPACTION_STATS_TAG_MISSING in-turn")
        _warn_compaction_stats_once(a, "COMPACTION_STATS_TAG_MISSING in-turn")
    hits = [r for r in caplog.records if "COMPACTION_STATS_TAG_MISSING" in r.getMessage()]
    assert len(hits) == 1, "throttle defeated"


def test_src_test_flag_fires_in_fork_test_path(caplog):
    """Pass-3 RC4: the measured polluter (test_compression_concurrent_fork.py) emits
    markers from threading.Thread workers inside the pytest process — NOT subprocesses —
    so PYTEST_CURRENT_TEST is inherited by the emitter. Prove src=test appears on a
    marker emitted from a worker thread (the actual pollution shape)."""
    import threading

    class _Agent:
        session_id = "PARENT_TEST_SESSION"

    with caplog.at_level(logging.WARNING):
        t = threading.Thread(
            target=_warn_compaction_stats_once,
            args=(_Agent(), "COMPACTION_STATS_APPROX_ATTRIBUTION in-turn degraded (x); two-line"),
            name="fork_worker",
        )
        t.start()
        t.join()
    rec = [r for r in caplog.records if "APPROX_ATTRIBUTION" in r.getMessage()]
    assert rec, "marker not emitted from thread"
    msg = rec[0].getMessage()
    assert "session=PARENT_TEST_SESSION" in msg
    assert "src=test" in msg
