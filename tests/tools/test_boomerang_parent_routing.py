"""Parent routing is independent of boomerang context/cache eligibility."""
from __future__ import annotations

import copy
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import run_agent

from tools import delegate_tool as dt


@pytest.mark.parametrize("mode", ["summary", "fork", "branch", None])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("disabled", [False, True, "auto"])
def test_context_mode_does_not_select_route(monkeypatch, mode, explicit, disabled):
    chain = [] if disabled is True else [{"provider": "openrouter", "model": "fallback-model"},
                                {"provider": "openai", "model": "cross-vendor-model"}]
    parent = SimpleNamespace(
        model="parent-model", provider="openrouter", requested_provider="openrouter",
        base_url="http://127.0.0.1:1/v1", api_key="synthetic-parent-key",
        api_mode="chat_completions", client=None, _client_kwargs={},
        _fallback_chain=copy.deepcopy(chain), request_overrides={"extra_body": {"nonce": "parent"}},
        _session_db=None, _delegate_depth=0, _active_children=[],
        _active_children_lock=threading.Lock(), _print_fn=None, tool_progress_callback=None,
        thinking_callback=None, prefill_messages=None, session_id="routing-parent",
        platform="cli", enabled_toolsets=[], disabled_toolsets=[],
        _session_messages=[{"role": "assistant", "content": "parent context"}],
    )
    captured = []

    def construct(**kwargs):
        captured.append(kwargs)
        child = MagicMock()
        child.run_conversation.return_value = {"final_response": "done", "completed": True, "api_calls": 1}
        child._delegate_depth = 1
        child.get_activity_summary.return_value = {"api_call_count": 1}
        return child

    monkeypatch.setattr("run_agent.AIAgent", construct)
    monkeypatch.setattr(dt, "_load_config", lambda: {"model": "worker-model", "fallback_providers": "auto" if disabled == "auto" else []})
    result = json.loads(dt.delegate_task(
        goal="read parent context", parent_agent=parent, inherit_mode=mode,
        model="explicit-child-model" if explicit else None,
    ))
    assert not result.get("error"), result
    assert captured
    expected = "explicit-child-model" if explicit else ("parent-model" if mode else "worker-model")
    assert captured[0]["model"] == expected
    selected_chain = captured[0]["fallback_model"] or []
    if disabled == "auto" and (explicit or not mode):
        assert isinstance(selected_chain, list)
        # Unknown-vendor names stay unchanged; auto must not invent a vendor mapping.
        assert selected_chain == chain
    else:
        assert selected_chain == (chain if mode and not explicit else [])
    assert parent._fallback_chain == chain
    if mode and not explicit:
        captured[0]["request_overrides"]["extra_body"]["nonce"] = "child"
        if chain:
            captured[0]["fallback_model"][0]["model"] = "child-model"
        assert parent.request_overrides["extra_body"]["nonce"] == "parent"
        assert parent._fallback_chain == chain


def test_route_snapshot_is_coherent_during_activation():
    from agent.route_snapshot import route_lock, snapshot_parent
    parent = SimpleNamespace(model="a", provider="a", base_url="http://a", api_key="a",
                             client=None, _client_kwargs={}, request_overrides={}, _fallback_chain=[])
    entered, release, captured = threading.Event(), threading.Event(), []

    def switch():
        with route_lock(parent):
            parent.provider = "b"
            entered.set()
            assert release.wait(5)
            parent.model, parent.base_url, parent.api_key = "b", "http://b", "b"

    writer = threading.Thread(target=switch)
    writer.start()
    assert entered.wait(5)
    reader = threading.Thread(target=lambda: captured.append(snapshot_parent(parent)))
    reader.start()
    release.set()
    writer.join(5)
    reader.join(5)
    assert not writer.is_alive() and not reader.is_alive()
    view = captured[0]
    assert (view.model, view.provider, view.base_url, view.api_key) == ("b", "b", "http://b", "b")


def test_recovered_routes_are_secret_free_and_session_isolated(monkeypatch):
    from tools.delegate_tool_routing import persist_route, recover_route_parent
    parent = SimpleNamespace(model="parent", provider="openrouter", api_mode="chat_completions",
                             base_url="http://parent", api_key="SECRET-SENTINEL", client=None,
                             _client_kwargs={}, _fallback_chain=[], request_overrides={})
    records = []
    for name in ("a", "b"):
        parent.requested_provider = name
        parent.base_url = "http://" + name
        records.append(json.loads(json.dumps(persist_route(parent))))
    assert "SECRET-SENTINEL" not in json.dumps(records)
    calls = []

    def resolve(provider, **kw):
        calls.append(provider)
        return {"provider": provider, "base_url": "http://" + provider,
                "api_key": "SECRET-" + provider, "api_mode": "chat_completions"}

    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", resolve)
    a = recover_route_parent(parent, records[0])
    b = recover_route_parent(parent, records[1])
    again = recover_route_parent(parent, records[0])
    assert calls == ["a", "b", "a"]
    assert (a.provider, b.provider, again.provider) == ("a", "b", "a")
    assert a.api_key == again.api_key != b.api_key
    assert a._fallback_chain == b._fallback_chain == []
    a.request_overrides["new"] = True
    assert not b.request_overrides and not parent.request_overrides
    with pytest.raises(ValueError, match="incomplete persisted identity"):
        recover_route_parent(parent, {"model": "missing-provider"})
    assert calls == ["a", "b", "a"]


