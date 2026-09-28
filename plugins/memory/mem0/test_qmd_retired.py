"""The qmd document leg is retired (qmd daemon swept 2026-08-28).

Contract: the plugin loads and recalls with no qmd key in mem0.json; a leftover
`mem0_qmd` / `qmd` block or top-level `qmd_total_deadline_s` is ignored and
produces exactly one deprecation log line; the gbrain document leg still runs.
Every test drives the real initialize() against a temp HERMES_HOME.
"""
import importlib.util
import json
import logging

import pytest

import plugins.memory.mem0 as mem0_plugin
from plugins.memory.mem0 import Mem0MemoryProvider, gbrain_recall

STALE_QMD = {
    "mem0_qmd": {"enabled": True, "url": "http://[::1]:8181/mcp",
                 "qmd_total_deadline_s": 6.0, "prefetch_limit": 3},
    "qmd_total_deadline_s": 6.0,
}


@pytest.fixture(autouse=True)
def _reset_once_flag(monkeypatch):
    monkeypatch.setattr(mem0_plugin, "_QMD_RETIRED_LOGGED", False)


def _init(monkeypatch, tmp_path, cfg):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MEM0_HOST", "http://mem0.test")
    monkeypatch.setenv("MEM0_ADMIN_API_KEY", "admin-key")
    monkeypatch.setenv("MEM0_USER_ID", "ace")
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    (tmp_path / "mem0.json").write_text(json.dumps(cfg))
    p = Mem0MemoryProvider()
    p.initialize("test-session")
    return p


def _stub_client(p, rows):
    class _Stub:
        def search(self, **kw):
            return list(rows)
    p._get_client = lambda: _Stub()
    p._drop_forgotten = lambda r: r
    p._read_filters = lambda: {}
    p._rerank = False
    p._temporal_search = False
    return p


def _prefetch(p, query):
    p.queue_prefetch(query)
    if p._prefetch_future:
        p._prefetch_future.result(timeout=5)
    return p.prefetch(query)


def _qmd_records(caplog):
    return [r for r in caplog.records if "qmd" in r.getMessage().lower()]


def test_qmd_recall_module_is_gone():
    assert importlib.util.find_spec("plugins.memory.mem0.qmd_recall") is None


def test_loads_with_no_qmd_key(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="plugins.memory.mem0")
    p = _init(monkeypatch, tmp_path, {"user_id": "ace"})
    assert _qmd_records(caplog) == []
    assert not [a for a in vars(p) if "qmd" in a]
    _stub_client(p, [{"memory": "fact one"}])
    assert _prefetch(p, "where did we decide the local dns split") == "## Mem0 Memory\n- fact one"
    reply = json.loads(p.handle_tool_call("mem0_search", {"query": "local dns split"}))
    assert "docs" not in reply


def test_stale_qmd_keys_ignored_with_one_log_line(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="plugins.memory.mem0")
    p = _init(monkeypatch, tmp_path, dict(STALE_QMD, qmd={"enabled": True}))
    _init(monkeypatch, tmp_path, dict(STALE_QMD))  # second session, same process
    recs = _qmd_records(caplog)
    assert len(recs) == 1, [r.getMessage() for r in recs]
    assert recs[0].levelno == logging.WARNING
    msg = recs[0].getMessage()
    for key in ("mem0_qmd", "qmd_total_deadline_s", "qmd"):
        assert key in msg
    # Ignored: no qmd state, no document leg, recall output unchanged.
    assert not [a for a in vars(p) if "qmd" in a]
    _stub_client(p, [{"memory": "fact one"}])
    assert _prefetch(p, "where did we decide the local dns split") == "## Mem0 Memory\n- fact one"
    reply = json.loads(p.handle_tool_call("mem0_search", {"query": "local dns split"}))
    assert "docs" not in reply


def test_gbrain_leg_still_runs_next_to_stale_qmd_key(monkeypatch, tmp_path):
    calls = {"n": 0}

    def fake_search(*a, **k):
        calls["n"] += 1
        return [{"file": "ai/dora-doorbell/dora-ai-doorbell", "title": "Dora Ai Doorbell",
                 "score": 0.859, "line": 0, "docid": "gbrain:4214"}]

    monkeypatch.setattr(gbrain_recall, "gbrain_search", fake_search)
    p = _init(monkeypatch, tmp_path, dict(STALE_QMD, mem0_gbrain={"enabled": True}))
    assert p._gbrain_prefetch_enabled and p._gbrain_search_enabled
    _stub_client(p, [{"memory": "fact one", "score": 0.9}])
    out = _prefetch(p, "where did we decide the local dns split")
    assert out.startswith("## Mem0 Memory\n- fact one\n\n## Local Docs (gbrain)")
    assert "ai/dora-doorbell/dora-ai-doorbell" in out
    reply = json.loads(p.handle_tool_call("mem0_search", {"query": "local dns split"}))
    assert reply["docs"][0]["docid"] == "gbrain:4214"
    assert calls["n"] == 2
