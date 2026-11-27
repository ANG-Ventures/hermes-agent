"""t_a9bbc4ad: a cold-rebuilt boomerang parent carries the gateway's provider_routing filters.

After a gateway restart there is no live agent, so a wake child is delegated through ``_db_parent``.
The child inherits routing filters from the parent's attributes (tools/delegate_tool_config.py), so a
parent built without them sent the child's requests with no allowlist and no data_collection deny.
A provider_routing read error must fail the cold wake closed instead of looking like "no filters".
"""
from types import SimpleNamespace

import pytest

import run_agent  # Load bootstrap before the per-test home-I/O guard.
from gateway import kanban_boomerang_wake as wake

ROUTING = {"only": ["allowed-a"], "ignore": ["ignored-b"], "order": ["allowed-a", "c"], "sort": "price",
           "require_parameters": True, "data_collection": "deny"}
FILTERS = {"providers_allowed": ["allowed-a"], "providers_ignored": ["ignored-b"],
           "providers_order": ["allowed-a", "c"], "provider_sort": "price",
           "provider_require_parameters": True, "provider_data_collection": "deny"}


def _runner(monkeypatch, seen):
    import gateway.run as gr
    from gateway.run import GatewayRunner

    monkeypatch.setattr(gr, "_resolve_runtime_agent_kwargs",
                        lambda: {"provider": "openrouter", "base_url": "http://localhost:1", "api_key": "k"})
    monkeypatch.setattr(gr, "_resolve_gateway_model", lambda *a, **k: "cfg-model")
    monkeypatch.setattr(run_agent, "AIAgent", lambda **kw: (seen.update(kw) or SimpleNamespace()))
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.session_store = SimpleNamespace(load_transcript=lambda sid: [])
    runner._session_model_overrides = {}
    runner._refresh_fallback_model = lambda: None
    return runner


HOME = SimpleNamespace(session_id="s1", session_key=None, origin=None)


def test_cold_parent_carries_the_gateway_provider_routing_filters(monkeypatch):
    seen = {}
    runner = _runner(monkeypatch, seen)
    runner._provider_routing = dict(ROUTING)
    wake._db_parent(runner, HOME)
    assert {k: seen.get(k) for k in FILTERS} == FILTERS

    # ...and the child built off that parent inherits them (default path, no delegation override).
    from tools.delegate_tool_config import _resolve_child_runtime

    parent = SimpleNamespace(model="cfg-model", provider="openrouter", base_url="http://localhost:1",
                             api_key="k", api_mode="chat_completions", **{k: seen.get(k) for k in FILTERS})
    child = _resolve_child_runtime(parent, {}, "k", model=None, override_provider=None, override_base_url=None,
                                   override_api_key=None, override_api_mode=None, override_acp_command=None,
                                   override_acp_args=None)
    assert {k: child.get(k) for k in FILTERS} == FILTERS


def test_cold_parent_with_no_provider_routing_block_builds_unfiltered(monkeypatch):
    seen = {}
    runner = _runner(monkeypatch, seen)
    runner._provider_routing = {}
    wake._db_parent(runner, HOME)
    assert seen["model"] == "cfg-model"
    assert not seen.get("providers_allowed") and not seen.get("provider_data_collection")


def _load_runtime_routing(monkeypatch, tmp_path, text):
    import gateway.run as gr
    from gateway.run import GatewayRunner

    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setattr(gr, "_hermes_home", tmp_path)
    monkeypatch.setattr(gr, "get_hermes_home_override", lambda: None, raising=False)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._init_runtime_settings()
    return runner


def test_provider_routing_read_error_is_not_an_absent_block(monkeypatch, tmp_path):
    (tmp_path / "absent").mkdir()
    (tmp_path / "broken").mkdir()
    absent = _load_runtime_routing(monkeypatch, tmp_path / "absent", "model: m\n")
    assert absent._provider_routing == {}
    assert getattr(absent, "_provider_routing_error", None) is None

    broken = _load_runtime_routing(monkeypatch, tmp_path / "broken", "provider_routing: [unclosed\n  only: a\n")
    assert broken._provider_routing == {}
    assert broken._provider_routing_error  # distinguishable from "no filters"


def test_cold_wake_fails_closed_when_provider_routing_could_not_be_read(monkeypatch, tmp_path):
    seen = {}
    loaded = _load_runtime_routing(monkeypatch, tmp_path, "provider_routing: [unclosed\n  only: a\n")
    runner = _runner(monkeypatch, seen)
    runner._provider_routing = loaded._provider_routing
    runner._provider_routing_error = getattr(loaded, "_provider_routing_error", None)
    with pytest.raises(RuntimeError, match="provider_routing"):
        wake._db_parent(runner, HOME)
    assert not seen  # no unfiltered parent was ever built


def test_refused_cold_wake_reaches_the_home_as_a_dispatch_failure(monkeypatch, tmp_path):
    """The raise is not swallowed: the claim fails with a reason naming provider_routing, which
    ``_fail`` turns into the home's line, and no child runs."""
    import asyncio

    seen, failed, ran = {}, [], []
    runner = _runner(monkeypatch, seen)
    runner._provider_routing, runner._provider_routing_error = {}, "config.yaml read failed (YAMLError)"
    state = wake.BoomerangWakeState(tmp_path / "claims.json")
    entry = {"key": "claim", "delegation_id": "d1", "task_id": "t_fixture", "home_sid": "s1", "wake_text": "w"}
    state.claim(entry["key"], entry)

    async def cold(r, e):
        return wake._db_parent(r, HOME), "summary"

    async def fail(_r, live, reason, _s, *a, **k):
        failed.append(reason)

    monkeypatch.setattr(wake, "parent_resolver", cold)
    monkeypatch.setattr(wake, "run_child", lambda *a: ran.append(a) or ("completed", ""))
    monkeypatch.setattr(wake, "_fail", fail)
    monkeypatch.setattr(wake, "ledger_append", lambda *a, **k: None)
    asyncio.run(wake._run_entry_scoped(runner, entry, state))
    assert not ran and not seen
    assert len(failed) == 1 and "provider_routing unreadable" in failed[0]