def test_real_credential_sync_cannot_publish_mixed_endpoint():
    from agent.client_lifecycle import ClientLifecycleMixin
    from agent.route_snapshot import snapshot_parent
    entered, release, attempted, finished = (threading.Event() for _ in range(4))

    class PausingDict(dict):
        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            if key == "api_key":
                entered.set()
                assert release.wait(5)

    parent = SimpleNamespace(model="a", provider="a", base_url="http://new", api_key="NEW",
                             client=None, _client_kwargs=PausingDict(base_url="http://old", api_key="OLD"))
    writer = threading.Thread(target=lambda: ClientLifecycleMixin._sync_client_kwargs_credentials(parent))
    captured = []

    def read():
        attempted.set()
        captured.append(snapshot_parent(parent))
        finished.set()

    writer.start()
    assert entered.wait(5)
    reader = threading.Thread(target=read)
    reader.start()
    assert attempted.wait(5)
    try:
        assert not finished.wait(0.1), "snapshot escaped while real credential writer was incomplete"
    finally:
        release.set()
        writer.join(5)
        reader.join(5)
    assert (captured[0].base_url, captured[0].api_key) == ("http://new", "NEW")


@pytest.mark.parametrize("sources", [("live-parent", "delegation-default"), ("delegation-default", "live-parent"), ("explicit-child",)])
def test_recovery_replays_each_selected_task_route(monkeypatch, sources):
    from tools.delegate_tool_routing import persist_route
    parent = SimpleNamespace(model="parent", provider="openrouter", api_mode="chat_completions",
        base_url="http://parent", api_key="test", client=None, _client_kwargs={},
        _fallback_chain=[{"provider": "openai", "model": "parent-backup"}], request_overrides={},
        _session_db=None, _delegate_depth=0, _active_children=[], _active_children_lock=threading.Lock(),
        session_id="recovery-parent", platform="cli", enabled_toolsets=[], disabled_toolsets=[])
    captured = []
    def construct(**kw):
        captured.append(kw)
        child = MagicMock()
        child.run_conversation.return_value = {"final_response": "done", "completed": True, "api_calls": 1}
        child.get_activity_summary.return_value = {"api_call_count": 1}
        return child
    monkeypatch.setattr("run_agent.AIAgent", construct)
    monkeypatch.setattr(dt, "_load_config", lambda: {"model": "changed-default", "fallback_providers": "auto"})
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", lambda provider, **kw: {
        "provider": provider, "api_key": "fresh-key", "base_url": "http://" + provider, "api_mode": "chat_completions"})
    tasks = []
    for index, source in enumerate(sources):
        route = {"source": "parent", "provider": "openrouter", "model": source,
                 "api_mode": "chat_completions", "fallback_chain": [] if source != "live-parent" else parent._fallback_chain,
                 "request_overrides": {}}
        tasks.append({"goal": "restore selected route " + str(index), "inherit_mode": "summary",
                      "route_source": source, "parent_route": route, "materialized_prefill_messages": []})
    record = {"execution": {"model": "parent", "provider": "openrouter", "fallback_chain": parent._fallback_chain,
              "credential_ref": {"source": "provider", "provider": "openrouter", "inherited_route": True}},
              "source": {"tasks": tasks}, "route": {}}
    result = json.loads(dt.delegate_task(tasks=tasks, parent_agent=parent, _recovery_spec=record))
    assert not result.get("error"), result
    assert [kw["model"] for kw in captured] == list(sources)
    assert [kw["fallback_model"] for kw in captured] == [parent._fallback_chain if s == "live-parent" else [] for s in sources]


def test_route_snapshot_has_bounded_wait():
    from agent.route_snapshot import route_lock, snapshot_parent
    parent = SimpleNamespace(base_url="http://parent", api_key="synthetic", client=None)
    entered, release = threading.Event(), threading.Event()
    def hold():
        with route_lock(parent):
            entered.set()
            release.wait(5)
    thread = threading.Thread(target=hold)
    thread.start()
    assert entered.wait(5)
    try:
        with pytest.raises(RuntimeError, match="route activation is still in progress"):
            snapshot_parent(parent, timeout=0.01)
    finally:
        release.set()
        thread.join(5)


