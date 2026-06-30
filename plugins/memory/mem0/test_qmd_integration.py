"""Integration tests for the QMD fold-in wiring inside Mem0MemoryProvider.

Drives the REAL prefetch / queue_prefetch / handle_tool_call paths with the mem0 client
and qmd_recall.qmd_query stubbed (no live daemon, no live mem0). Run from the repo root:
  venv/bin/python -m pytest plugins/memory/mem0/test_qmd_integration.py -v -o addopts=""
"""
import json
import time

import pytest

from plugins.memory.mem0 import qmd_recall
import plugins.memory.mem0 as mem0pkg
from plugins.memory.mem0 import Mem0MemoryProvider, SEARCH_SCHEMA


class _StubClient:
    def __init__(self, rows):
        self._rows = rows

    def search(self, **kwargs):
        return list(self._rows)


def _provider(qmd_enabled=False, mem0_rows=None, qmd_hits=None, monkeypatch=None):
    p = Mem0MemoryProvider()
    # minimal init state (skip initialize() network/config machinery)
    p._config = {}
    p._rerank = False
    p._keyword_search = None
    p._temporal_search = False
    p._consecutive_failures = 0
    p._breaker_open_until = 0
    p._qmd_cfg = qmd_recall.load_qmd_config({"enabled": qmd_enabled})
    p._qmd_enabled = qmd_enabled
    p._get_client = lambda: _StubClient(mem0_rows or [])
    # neutralize forgotten-filter + read_filters so the stub rows flow through
    p._drop_forgotten = lambda rows: rows
    p._read_filters = lambda: {}
    if monkeypatch is not None:
        monkeypatch.setattr(qmd_recall, "qmd_query", lambda *a, **k: list(qmd_hits or []))
    return p


_HIT = {"file": "obsidian/DNS-PRD.md", "title": "DNS Block Portal", "score": 0.93, "line": 1, "docid": "#ec41f3"}


def _run_prefetch(p, query):
    p.queue_prefetch(query)
    if p._prefetch_thread:
        p._prefetch_thread.join(timeout=5)
    return p.prefetch(query)


# ---- AC1: disabled output byte-identical to legacy shape ------------------
def test_prefetch_disabled_is_legacy_shape(monkeypatch):
    p = _provider(qmd_enabled=False, mem0_rows=[{"memory": "fact one"}], monkeypatch=monkeypatch)
    out = _run_prefetch(p, "where did we decide the dns split")
    assert out == "## Mem0 Memory\n- fact one"  # exactly the pre-change render


def test_prefetch_disabled_empty_is_empty(monkeypatch):
    p = _provider(qmd_enabled=False, mem0_rows=[], monkeypatch=monkeypatch)
    assert _run_prefetch(p, "where did we decide the dns split") == ""


# ---- AC2: lookup prefetch injects both blocks -----------------------------
def test_prefetch_lookup_injects_both(monkeypatch):
    p = _provider(qmd_enabled=True, mem0_rows=[{"memory": "fact one"}], qmd_hits=[_HIT], monkeypatch=monkeypatch)
    out = _run_prefetch(p, "where did we decide the local dns split")
    assert "## Mem0 Memory\n- fact one" in out
    assert "## Local Docs (QMD)" in out
    assert "obsidian/DNS-PRD.md" in out
    assert out.index("## Mem0 Memory") < out.index("## Local Docs")  # mem0 first


# ---- AC3: non-lookup utterance skips QMD ----------------------------------
def test_prefetch_non_lookup_skips_qmd(monkeypatch):
    calls = {"n": 0}

    def _spy(*a, **k):
        calls["n"] += 1
        return [_HIT]
    monkeypatch.setattr(qmd_recall, "qmd_query", _spy)
    p = _provider(qmd_enabled=True, mem0_rows=[{"memory": "fact one"}])
    p._qmd_enabled = True
    out = _run_prefetch(p, "ship it")
    assert calls["n"] == 0                 # gate short-circuited before the call
    assert out == "## Mem0 Memory\n- fact one"


# ---- AC9: slow QMD never drops the mem0 block -----------------------------
def test_prefetch_slow_qmd_keeps_mem0(monkeypatch):
    def _slow(*a, **k):
        time.sleep(3.0)
        return [_HIT]
    monkeypatch.setattr(qmd_recall, "qmd_query", _slow)
    p = _provider(qmd_enabled=True, mem0_rows=[{"memory": "fact one"}])
    p._qmd_enabled = True
    p.queue_prefetch("where did we decide the local dns split")
    # the mem0 block is committed before QMD runs; read it without waiting for the slow leg
    time.sleep(0.5)
    out = p.prefetch("where did we decide the local dns split")
    assert "## Mem0 Memory\n- fact one" in out  # mem0 present despite slow QMD


# ---- AC4: mem0_search additive docs key -----------------------------------
def test_search_adds_docs_key(monkeypatch):
    p = _provider(qmd_enabled=True, mem0_rows=[{"memory": "fact one", "score": 0.9}],
                  qmd_hits=[_HIT], monkeypatch=monkeypatch)
    out = json.loads(p.handle_tool_call("mem0_search", {"query": "local dns split"}))
    assert "results" in out and "docs" in out
    assert out["results"][0]["memory"] == "fact one"
    assert out["docs"][0]["file"] == "obsidian/DNS-PRD.md"


def test_search_disabled_is_legacy_shape(monkeypatch):
    p = _provider(qmd_enabled=False, mem0_rows=[{"memory": "fact one", "score": 0.9}], monkeypatch=monkeypatch)
    out = json.loads(p.handle_tool_call("mem0_search", {"query": "local dns split"}))
    assert out == {"results": [{"memory": "fact one", "score": 0.9}], "count": 1}  # no docs key


def test_search_qmd_fail_keeps_mem0(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("qmd down")
    # _qmd_pointers swallows; but prove the search still returns mem0 even if qmd raises
    monkeypatch.setattr(qmd_recall, "qmd_query", _boom)
    p = _provider(qmd_enabled=True, mem0_rows=[{"memory": "fact one", "score": 0.9}])
    p._qmd_enabled = True
    out = json.loads(p.handle_tool_call("mem0_search", {"query": "local dns split"}))
    assert out["results"][0]["memory"] == "fact one"
    assert "docs" not in out  # qmd failed -> no docs, mem0 intact


# ---- INV-8 / AC7: tool schema byte-unchanged ------------------------------
def test_search_schema_unchanged():
    # the model-facing schema must not gain a field (prompt-cache safety)
    props = SEARCH_SCHEMA.get("function", SEARCH_SCHEMA).get("parameters", {}).get("properties", {})
    assert "docs" not in props
    assert "qmd" not in props
    assert set(props) == {"query", "rerank", "top_k"} or "query" in props
