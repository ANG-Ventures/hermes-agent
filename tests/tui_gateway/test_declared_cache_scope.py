"""session.create ``cache_scope`` + per-call usage ledger (clanker-v2 L3, t_c16a5ab2).

The clanker voice warm pool opens a fresh tui_gateway session per room/restart.
The Codex ``prompt_cache_key`` was scoped by the physical session id, so the
first model call of every new session was cache-cold even though instructions
and tools were byte-identical (live 2026-09-30: every single-call turn read
0 cached tokens; calls 2+ in the same session read 71-95%). A declared scope
lets those sessions share one cache bucket; the usage payload carries exact
cache reads, the served tier, and a per-call ledger so the client can prove it.
"""
from __future__ import annotations

import collections
import types

from agent.prompt_cache_scope import resolve_prompt_cache_scope
from agent.transports.codex import _cache_scope_from_session_id, _content_cache_key
from tui_gateway import methods_session, server


def _agent(sid, **extra):
    return types.SimpleNamespace(session_id=sid, _session_db=None, **extra)


class TestDeclaredScope:
    def test_declared_scope_wins_over_physical_id(self):
        a = _agent("20260930_121439_26aef2", _declared_cache_scope="declared:clanker:kitchen")
        b = _agent("20260930_123257_c5ea42", _declared_cache_scope="declared:clanker:kitchen")
        assert resolve_prompt_cache_scope(a) == resolve_prompt_cache_scope(b) == "declared:clanker:kitchen"

    def test_same_declared_scope_same_codex_key_across_sessions(self):
        instructions, tools = "SOUL", [{"type": "function", "name": "hacr"}]
        keys = {
            _content_cache_key(instructions, tools, _cache_scope_from_session_id(
                resolve_prompt_cache_scope(_agent(sid, _declared_cache_scope="declared:x"))))
            for sid in ("s1", "s2", "s3")
        }
        assert len(keys) == 1

    def test_undeclared_sessions_keep_isolated_keys(self):
        keys = {_content_cache_key("SOUL", None, resolve_prompt_cache_scope(_agent(sid)))
                for sid in ("s1", "s2")}
        assert len(keys) == 2

    def test_param_is_bounded_and_printable(self):
        f = methods_session._declared_cache_scope_param
        assert f(None) is None and f("") is None and f("   ") is None and f(7) is None
        assert f("clanker warm\nkitchen") == "declared:clankerwarmkitchen"
        assert len(f("x" * 500)) == len("declared:") + 96

    def test_stamp_carries_scope_onto_rebuilt_agent(self):
        agent = types.SimpleNamespace()
        server._stamp_declared_cache_scope({"cache_scope": "declared:k"}, agent)
        assert agent._declared_cache_scope == "declared:k"
        bare = types.SimpleNamespace()
        server._stamp_declared_cache_scope({}, bare)
        assert not hasattr(bare, "_declared_cache_scope")


class TestUsageLedger:
    def _agent(self, **extra):
        base = dict(model="gpt-6-astra", session_input_tokens=10799, session_prompt_tokens=27695,
                    session_output_tokens=123, session_api_calls=3,
                    session_cache_read_tokens=16896, context_compressor=None,
                    _api_latency_history=collections.deque(maxlen=10),
                    _api_output_history=collections.deque(maxlen=10))
        base.update(extra)
        return types.SimpleNamespace(**base)

    def test_exact_cache_read_tier_and_ledger(self):
        ledger = collections.deque([
            {"prompt": 7215, "cached": 0, "output": 37, "latency_ms": 2100, "service_tier": "ultrafast"},
            {"prompt": 9955, "cached": 7040, "output": 21, "latency_ms": 2600, "service_tier": "ultrafast"},
        ], maxlen=64)
        usage = server._get_usage(self._agent(_served_service_tier="ultrafast", _api_call_ledger=ledger))
        assert usage["cache_read"] == 16896
        assert usage["service_tier"] == "ultrafast"
        assert usage["call_ledger"][1]["cached"] == 7040
        assert usage["call_ledger"] is not ledger  # a copy, never the live deque

    def test_zero_cache_reads_are_reported_as_zero_not_omitted(self):
        usage = server._get_usage(self._agent(session_cache_read_tokens=0))
        assert usage["cache_read"] == 0
        assert "service_tier" not in usage  # unknown stays absent
