"""``{provider: auto}`` in ``auxiliary.<task>.fallback_chain`` = the session's main model.

Chain under test is the fleet's compression seat: primary claude-bpr / Sonnet,
then ``auto`` (the session's own main model), then openai-codex / Luna.
"""
from unittest.mock import MagicMock

import pytest

from agent import auxiliary_client as aux
from agent.auxiliary_client import call_llm

PRIMARY = ("claude-bpr", "claude-sonnet-5-5")
RESOLVED_KWARGS: list = []  # (provider, resolve_provider_client kwargs) per seat call
CHAIN = [
    {"provider": "auto", "timeout": 300},
    {"provider": "openai-codex", "model": "gpt-6-luna-900k", "timeout": 300},
]


def _client(text):
    client = MagicMock()
    client.base_url = f"https://{text.split()[0]}.invalid/v1"
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=text), finish_reason="stop")]
    )
    return client


@pytest.fixture
def seat(monkeypatch):
    """Dead primary (429), real chain walker, one fake client per provider."""
    aux.clear_runtime_main()
    config = {"provider": PRIMARY[0], "model": PRIMARY[1], "fallback_chain": CHAIN}
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: config)

    primary = MagicMock()
    err = Exception("Rate limit exceeded, try again in 60 seconds")
    err.status_code = 429
    primary.chat.completions.create.side_effect = err
    monkeypatch.setattr(aux, "_get_cached_client", lambda *a, **kw: (primary, PRIMARY[1]))
    monkeypatch.setattr(
        aux, "_resolve_task_provider_model",
        lambda *a, **kw: (PRIMARY[0], PRIMARY[1], None, None, None),
    )
    clients = {
        "custom": _client("session summary"),
        "kimi-coding": _client("kimi summary"),
        "claude-bpr": _client("sonnet summary"),
        "openai-codex": _client("luna summary"),
    }
    resolved = []

    def resolve(provider, model=None, **kwargs):
        resolved.append((provider, model))
        RESOLVED_KWARGS.append((provider, kwargs))
        return clients.get(provider), model

    RESOLVED_KWARGS.clear()

    monkeypatch.setattr(aux, "resolve_provider_client", resolve)
    monkeypatch.setattr(aux, "_is_provider_unhealthy", lambda *a, **kw: False)
    monkeypatch.setattr(aux, "_candidate_context_window", lambda *a, **kw: None)
    main_net = MagicMock(return_value=(None, None, ""))
    monkeypatch.setattr(aux, "_try_main_agent_model_fallback", main_net)
    yield clients, resolved, main_net
    aux.clear_runtime_main()


def _summarise(main_runtime):
    route_info = {}
    resp = call_llm(
        task="compression",
        main_runtime=main_runtime,
        messages=[{"role": "user", "content": "summarise this transcript"}],
        route_info=route_info,
    )
    return resp.choices[0].message.content, route_info


def test_dead_primary_falls_to_session_main_model(seat):
    clients, resolved, main_net = seat
    text, route = _summarise({"provider": "kimi-coding", "model": "kimi-k3"})
    assert text == "kimi summary"
    assert ("kimi-coding", "kimi-k3") in resolved
    # telemetry names the concrete route, never "auto"
    assert route["provider"] == "kimi-coding"
    assert route["model"] == "kimi-k3"
    clients["openai-codex"].chat.completions.create.assert_not_called()
    main_net.assert_not_called()


def test_main_model_equal_to_failed_primary_is_skipped_to_luna(seat):
    clients, resolved, _ = seat
    text, route = _summarise({"provider": PRIMARY[0], "model": PRIMARY[1]})
    assert text == "luna summary"
    assert PRIMARY not in resolved
    assert route["provider"] == "openai-codex"
    clients["claude-bpr"].chat.completions.create.assert_not_called()


def test_no_live_main_runtime_skips_auto_rung(seat):
    _clients, resolved, _ = seat
    text, route = _summarise(None)
    assert text == "luna summary"
    assert [p for p, _m in resolved] == ["openai-codex"]
    assert route["provider"] == "openai-codex"


def test_resolved_entry_keeps_timeout_and_drops_route_keys():
    entry = {"provider": "auto", "timeout": 300, "base_url": "https://x.invalid", "api_key": "k"}
    got = aux.resolve_auto_chain_entry(entry, {"provider": "Kimi-Coding", "model": "kimi-k3"})
    assert got == {"provider": "kimi-coding", "model": "kimi-k3", "timeout": 300}
    assert aux.resolve_auto_chain_entry(entry, {"provider": "auto", "model": "x"}) is None
    concrete = {"provider": "openai-codex", "model": "gpt-6-luna-900k"}
    assert aux.resolve_auto_chain_entry(concrete, {}) is concrete


