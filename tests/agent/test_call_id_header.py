"""x-hermes-call-id: per-attempt correlation id on bridge lanes (cachehop t_26d3993c).

The harness stamps a fresh id on every HTTP attempt to a claude-bpx bridge lane,
and the ledger row for that attempt carries the SAME id, so cachehop joins
turn_api_calls to the bridge journal by id rather than by timestamp.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from agent import chat_completion_helpers as cch
from agent.fork_ext import relay_headers as rh


def _agent(provider: str, turn_id: str = "turn-1"):
    return SimpleNamespace(
        _current_turn_id=turn_id,
        provider=provider,
        model="claude-opus-4-8",
        api_mode="chat_completions",
    )


@pytest.fixture
def recorded(monkeypatch):
    rows = []
    monkeypatch.setattr("plugins.blackbox.record_api_call", lambda **row: rows.append(row))
    monkeypatch.setattr(rh, "_call_id_profile", lambda: "apollo")
    return rows


def _direct(monkeypatch, fail: bool = False):
    sent = []

    def call(_agent, kwargs):
        sent.append(dict(kwargs.get("extra_headers") or {}))
        if fail:
            raise RuntimeError("boom")
        return SimpleNamespace(usage=None, pool_headers={})  # pooled lanes require the stamp

    monkeypatch.setattr(cch, "should_use_direct_api_call", lambda _agent: True)
    monkeypatch.setattr(cch, "direct_api_call", call)
    return sent


@pytest.mark.parametrize("provider", ["claude-bpx-21", "claude-bpx-0", "claude-bpr"])
def test_bridge_lane_sends_header_and_ledger_row_carries_same_id(recorded, monkeypatch, provider):
    sent = _direct(monkeypatch)
    kwargs = {"model": "m", "extra_headers": {"x-keep": "1"}}

    cch.interruptible_api_call(_agent(provider), kwargs)

    assert len(sent) == 1 and len(recorded) == 1
    cid = sent[0][rh.CALL_ID_HEADER]
    assert rh.CALL_ID_RE.fullmatch(cid) and cid.startswith("apollo:")
    assert sent[0]["x-keep"] == "1"  # merge, never clobber existing headers
    assert recorded[0]["call_id"] == cid


def test_failed_attempt_row_carries_the_id_that_was_sent(recorded, monkeypatch):
    sent = _direct(monkeypatch, fail=True)
    with pytest.raises(RuntimeError):
        cch.interruptible_api_call(_agent("claude-bpx-23"), {"model": "m"})
    assert recorded[0]["http_status"] is None
    assert recorded[0]["call_id"] == sent[0][rh.CALL_ID_HEADER]


def test_every_attempt_gets_a_fresh_id_even_when_kwargs_are_reused(recorded, monkeypatch):
    sent = _direct(monkeypatch)
    agent, kwargs = _agent("claude-bpx-21"), {"model": "m"}
    cch.interruptible_api_call(agent, kwargs)
    cch.interruptible_api_call(agent, kwargs)
    ids = [h[rh.CALL_ID_HEADER] for h in sent]
    assert len(set(ids)) == 2
    assert [r["call_id"] for r in recorded] == ids


@pytest.mark.parametrize(
    "provider", ["anthropic", "openrouter", "openai-codex", "claude-apr", "claude-apx-3", "claude-bpx"]
)
def test_vendors_and_non_bridge_lanes_never_get_the_header(recorded, monkeypatch, provider):
    sent = _direct(monkeypatch)
    # a stale id left on a reused kwargs dict (e.g. after a failover off bpx) is removed
    kwargs = {"model": "m", "extra_headers": {rh.CALL_ID_HEADER: "apollo:0123456789abcdef"}}
    cch.interruptible_api_call(_agent(provider), kwargs)
    assert rh.CALL_ID_HEADER not in sent[0]
    assert recorded[0]["call_id"] is None


def test_call_id_of_rejects_malformed_values():
    assert rh.call_id_of({"extra_headers": {rh.CALL_ID_HEADER: "apollo:0123456789abcdef"}})
    for bad in ("apollo:XYZ", "a b:0123456789abcdef", "apollo:0123456789abcdef0", 7):
        assert rh.call_id_of({"extra_headers": {rh.CALL_ID_HEADER: bad}}) is None
    assert rh.call_id_of(None) is None


def test_header_reaches_the_wire_on_a_real_openai_client(monkeypatch):
    """The stamped extra_headers actually ride the HTTP request (captured transport)."""
    import openai

    monkeypatch.setattr(rh, "_call_id_profile", lambda: "daedalus-opus")
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
        })

    client = openai.OpenAI(api_key="k", base_url="http://127.0.0.1:3556/v1",
                           http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    kwargs = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    cid = rh.stamp_call_id(_agent("claude-bpx-21"), kwargs)
    client.chat.completions.create(**kwargs)

    assert captured[0].headers[rh.CALL_ID_HEADER] == cid
    assert rh.CALL_ID_HEADER not in json.loads(captured[0].content)  # header only, never body


def test_store_persists_call_id_and_migrates_old_ledger(tmp_path, monkeypatch):
    from agent.usage_pricing import CanonicalUsage
    from plugins.blackbox import store

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = store._db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(str(path))
    old.execute(
        "CREATE TABLE turn_api_calls (turn_id TEXT NOT NULL, seq INT NOT NULL, ts REAL,"
        " provider TEXT, sub_key TEXT, model TEXT, input_tokens INT, output_tokens INT,"
        " cache_read INT, cache_write INT, reasoning INT, attribution TEXT, http_status INT,"
        " relay_synthetic INT NOT NULL DEFAULT 0, route_id TEXT, PRIMARY KEY(turn_id, seq))"
    )
    old.commit()
    old.close()

    common = dict(ts=1.0, provider="claude-bpx-21", model="m",
                  usage=CanonicalUsage(10, 1, 0, 0, 0), sub_key="claude-bpx-21",
                  attribution="pinned")
    store.insert_api_call("t", 0, call_id="apollo:0123456789abcdef", **common)
    store.insert_api_call("t", 1, **common)

    with sqlite3.connect(str(path)) as conn:
        rows = conn.execute("SELECT seq, call_id FROM turn_api_calls ORDER BY seq").fetchall()
    assert rows == [(0, "apollo:0123456789abcdef"), (1, None)]
