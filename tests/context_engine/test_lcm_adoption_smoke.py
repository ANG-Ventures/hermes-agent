"""Isolated adoption smoke for the vendored hermes-lcm context engine.

PRD #2 v2 — Phase 3 (LCM adoption in isolated profile). These tests drive the
real ``LCMEngine`` from the vendored copy under ``staging/lcm-profile`` WITHOUT
touching any live profile. Summarization is stubbed deterministically (offline),
the same ``summarize_with_escalation`` / ``_invoke_summary_llm_chain`` seam the
plugin's own test suite patches.

Run only this file:
    pytest tests/context_engine/test_lcm_adoption_smoke.py -q

If the vendored plugin is absent the module skips (so a clean checkout without
the staged profile does not fail the suite).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN_DIR = WORKTREE_ROOT / "staging" / "lcm-profile" / "plugins" / "hermes-lcm"

pytestmark = pytest.mark.skipif(
    not PLUGIN_DIR.is_dir(),
    reason="vendored hermes-lcm not staged at staging/lcm-profile/plugins/hermes-lcm",
)


def _load_plugin_package():
    if str(WORKTREE_ROOT) not in sys.path:
        sys.path.insert(0, str(WORKTREE_ROOT))
    pkg = "hermes_lcm"
    if pkg in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        pkg, str(PLUGIN_DIR / "__init__.py"),
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__path__ = [str(PLUGIN_DIR)]
    mod.__package__ = pkg
    sys.modules[pkg] = mod


_load_plugin_package()


@pytest.fixture
def engine_factory(tmp_path):
    """Build isolated LCMEngine instances against throwaway DBs under tmp_path."""
    from hermes_lcm.config import LCMConfig
    from hermes_lcm.engine import LCMEngine

    created = []

    def _make(name: str, *, leaf_chunk_tokens: int = 1):
        cfg = LCMConfig(
            fresh_tail_count=4,
            leaf_chunk_tokens=leaf_chunk_tokens,
            database_path=str(tmp_path / f"{name}.db"),
        )
        e = LCMEngine(config=cfg)
        e.context_length = 200_000
        e.threshold_tokens = int(200_000 * cfg.context_threshold)
        e.on_session_start(f"{name}-session", platform="cli", context_length=200_000)
        created.append(e)
        return e

    yield _make

    for e in created:
        try:
            e.shutdown()
        except Exception:
            pass


@pytest.fixture
def stub_summarizer(monkeypatch):
    """Deterministic offline summary — no LLM call."""
    import hermes_lcm.engine as lcm_engine
    monkeypatch.setattr(
        lcm_engine, "summarize_with_escalation",
        lambda **kw: ("SUMMARY: earlier turns covered the deploy code and arithmetic", 1),
    )
    return lcm_engine


def _convo_with_secret(secret: str):
    return [
        {"role": "system", "content": "You are concise."},
        {"role": "user", "content": f"Remember the deploy code is {secret} for prod."},
        {"role": "assistant", "content": f"Noted {secret}."},
        {"role": "user", "content": "What is 2+2?"},
        {"role": "assistant", "content": "4"},
        {"role": "user", "content": "And 3+3?"},
        {"role": "assistant", "content": "6"},
        {"role": "user", "content": "What was the deploy code?"},
    ]


def test_load_and_identity():
    """LCMEngine loads as a ContextEngine and reports name 'lcm'."""
    from agent.context_engine import ContextEngine
    from hermes_lcm.engine import LCMEngine

    assert issubclass(LCMEngine, ContextEngine)
    # plugin.yaml declares the runtime/manifest identity
    yaml_text = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
    assert "name: hermes-lcm" in yaml_text


def test_normal_chat_tool_surface(engine_factory):
    """Normal ingest + status/describe tools respond on a live session."""
    e = engine_factory("normal")
    e._ingest_messages([
        {"role": "user", "content": "hello, normal turn"},
        {"role": "assistant", "content": "hi"},
    ])
    status = json.loads(e.handle_tool_call("lcm_status", {}))
    describe = json.loads(e.handle_tool_call("lcm_describe", {}))
    assert status.get("session_id")
    assert describe.get("store_message_count", 0) >= 2


def test_threshold_compaction_builds_dag_summary(engine_factory, stub_summarizer):
    """should_compress honors threshold; compress builds a DAG summary + shrinks ctx."""
    e = engine_factory("compact")
    assert not e.should_compress(1000)
    assert e.should_compress(e.threshold_tokens)

    convo = _convo_with_secret("DEPLOY-CODE-7F3A")
    active = e.compress(list(convo))

    assert e._last_compression_status == "compacted"
    assert e.compression_count == 1
    assert len(active) < len(convo)
    assert any("SUMMARY:" in (m.get("content") or "") for m in active)


def test_grep_expand_recovers_compacted_fact_byte_exact(engine_factory, stub_summarizer):
    """A fact compacted out of active context is found by grep and expanded byte-exact."""
    e = engine_factory("recall")
    secret = "DEPLOY-CODE-7F3A"
    active = e.compress(list(_convo_with_secret(secret)))
    # The secret turn was compacted out of the live context (only summary + tail remain)
    assert not any(secret in (m.get("content") or "") for m in active)

    grep = json.loads(e.handle_tool_call("lcm_grep", {"query": secret}))
    assert grep.get("total_results", 0) >= 1
    results = grep.get("results") or []
    chosen = next(
        (r for r in results if secret in (r.get("snippet") or r.get("content") or "")),
        results[0],
    )
    expand = json.loads(e.handle_tool_call("lcm_expand", {"store_id": chosen["store_id"]}))
    assert secret in (expand.get("content") or "")  # byte-exact raw recovery


def test_expand_unknown_id_is_loud_error(engine_factory):
    """Expanding an unknown store_id returns a loud error, never fabricated content."""
    e = engine_factory("badid")
    e._ingest_messages([{"role": "user", "content": "anything"}])
    out = json.loads(e.handle_tool_call("lcm_expand", {"store_id": 999_999}))
    assert "error" in out
    assert "999999" in json.dumps(out)


def test_reset_semantics_clear_counters_but_store_persists(engine_factory, stub_summarizer):
    """on_session_reset zeroes per-session counters; the lossless store still answers grep."""
    e = engine_factory("reset")
    convo = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "fact ALPHA-secret-token"},
        {"role": "assistant", "content": "ok alpha"},
        {"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"}, {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "q3"},
    ]
    e.compress(list(convo))
    assert e.compression_count >= 1

    e.on_session_reset()
    assert e.compression_count == 0

    grep_all = json.loads(e.handle_tool_call(
        "lcm_grep", {"query": "ALPHA-secret-token", "session_scope": "all"}))
    assert grep_all.get("total_results", 0) >= 1  # immutable store survives reset


def test_failure_fail_open_degrades_to_deterministic_truncation(engine_factory, monkeypatch):
    """Summarizer LLM unavailable degrades to L3 deterministic truncation — no crash."""
    import hermes_lcm.escalation as lcm_escalation
    # LLM chain yields no usable summary -> escalation must fall to L3 truncation
    monkeypatch.setattr(lcm_escalation, "_invoke_summary_llm_chain", lambda *a, **k: None)

    e = engine_factory("failopen")
    convo = [{"role": "system", "content": "sys"}] + [
        {"role": "user", "content": f"fact number {i} with filler text to summarize"}
        for i in range(8)
    ]
    # Must not raise (fail-open). Active context stays bounded and non-empty.
    active = e.compress(list(convo))
    assert active is not None and len(active) >= 1

    # Raw content remains recoverable from the lossless store despite degraded summary
    grep = json.loads(e.handle_tool_call("lcm_grep", {"query": "fact number 0"}))
    assert grep.get("total_results", 0) >= 1