class TestPinnedRouteReaders:
    """Refusal re-send and stall retry read the chain themselves."""

    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch):
        aux.clear_runtime_main()
        config = {"provider": PRIMARY[0], "model": PRIMARY[1], "fallback_chain": CHAIN}
        monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: config)
        yield
        aux.clear_runtime_main()

    def test_refusal_routes_resolve_auto_and_drop_primary(self):
        from agent.context_compressor import _compression_refusal_fallback_routes

        kimi = _compression_refusal_fallback_routes({"provider": "kimi-coding", "model": "kimi-k3"})
        assert [(r["provider"], r.get("model")) for r in kimi] == [
            ("kimi-coding", "kimi-k3"), ("openai-codex", "gpt-6-luna-900k"),
        ]
        same = _compression_refusal_fallback_routes({"provider": PRIMARY[0], "model": PRIMARY[1]})
        assert [r["provider"] for r in same] == ["openai-codex"]
        assert all(r["provider"] != "auto" for r in _compression_refusal_fallback_routes({}))

    def test_stall_route_resolves_auto_and_keeps_configured_index(self, monkeypatch):
        from agent.conversation_compression import resolve_compression_fallback_route

        monkeypatch.setattr(aux, "_fallback_entry_api_key", lambda entry: None)
        with aux.scoped_runtime_main({"provider": "kimi-coding", "model": "kimi-k3"}):
            route = resolve_compression_fallback_route()
        assert (route["label"], route["provider"], route["model"], route["timeout"]) == (
            "fallback_chain[0](kimi-coding)", "kimi-coding", "kimi-k3", 300.0,
        )
        with aux.scoped_runtime_main({"provider": PRIMARY[0], "model": PRIMARY[1]}):
            route = resolve_compression_fallback_route()
        assert (route["label"], route["model"]) == (
            "fallback_chain[1](openai-codex)", "gpt-6-luna-900k",
        )


# ── Lost Route (Prism 296a5e86575b, t_d625ee4e): the auto rung must reach the
# session's main ROUTE (endpoint + key + api_mode), not just its provider/model.
SESSION_ENDPOINT = {
    "provider": "custom",
    "model": "qwen-local",
    "base_url": "http://10.0.0.5:8000/v1",
    "api_key": "session-key",
    "api_mode": "chat_completions",
}


def test_dead_primary_auto_rung_reaches_session_endpoint(seat):
    clients, resolved, _ = seat
    text, route = _summarise(dict(SESSION_ENDPOINT))
    assert text == "session summary"
    kwargs = dict(RESOLVED_KWARGS)["custom"]
    assert kwargs["explicit_base_url"] == SESSION_ENDPOINT["base_url"]
    assert kwargs["explicit_api_key"] == SESSION_ENDPOINT["api_key"]
    assert kwargs["api_mode"] == "chat_completions"
    clients["openai-codex"].chat.completions.create.assert_not_called()


def test_resolved_entry_inherits_runtime_route_fields():
    entry = {"provider": "auto", "timeout": 300, "base_url": "https://stale.invalid", "api_key": "stale"}
    got = aux.resolve_auto_chain_entry(entry, dict(SESSION_ENDPOINT))
    assert got == {
        "provider": "custom", "model": "qwen-local", "timeout": 300,
        "base_url": "http://10.0.0.5:8000/v1", "api_key": "session-key",
        "api_mode": "chat_completions",
    }
    # Session-pinned key on a named provider: kept, provider defaults otherwise.
    got = aux.resolve_auto_chain_entry(
        {"provider": "auto"}, {"provider": "kimi-coding", "model": "kimi-k3", "api_key": "pinned"},
    )
    assert got == {"provider": "kimi-coding", "model": "kimi-k3", "api_key": "pinned"}


def test_resolved_entry_keeps_callable_runtime_key():
    def token():
        return "entra-token"

    got = aux.resolve_auto_chain_entry(
        {"provider": "auto"},
        {"provider": "custom", "model": "m", "base_url": "https://foundry.invalid/v1", "api_key": token},
    )
    assert got["api_key"] is token
    assert aux._fallback_entry_api_key(got) is token


def test_moa_auto_rung_drops_facade_route(monkeypatch):
    monkeypatch.setattr(aux, "_resolve_moa_aggregator", lambda preset: ("kimi-coding", "kimi-k3"))
    got = aux.resolve_auto_chain_entry(
        {"provider": "auto"},
        {"provider": "moa", "model": "opus-gpt", "base_url": "moa://local", "api_key": "placeholder"},
    )
    assert got == {"provider": "kimi-coding", "model": "kimi-k3"}


class TestPinnedRouteReadersKeepSessionRoute:
    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch):
        aux.clear_runtime_main()
        config = {"provider": PRIMARY[0], "model": PRIMARY[1], "fallback_chain": CHAIN}
        monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: config)
        yield
        aux.clear_runtime_main()

    def test_refusal_route_carries_session_endpoint(self):
        from agent.context_compressor import _compression_refusal_fallback_routes

        routes = _compression_refusal_fallback_routes(dict(SESSION_ENDPOINT))
        assert routes[0] == {
            "provider": "custom", "model": "qwen-local", "timeout": 300,
            "base_url": "http://10.0.0.5:8000/v1", "api_key": "session-key",
            "api_mode": "chat_completions",
        }

    def test_compressor_refusal_path_passes_full_main_runtime(self):
        from agent.context_compressor import ContextCompressor

        comp = ContextCompressor.__new__(ContextCompressor)
        comp.model, comp.provider = "qwen-local", "custom"
        comp.base_url, comp.api_key, comp.api_mode = (
            "http://10.0.0.5:8000/v1", "session-key", "chat_completions",
        )
        assert comp._summary_main_runtime() == SESSION_ENDPOINT

    def test_stall_route_carries_session_endpoint(self):
        from agent.conversation_compression import resolve_compression_fallback_route

        with aux.scoped_runtime_main(dict(SESSION_ENDPOINT)):
            route = resolve_compression_fallback_route()
        assert (route["provider"], route["model"], route["base_url"], route["api_key"], route["api_mode"]) == (
            "custom", "qwen-local", "http://10.0.0.5:8000/v1", "session-key", "chat_completions",
        )
