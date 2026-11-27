"""Fail-closed endpoint/policy roundtrips at the durable routing boundary."""
from types import SimpleNamespace

import pytest

from tools.delegate_tool_routing import persist_route, recover_route_parent


def parent():
    return SimpleNamespace(model="selected", provider="openai", requested_provider="openai",
        api_mode="chat_completions", base_url="http://127.0.0.1:9001/selected/v1",
        api_key="SECRET-PRIMARY", client=None, _client_kwargs={}, request_overrides={},
        _fallback_chain=[{"provider": "openai", "model": "backup",
                          "base_url": "http://127.0.0.1:9002/backup/v1"}])


@pytest.mark.parametrize("matching", [False, True])
def test_recovery_credentials_are_bound_to_selected_endpoint(monkeypatch, matching):
    p = parent()
    route = persist_route(p)
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", lambda *a, **kw: {
        "provider": "openai", "api_key": "FRESH-KEY", "base_url": p.base_url if matching else "http://provider-default/v1",
        "api_mode": "chat_completions"})
    requests = []
    monkeypatch.setattr("httpx.Client.send", lambda *a, **kw: requests.append(kw))
    if not matching:
        with pytest.raises(ValueError, match="endpoint") as error:
            recover_route_parent(p, route)
        assert requests == []
        assert "FRESH-KEY" not in str(error.value)
        assert "SECRET-PRIMARY" not in str(error.value)
        return
    restored = recover_route_parent(p, route)
    assert restored.base_url == p.base_url, "selected endpoint lost to provider default"
    assert restored._client_kwargs == {"api_key": "FRESH-KEY", "base_url": p.base_url}
    assert restored._fallback_chain == p._fallback_chain
    assert "SECRET-PRIMARY" not in str(route)


@pytest.mark.parametrize("provider,endpoint", [("copilot-acp", "acp://copilot"), ("moa", "moa://local")])
def test_virtual_provider_route_roundtrip(monkeypatch, provider, endpoint):
    p = parent()
    p.provider = p.requested_provider = provider
    p.base_url = endpoint
    p._fallback_chain = []
    route = persist_route(p)
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", lambda *a, **kw: {
        "provider": provider, "api_key": "virtual-credential", "base_url": endpoint,
        "api_mode": "chat_completions"})
    restored = recover_route_parent(p, route)
    assert restored.base_url == endpoint
    assert restored.requested_provider == provider


@pytest.mark.parametrize("provider,endpoint", [("openai", "acp://copilot"), ("openai", "moa://local"),
                                               ("moa", "moa://foreign")])
def test_virtual_endpoint_requires_owning_provider(provider, endpoint):
    p = parent()
    p.provider = p.requested_provider = provider
    p.base_url = endpoint
    with pytest.raises(ValueError, match="endpoint"):
        persist_route(p)


@pytest.mark.parametrize("chain", [None, "auto", {"provider": "openai"}, [None], ["openai"],
                                    [{}], [{"provider": 1, "model": "m"}], [{"provider": "openai", "model": []}]])
def test_malformed_persisted_chain_refused_before_credentials(monkeypatch, chain):
    route = {"provider": "openai", "model": "m", "fallback_chain": chain}
    calls = []
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", lambda *a, **kw: calls.append(a))
    with pytest.raises(ValueError, match="invalid persisted policy"):
        recover_route_parent(parent(), route)
    assert calls == []


def test_persisted_filters_cannot_replace_execution_attributes(monkeypatch):
    route = {"provider": "openai", "model": "m", "fallback_chain": [],
             "routing_filters": {"base_url": "http://unselected/v1"}}
    calls = []
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", lambda *a, **kw: calls.append(a))
    with pytest.raises(ValueError, match="invalid persisted policy"):
        recover_route_parent(parent(), route)
    assert not calls


@pytest.mark.parametrize("identity", [{"provider": 1, "model": "m"}, {"provider": "openai", "model": []},
                                     {"provider": "", "model": "m"}])
def test_corrupt_identity_refused_before_credentials(monkeypatch, identity):
    calls = []
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", lambda *a, **kw: calls.append(a))
    with pytest.raises(ValueError, match="incomplete persisted identity"):
        recover_route_parent(parent(), {**identity, "fallback_chain": []})
    assert not calls


@pytest.mark.parametrize("url", ["https://user:URL-SECRET@host/v1", "https://host/v1?api_key=URL-SECRET"])
def test_secret_endpoint_never_persisted_or_recovered(monkeypatch, url):
    p = parent()
    p.base_url = url
    with pytest.raises(ValueError, match="credentials"):
        persist_route(p)
    calls = []
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", lambda *a, **kw: calls.append(a))
    with pytest.raises(ValueError, match="invalid persisted policy"):
        recover_route_parent(p, {"provider": "openai", "model": "m", "base_url": url, "fallback_chain": []})
    assert calls == []
