"""Auxiliary-model calls are ledgered in turn_api_calls as lane_family='aux' (t_39628ae3).

They must be measurable per aux lane (provider, model, cache fields) without
touching the main lane's cache statistics or the turn's totals.
"""
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from plugins import blackbox
from plugins.blackbox import store
from plugins.blackbox.record import TurnRecord
from agent import aux_accounting, auxiliary_client, title_generator


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(blackbox, "_config", lambda: {"enabled": True})
    with store._connect():
        pass
    return store._db_path()


def _gemini_response(prompt=1000, cached=600, out=20):
    return SimpleNamespace(
        model="gemini-2.5-flash",
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=out,
                              total_tokens=prompt + out,
                              prompt_tokens_details=SimpleNamespace(cached_tokens=cached)),
    )


@pytest.fixture
def fake_impl(monkeypatch):
    """Stand in for the provider round-trip only; call_llm's own bookkeeping runs."""
    def impl(**kwargs):
        route = kwargs.get("route_info")
        route["provider"] = "gemini-bridge"
        route["model"] = "gemini-2.5-flash"
        return _gemini_response()
    monkeypatch.setattr(auxiliary_client, "_call_llm_impl", impl)


def _rows(db):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT turn_id, seq, provider, model, sub_key, attribution, lane_family, "
            "input_tokens, cache_read, cache_write, output_tokens FROM turn_api_calls "
            "ORDER BY turn_id, seq").fetchall()


def _agent():
    return SimpleNamespace(provider="claude-bpr", model="main", api_mode="anthropic_messages")


def test_one_aux_call_in_a_turn_lands_one_aux_row(db, fake_impl):
    agent = _agent()
    token = aux_accounting.set_blackbox_turn(agent, "turn-1")
    try:
        auxiliary_client.call_llm(task="compression", messages=[{"role": "user", "content": "x"}])
    finally:
        aux_accounting.reset_blackbox_turn(token)
    assert _rows(db) == [("turn-1", 0, "gemini-bridge", "gemini-2.5-flash", None,
                          "aux:compression", "aux", 400, 600, 0, 20)]


@pytest.mark.asyncio
async def test_async_aux_call_is_ledgered_too(db, monkeypatch):
    async def impl(**kwargs):
        kwargs["route_info"].update(provider="gemini-bridge", model="gemini-2.5-flash")
        return _gemini_response()
    monkeypatch.setattr(auxiliary_client, "_async_call_llm_impl", impl)
    token = aux_accounting.set_blackbox_turn(_agent(), "turn-a")
    try:
        await auxiliary_client.async_call_llm(task="web_extract", messages=[])
    finally:
        aux_accounting.reset_blackbox_turn(token)
    assert [r[5:7] for r in _rows(db)] == [("aux:web_extract", "aux")]


def test_no_row_outside_a_turn_or_for_moa_or_streams(db, fake_impl):
    auxiliary_client.call_llm(task="compression", messages=[])  # no turn bound
    token = aux_accounting.set_blackbox_turn(_agent(), "turn-2")
    try:
        auxiliary_client.call_llm(task="moa_reference", messages=[])
    finally:
        aux_accounting.reset_blackbox_turn(token)
    assert _rows(db) == []


def test_aux_shares_the_turn_seq_allocator_with_main_calls(db, fake_impl):
    from agent.chat_completion_helpers import _record_successful_api_call

    agent = _agent()
    agent.provider = "openai-codex"
    agent._current_turn_id = "turn-3"
    _record_successful_api_call(agent, SimpleNamespace(
        usage=SimpleNamespace(input_tokens=10, output_tokens=1)))
    token = aux_accounting.set_blackbox_turn(agent, "turn-3")
    try:
        auxiliary_client.call_llm(task="vision", messages=[])
    finally:
        aux_accounting.reset_blackbox_turn(token)
    assert [(r[1], r[6]) for r in _rows(db)] == [(0, "codex"), (1, "aux")]


def _main_call(turn_id, seq, *, cache_read, cache_write, tier_1h):
    usage = SimpleNamespace(input_tokens=10, output_tokens=5,
                            cache_read_input_tokens=cache_read,
                            cache_creation_input_tokens=cache_write,
                            cache_creation={"ephemeral_5m_input_tokens": 0,
                                            "ephemeral_1h_input_tokens": tier_1h})
    blackbox.record_api_call(turn_id=turn_id, seq=seq, ts=100 + seq, provider="claude-bpr",
                             model="m", usage=usage, api_mode="anthropic_messages",
                             sub_key="sub-vps-1", attribution="wire", http_status=200,
                             relay_synthetic=False, route_id=None)


def test_main_lane_miss_rate_and_tiers_unchanged_by_aux_calls(db):
    # A COLD aux call lands first in the turn (seq 0, all cache_write, a 1h tier
    # split) ahead of a WARM main-lane call. Control: same main call, no aux.
    cold_aux = SimpleNamespace(input_tokens=5, output_tokens=1,
                               cache_creation_input_tokens=9000,
                               cache_creation={"ephemeral_5m_input_tokens": 0,
                                               "ephemeral_1h_input_tokens": 9000})
    blackbox.record_api_call(turn_id="with_aux", seq=0, ts=99, provider="claude-bpr",
                             model="haiku", usage=cold_aux, api_mode="anthropic_messages",
                             sub_key=None, attribution="aux:compression", http_status=200,
                             relay_synthetic=False, route_id=None)
    _main_call("with_aux", 1, cache_read=50_000, cache_write=100, tier_1h=100)
    _main_call("control", 0, cache_read=50_000, cache_write=100, tier_1h=100)
    for tid in ("with_aux", "control"):
        store.insert_turn(TurnRecord(turn_id=tid, chat_id=tid, ts_start=1, ts_end=2))
    with sqlite3.connect(db) as conn:
        got = dict((r[0], r[1:]) for r in conn.execute(
            "SELECT turn_id, first_call_cache_miss, cache_write_1h, cache_write_5m FROM turns"))
        assert got["with_aux"] == got["control"] == (0, 100, 0)
        assert conn.execute("SELECT lane_family FROM turn_api_calls WHERE turn_id='with_aux' "
                            "ORDER BY seq").fetchall() == [("aux",), ("bpx/bpr",)]


@pytest.mark.parametrize("attribution", ["aux:", "aux", "auxiliary"])
def test_aux_attribution_needs_a_task(db, attribution):
    with pytest.raises(ValueError):
        store.insert_api_call("t", 0, ts=1.0, provider="p", model="m",
                              usage=__import__("agent.usage_pricing", fromlist=["x"]).CanonicalUsage(),
                              sub_key=None, attribution=attribution)


def test_title_thread_inherits_the_turn_binding(monkeypatch):
    seen = []
    done = threading.Event()

    def fake_auto_title(*args, **kwargs):
        seen.append(aux_accounting.get_blackbox_turn())
        done.set()

    monkeypatch.setattr(title_generator, "auto_title_session", fake_auto_title)
    monkeypatch.setattr(title_generator, "_auto_title_enabled", lambda: True)
    monkeypatch.setattr(title_generator, "is_titleable_user_message", lambda m: True)
    monkeypatch.setattr(title_generator, "apply_instant_title", lambda *a, **k: None)
    agent = _agent()
    token = aux_accounting.set_blackbox_turn(agent, "turn-t")
    try:
        title_generator.maybe_auto_title(object(), "sess", "hello there", conversation_history=[])
    finally:
        aux_accounting.reset_blackbox_turn(token)
    assert done.wait(5)
    assert seen == [(agent, "turn-t")]
