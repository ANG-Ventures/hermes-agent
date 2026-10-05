"""Background-review mem0 write path (memory.background_review_mem0_write).

The 2026-06 version of this feature was dark from merge until it was reverted: the dispatch
whitelist admitted mem0_remember but the tool was never in the tools[] the review fork inherits,
so the model never saw it. These tests drive the real chain (config -> registry check_fn ->
toolset resolution -> fork tools[] -> fork whitelist -> registry dispatch -> mem0 REST) against a
temp HERMES_HOME, with only the HTTP layer faked.
"""

from __future__ import annotations

import json
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.skill_provenance import reset_current_write_origin, set_current_write_origin


class _Resp:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._raw


class _FakeMem0:
    """In-memory stand-in for the self-hosted mem0 REST API."""

    def __init__(self):
        self.rows: list[dict] = []
        self.calls: list[tuple[str, str, dict]] = []

    def urlopen(self, request, timeout=0, context=None):
        from urllib.parse import urlparse

        path = urlparse(request.full_url).path
        body = json.loads(request.data.decode()) if request.data else {}
        self.calls.append((request.get_method(), path, body))
        if path == "/memories" and request.get_method() == "POST":
            text = body["messages"][0]["content"]
            self.rows.append({"id": f"m{len(self.rows)}", "memory": text, "metadata": body.get("metadata") or {}})
            return _Resp({"results": [{"id": self.rows[-1]["id"], "memory": text}]})
        if path == "/search":
            if body.get("filters"):
                want = body["filters"]
                hits = [r for r in self.rows if all(r["metadata"].get(k) == v for k, v in want.items())]
            else:
                hits = list(self.rows)
            return _Resp({"results": hits[: int(body.get("top_k") or 10)]})
        raise AssertionError(f"unexpected mem0 call {request.get_method()} {path}")