@pytest.mark.parametrize("source", ["delegation-default", "explicit-child", "mixed-parent-first", "mixed-direct-first"])
@pytest.mark.parametrize("same_endpoint", [False, True])
def test_direct_recovery_uses_endpoint_bound_delegation_key(monkeypatch, source, same_endpoint):
    parent = SimpleNamespace(model="parent", provider="openrouter", api_mode="chat_completions",
        base_url="http://parent", api_key="parent-key", client=None, _client_kwargs={},
        _fallback_chain=[], request_overrides={}, _session_db=None, _delegate_depth=0,
        _active_children=[], _active_children_lock=threading.Lock(), session_id="direct-parent",
        platform="cli", enabled_toolsets=[], disabled_toolsets=[])
    route = {"source": "parent", "model": "selected-child", "provider": "custom",
        "base_url": "http://selected/v1", "api_mode": "chat_completions",
        "fallback_chain": [{"provider": "openai", "model": "selected-backup"}],
        "request_overrides": {"extra_body": {"nonce": "selected"}},
        "routing_filters": {"providers_allowed": ["selected-lane"]}}
    config = {"model": "changed-default", "base_url": route["base_url"], "api_key": "delegation-key"}
    monkeypatch.setattr(dt, "_load_config", lambda: config)
    captured = []
    def construct(**kw):
        captured.append(kw)
        child = MagicMock()
        child.run_conversation.return_value = {"final_response": "done", "completed": True, "api_calls": 1}
        child.get_activity_summary.return_value = {"api_call_count": 1}
        return child
    monkeypatch.setattr("run_agent.AIAgent", construct)
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", lambda *a, **kw: {
        "provider": "custom", "api_key": "WRONG-provider-key", "api_mode": "chat_completions",
        "base_url": route["base_url"] if same_endpoint else "http://different/v1"})
    task = {"goal": "restore direct child", "route_source": source, "parent_route": route,
            "credential_ref": {"source": "delegation_config"}, "materialized_prefill_messages": []}
    tasks = [task]
    inherited_route = {**route, "provider": "openrouter", "model": "inherited-child",
        "base_url": route["base_url"] if same_endpoint else "http://different/v1",
        "fallback_chain": [{"provider": "openrouter", "model": "inherited-backup"}]}
    if source.startswith("mixed"):
        task["route_source"] = "delegation-default"
        inherited = {"goal": "restore inherited child", "route_source": "live-parent",
            "inherit_mode": "summary", "parent_route": inherited_route,
            "credential_ref": {"source": "provider", "provider": "openrouter"},
            "materialized_prefill_messages": []}
        tasks = [inherited, task] if source == "mixed-parent-first" else [task, inherited]
    record = {"execution": {"model": route["model"], "provider": "custom", "base_url": route["base_url"],
              "fallback_chain": [], "credential_ref": {"source": "delegation_config"}}, "route": {}}
    result = json.loads(dt.delegate_task(tasks=tasks, parent_agent=parent, _recovery_spec=record))
    assert not result.get("error"), result
    selected = next(kw for kw in captured if kw["model"] == route["model"])
    assert selected["api_key"] == "delegation-key"
    assert selected["fallback_model"] == route["fallback_chain"]
    assert selected["providers_allowed"] == ["selected-lane"]
    assert selected["request_overrides"] == route["request_overrides"]
    if source.startswith("mixed"):
        other = next(kw for kw in captured if kw["model"] == inherited_route["model"])
        assert other["api_key"] == "WRONG-provider-key"  # correct provider source, not the direct config
        assert other["fallback_model"] == inherited_route["fallback_chain"]
        assert [kw["model"] for kw in captured] == [t["parent_route"]["model"] for t in tasks]
    # A rotated endpoint or removed key must not expose the unrelated provider key.
    captured.clear()
    network = []
    monkeypatch.setattr("httpx.Client.send", lambda *a, **kw: network.append(kw))
    config["base_url"] = "http://rotated/v1"
    refused = json.loads(dt.delegate_task(tasks=[task], parent_agent=parent, _recovery_spec=record))
    assert "endpoint credentials" in refused["error"]
    assert captured == [] and not network and "WRONG-provider-key" not in json.dumps(refused)
    config["base_url"] = route["base_url"]
    config["api_key"] = ""
    refused = json.loads(dt.delegate_task(tasks=[task], parent_agent=parent, _recovery_spec=record))
    assert "credentials could not be resolved" in refused["error"]
    assert captured == [] and not network and "parent-key" not in json.dumps(refused)


def test_persist_route_uses_constructed_fallback_chain():
    from tools.delegate_tool_routing import persist_route
    resolved = [{"provider": "openai", "model": "resolved-backup"}]
    child = SimpleNamespace(_fallback_chain=resolved, _boomerang_parent_route={
        "source": "parent", "model": "selected", "provider": "openrouter",
        "base_url": "http://selected/v1", "api_mode": "chat_completions",
        "fallback_chain": "auto", "request_overrides": {}})
    assert persist_route(child)["fallback_chain"] == resolved
