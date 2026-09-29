"""MoA composite calls: one virtual parent row + one child row per physical call.

Card t_02323499. A MoA turn's physical advisor + aggregator calls are ledgered
in ``turn_api_calls`` at their REAL provider/model/route, each pointing at the
virtual ``provider='moa'`` row through ``parent_call_id``, all tagged
``sub_harness='moa:<preset>'``. The virtual parent keeps the summed usage so a
parent-only reader and a physical-only reader total the same tokens, and a
reader that filters on the nesting key counts every token exactly once.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from agent import chat_completion_helpers as cch
from agent.conversation_loop import _build_moa_pricing_calls
from agent.usage_pricing import CanonicalUsage
from plugins import blackbox
from plugins.blackbox import sentinel, store

TURN = "sess:task:moa-composite"
_USAGE_COLS = ("input_tokens", "output_tokens", "cache_read", "cache_write", "reasoning")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(blackbox, "_config", lambda: {
        "enabled": True, "alerts_enabled": False, "record_subagents": True,
        "retention_days": 3650, "prefix_guard": False,
    })
    store._connect().close()
    return store._db_path()


def _agent():
    return SimpleNamespace(
        provider="moa", model="default", base_url="", api_mode="chat_completions",
        session_id="sess", _current_turn_id=TURN,
    )


def _advisor(model, provider, inp, out, cr=0, cw=0, reasoning=0, **extra):
    return {
        "model": model, "provider": provider, "base_url": None,
        "input_tokens": inp, "output_tokens": out, "cache_read_tokens": cr,
        "cache_write_tokens": cw, "reasoning_tokens": reasoning,
        "cost_usd": None, "cost_status": None, "http_status": 200,
        "pool_headers": None, **extra,
    }


def _moa_turn(agent):
    """Parent via the real transport chokepoint, children via the real emitter."""
    aggregator = CanonicalUsage(input_tokens=400, output_tokens=40,
                                cache_read_tokens=1000, cache_write_tokens=50)
    response = SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=1450, completion_tokens=40, total_tokens=1490,
        prompt_tokens_details=SimpleNamespace(cached_tokens=1000, cache_write_tokens=50),
    ))
    cch._record_successful_api_call(agent, response)
    advisors = [
        _advisor("gpt-6-astra-900k", "openai-codex", 1000, 100, cr=200, reasoning=30),
        _advisor("moonshotai/kimi-k3", "openrouter", 900, 90),
        _advisor("grok-4.7", "xai-oauth", 800, 80, cr=100),
    ]
    calls = _build_moa_pricing_calls(
        advisors, aggregator, aggregator_model="claude-fable-5-1",
        aggregator_provider="claude-bpr", aggregator_base_url=None,
    )
    calls[-1]["pool_headers"] = {"x-pool-served-by": "sub-vps-3"}
    n = cch._emit_composite_api_call_records(agent, calls, sub_harness="moa:default")
    return calls, n


def _rows(db):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM turn_api_calls WHERE turn_id = ? ORDER BY seq", (TURN,))]


def _sum(rows):
    return {c: sum(int(r[c] or 0) for r in rows) for c in _USAGE_COLS}


def test_three_advisors_plus_aggregator_yield_five_rows(db):
    agent = _agent()
    calls, n = _moa_turn(agent)
    rows = _rows(db)
    assert n == 4
    assert len(rows) == 5
    parent, children = rows[0], rows[1:]
    # Parent: the virtual preset identity, flagged composite, not nested.
    assert (parent["provider"], parent["model"]) == ("moa", "default")
    assert parent["sub_harness"] == "moa:default"
    assert parent["parent_call_id"] is None
    # Children: every physical call at its REAL route, nested under the parent.
    assert [(r["provider"], r["model"]) for r in children] == [
        (c["provider"], c["model"]) for c in calls
    ]
    assert all(r["parent_call_id"] == parent["seq"] for r in children)
    assert all(r["sub_harness"] == "moa:default" for r in children)
    assert [r["lane_family"] for r in children] == ["codex", "openrouter", "xai", "bpx/bpr"]
    assert all(r["http_status"] == 200 for r in children)
    # Relay identity: a relay pick's served-by header.
    by_provider = {r["provider"]: r for r in children}
    # An xai advisor's credential is unknown too: NULL, never the retired 'supergrok'.
    assert (by_provider["xai-oauth"]["sub_key"], by_provider["xai-oauth"]["attribution"]) == (
        None, "wire")
    assert by_provider["claude-bpr"]["sub_key"] == "sub-vps-3"
    # An advisor never ran on the agent's credential pool: codex account unknown.
    assert by_provider["openai-codex"]["sub_key"] is None
    # Children sum to the parent, class by class.
    assert _sum(children) == {c: parent[c] for c in _USAGE_COLS}
    assert parent["input_tokens"] == 400 + 1000 + 900 + 800


def test_nesting_key_counts_every_token_exactly_once(db):
    """Parent-only and physical-only views agree; counting both doubles.

    This is the double-spend guard: drop ``parent_call_id`` from the children
    and the parent-side view (``parent_call_id IS NULL``) sums parent AND
    children, so this goes RED.
    """
    agent = _agent()
    _moa_turn(agent)
    with sqlite3.connect(db) as conn:
        def total(where):
            return conn.execute(
                "SELECT COALESCE(SUM(input_tokens + output_tokens + cache_read"
                " + cache_write), 0) FROM turn_api_calls WHERE turn_id = ? AND "
                + where, (TURN,)).fetchone()[0]
        parent_view = total("parent_call_id IS NULL")
        physical_view = total("NOT (sub_harness IS NOT NULL AND parent_call_id IS NULL)")
        everything = total("1")
    assert parent_view == physical_view > 0
    assert everything == 2 * parent_view


def test_children_need_a_parent_row(db):
    """No virtual row (e.g. its insert failed) -> no dangling children."""
    agent = _agent()
    agent._blackbox_last_ok_call = (TURN, 7)
    n = cch._emit_composite_api_call_records(
        agent, [_advisor("grok-4.7", "xai-oauth", 1, 1)], sub_harness="moa:default")
    assert n == 0
    assert _rows(db) == []
    assert agent._api_call_recording_failures == 1


def test_children_bind_only_to_this_turns_parent(db):
    agent = _agent()
    agent._blackbox_last_ok_call = ("other:turn", 0)
    assert cch._emit_composite_api_call_records(
        agent, [_advisor("grok-4.7", "xai-oauth", 1, 1)], sub_harness="moa:default") == 0


def test_migration_adds_nullable_columns_to_existing_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = store._db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(str(path))
    legacy.execute(
        "CREATE TABLE turn_api_calls (turn_id TEXT NOT NULL, seq INT NOT NULL,"
        " ts REAL, provider TEXT, sub_key TEXT, model TEXT, input_tokens INT,"
        " output_tokens INT, cache_read INT, cache_write INT, reasoning INT,"
        " attribution TEXT, http_status INT, relay_synthetic INT NOT NULL DEFAULT 0,"
        " route_id TEXT, PRIMARY KEY(turn_id, seq))")
    legacy.execute("INSERT INTO turn_api_calls (turn_id, seq, provider) VALUES ('t', 0, 'x')")
    legacy.commit()
    legacy.close()
    store._connect().close()
    store._connect().close()  # idempotent
    with sqlite3.connect(str(path)) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(turn_api_calls)")}
        assert {"parent_call_id", "sub_harness"} <= cols
        assert conn.execute(
            "SELECT parent_call_id, sub_harness FROM turn_api_calls").fetchone() == (None, None)


def test_late_reference_calls_survive_to_the_next_pickup():
    """An interrupted advisor that bills late gets a physical call record."""
    from agent.moa_loop import MoAChatCompletions, _RefAccounting

    facade = MoAChatCompletions("default")
    facade._pending_reference_pricing_calls = [_advisor("grok-4.7", "xai-oauth", 5, 5)]
    facade._record_late_reference_accounting(
        "openrouter/kimi", _RefAccounting(
            CanonicalUsage(input_tokens=70, output_tokens=7), 0.01, "estimated",
            model="moonshotai/kimi-k3", provider="openrouter", http_status=200,
        ))
    # A cache-HIT iteration clears the fresh list only; the late call stays.
    facade._pending_reference_pricing_calls = []
    calls = facade.consume_reference_pricing_calls()
    assert [(c["model"], c["input_tokens"], c.get("late")) for c in calls] == [
        ("moonshotai/kimi-k3", 70, True)]
    assert calls[0]["http_status"] == 200
    assert facade.consume_reference_pricing_calls() == []


def test_sentinel_never_pages_on_the_virtual_parent(db):
    dispatched = []
    assert sentinel.observe_turn(
        "moa/default", "moa", "unknown", None,
        dispatch=lambda m, p, fn: dispatched.append((m, p)),
    ) is False
    assert dispatched == []


def test_session_end_observes_physical_routes_not_the_parent(db, monkeypatch):
    seen = []
    monkeypatch.setattr(sentinel, "observe_turn",
                        lambda model, provider, *a, **k: seen.append((model, provider)))
    monkeypatch.setattr(blackbox, "compute_turn_cost",
                        lambda *a, **k: (None, "unknown", {}))
    blackbox._on_session_start(session_id="sess")
    blackbox._on_session_end(
        session_id="sess", turn_id=TURN, provider="moa", model="default",
        platform="cli",
        turn_usage={"input_tokens": 10, "output_tokens": 2, "api_calls": 1,
                    "calls": [{"input_tokens": 10, "pricing_calls": [
                        _advisor("grok-4.7", "xai-oauth", 5, 1),
                        _advisor("claude-fable-5-1", "claude-bpr", 5, 1),
                    ]}]},
    )
    assert seen == [("grok-4.7", "xai-oauth"), ("claude-fable-5-1", "claude-bpr")]


def test_full_loop_moa_turn_ledgers_parent_and_physical_children(db, tmp_path):
    """Real ``run_conversation`` wiring: transport parent row + loop children."""
    from unittest.mock import MagicMock, patch

    from hermes_state import SessionDB
    from run_agent import AIAgent

    usage = SimpleNamespace(prompt_tokens=4000, completion_tokens=120, total_tokens=4120)
    msg = SimpleNamespace(content="acted", tool_calls=None)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")],
        model="claude-fable-5-1", usage=usage,
    )
    session_db = SessionDB(db_path=tmp_path / "state.db")
    try:
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            agent = AIAgent(
                api_key="test-key", base_url="https://openrouter.ai/api/v1",
                quiet_mode=True, skip_context_files=True, skip_memory=True,
                session_db=session_db, session_id="moa-loop", platform="cli",
            )
        client = MagicMock()
        client.chat.completions.create.return_value = response
        client.chat.completions.preset_name = "default"
        client.consume_reference_usage = lambda: (
            CanonicalUsage(input_tokens=1900, output_tokens=190), None)
        client.consume_reference_pricing_calls = lambda: [
            _advisor("gpt-6-astra-900k", "openai-codex", 1000, 100),
            _advisor("grok-4.7", "xai-oauth", 900, 90),
        ]
        client.last_aggregator_slot = {"provider": "claude-bpr", "model": "claude-fable-5-1"}
        agent.client = client
        agent.model = "default"
        agent.provider = "moa"
        agent.base_url = None
        agent.run_conversation("moa turn")
    finally:
        session_db.close()

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM turn_api_calls WHERE sub_harness = 'moa:default' ORDER BY seq")]
    assert len(rows) == 4, rows
    parent, children = rows[0], rows[1:]
    assert parent["provider"] == "moa" and parent["parent_call_id"] is None
    assert [(r["provider"], r["model"]) for r in children] == [
        ("openai-codex", "gpt-6-astra-900k"), ("xai-oauth", "grok-4.7"),
        ("claude-bpr", "claude-fable-5-1")]
    assert {r["turn_id"] for r in rows} == {parent["turn_id"]}
    assert all(r["parent_call_id"] == parent["seq"] for r in children)
    assert _sum(children) == {c: parent[c] for c in _USAGE_COLS}
    assert parent["input_tokens"] == 4000 + 1900