@pytest.fixture
def mem0_home(tmp_path, monkeypatch):
    def make(knob: bool):
        (tmp_path / "config.yaml").write_text(
            "memory:\n  memory_enabled: true\n  provider: mem0\n"
            f"  background_review_mem0_write: {'true' if knob else 'false'}\n")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("MEM0_HOST", "http://mem0.test")
        monkeypatch.setenv("MEM0_ADMIN_API_KEY", "admin-key")
        monkeypatch.setenv("MEM0_USER_ID", "ace")
        monkeypatch.delenv("MEM0_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)  # no embeddings: text-equality fallback
        fake = _FakeMem0()
        monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
        from tools import mem0_remember_tool as mrt
        from tools.registry import invalidate_check_fn_cache
        invalidate_check_fn_cache()
        mrt._providers.clear()
        return fake
    return make


def _parent_tools():
    from model_tools import get_tool_definitions
    return get_tool_definitions(enabled_toolsets=["hermes-cli"], quiet_mode=True)


def _names(tools):
    return {t["function"]["name"] for t in tools}


def _fork_from(parent_tools):
    """Build the review fork through the real cache-parity path with the parent's tools[]."""
    from agent.background_review import build_cache_parity_fork

    parent = SimpleNamespace(
        model="m", provider="openai", platform="cli", session_id="s", tools=parent_tools,
        valid_tool_names=_names(parent_tools), _cached_system_prompt="sys", session_start=object(),
        _memory_store=None, _memory_enabled=True, _user_profile_enabled=False,
        enabled_toolsets=["hermes-cli"], disabled_toolsets=None, request_overrides={},
    )

    class Fork:
        def __init__(self, **kw):
            self.tools, self.valid_tool_names, self._memory_manager, self.context_compressor = [], set(), None, None

    with patch("run_agent.AIAgent", Fork), patch(
            "agent.background_review._resolve_review_runtime",
            return_value={"model": "m", "provider": "openai", "routed": False}):
        fork, _, _ = build_cache_parity_fork(parent, max_iterations=3)
    return fork


def _dispatch(fact, supersedes=""):
    from model_tools import handle_function_call
    token = set_current_write_origin("background_review")
    try:
        args = {"fact": fact, **({"supersedes": supersedes} if supersedes else {})}
        return json.loads(handle_function_call("mem0_remember", args))
    finally:
        reset_current_write_origin(token)


def test_knob_on_tool_is_resident_in_fork_tools_whitelisted_and_writes(mem0_home):
    fake = mem0_home(knob=True)
    parent_tools = _parent_tools()
    assert "mem0_remember" in _names(parent_tools)

    fork = _fork_from(parent_tools)
    assert "mem0_remember" in _names(fork.tools)  # visible to the model

    from agent.background_review import _review_tool_whitelist
    whitelist, _ = _review_tool_whitelist(fork, None, review_memory=True)
    assert "mem0_remember" in whitelist  # and dispatchable
    skill_only, _ = _review_tool_whitelist(fork, None, review_memory=False)
    assert "mem0_remember" not in skill_only  # a skill-nudge review gets no memory writes

    out = _dispatch("Ace prefers dense utilitarian dashboards.")
    assert out["verdict"] == "stored", out
    assert fake.rows[0]["memory"] == "Ace prefers dense utilitarian dashboards."
    assert fake.rows[0]["metadata"]["write_origin"] == "background_review"


def test_knob_off_tool_absent_everywhere(mem0_home):
    mem0_home(knob=False)
    parent_tools = _parent_tools()
    assert "mem0_remember" not in _names(parent_tools)
    fork = _fork_from(parent_tools)
    from agent.background_review import _review_tool_whitelist
    whitelist, _ = _review_tool_whitelist(fork, None, review_memory=True)
    assert "mem0_remember" not in whitelist


def test_ladder_dedups_supersedes_and_ledgers(mem0_home, tmp_path):
    fake = mem0_home(knob=True)
    first = _dispatch("Ace lives in Los Osos, CA.")
    exact = _dispatch("ace lives in  Los Osos, CA.")
    assert (first["verdict"], exact["verdict"]) == ("stored", "deduped_exact")
    assert len(fake.rows) == 1

    changed = _dispatch("Ace lives in San Luis Obispo, CA.", supersedes="Ace lives in Los Osos, CA.")
    assert changed["verdict"] == "stored_supersedes"
    assert len(fake.rows) == 2  # the old row stays; nothing is overwritten
    assert fake.rows[1]["metadata"]["supersedes"] == "Ace lives in Los Osos, CA."
    assert "Supersedes" in fake.rows[1]["memory"]

    rows = [json.loads(l) for l in (tmp_path / "state" / "background-review-mem0.jsonl").read_text().splitlines()]
    assert [r["verdict"] for r in rows] == ["stored", "deduped_exact", "stored_supersedes"]
    assert all(r["fact"] and r["profile"] and r["ts"] for r in rows)


def test_similar_fact_is_deduped_without_supersedes(mem0_home, monkeypatch):
    fake = mem0_home(knob=True)
    from plugins.memory.mem0 import Mem0MemoryProvider
    monkeypatch.setattr(Mem0MemoryProvider, "_dedup_embed", lambda self, texts, timeout=15: [[1.0, 0.0]] * len(texts))
    assert _dispatch("Ace uses Alfred, not Raycast.")["verdict"] == "stored"
    out = _dispatch("Ace uses Alfred rather than Raycast.")
    assert out["verdict"] == "deduped_similar" and out["matched"] == "Ace uses Alfred, not Raycast."
    assert len(fake.rows) == 1


def test_foreground_call_is_refused(mem0_home):
    fake = mem0_home(knob=True)
    from model_tools import handle_function_call
    out = json.loads(handle_function_call("mem0_remember", {"fact": "x is y"}))
    assert "error" in out and "mem0_conclude" in out["error"]
    assert fake.rows == []


def test_review_prompt_carries_mem0_clause_only_when_resident():
    from agent.background_review import _MEMORY_REVIEW_MEM0_CLAUSE, spawn_background_review_thread

    def agent(tools):
        return SimpleNamespace(tools=tools, session_id="s")

    on = [{"function": {"name": "mem0_remember"}}]
    _, prompt_on = spawn_background_review_thread(agent(on), [], review_memory=True, task_cfg={})
    _, prompt_off = spawn_background_review_thread(agent([]), [], review_memory=True, task_cfg={})
    _, prompt_skill = spawn_background_review_thread(agent(on), [], review_skills=True, task_cfg={})
    assert prompt_on.endswith(_MEMORY_REVIEW_MEM0_CLAUSE)
    assert _MEMORY_REVIEW_MEM0_CLAUSE not in prompt_off
    assert _MEMORY_REVIEW_MEM0_CLAUSE not in prompt_skill


def test_summary_surfaces_mem0_store_only():
    from agent.background_review import summarize_background_review_actions

    msgs = [
        {"role": "assistant", "tool_calls": [
            {"id": "a", "function": {"name": "mem0_remember", "arguments": "{\"fact\": \"f\"}"}},
            {"id": "b", "function": {"name": "mem0_remember", "arguments": "{\"fact\": \"g\"}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": json.dumps({"result": "Fact stored.", "verdict": "stored"})},
        {"role": "tool", "tool_call_id": "b", "content": json.dumps({"result": "dup", "verdict": "deduped_exact"})},
    ]
    assert summarize_background_review_actions(msgs, []) == ["Long-term memory (mem0) updated"]
