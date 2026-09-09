"""Chat preferences survive key aliases, participants and process restarts."""
from dataclasses import replace


from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore


PIN = {"model": "pinned-model", "provider": "openrouter", "api_key": "must-not-persist"}


def make_store(tmp_path):
    store = SessionStore(config=GatewayConfig(), sessions_dir=tmp_path)
    store._db = None
    return store


def test_chat_pin_follows_other_session_and_restart(tmp_path):
    store = make_store(tmp_path)
    source = SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type="group", user_id="alice")
    entry = store.get_or_create_session(source)
    store.set_model_override(entry.session_key, PIN)
    other = store.get_or_create_session(replace(source, user_id="bob", chat_type="channel"))
    lookup = store.lookup_persisted_route_identity(other.session_key)
    assert lookup.state == "valid"
    assert lookup.identity["model"] == PIN["model"]
    restarted = make_store(tmp_path)
    assert restarted.lookup_persisted_route_identity(other.session_key).identity["model"] == PIN["model"]
    # The new preference store contains credential-free identity only.
    assert b"must-not-persist" not in (tmp_path / "chat-model-pins.sqlite3").read_bytes()


def test_clear_chat_pin_is_visible_to_already_open_store(tmp_path):
    first = make_store(tmp_path)
    source = SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type="group", user_id="alice")
    entry = first.get_or_create_session(source)
    first.set_model_override(entry.session_key, PIN)
    second = make_store(tmp_path)
    second._ensure_loaded()
    assert second.lookup_persisted_route_identity(entry.session_key).state == "valid"
    first.clear_model_route_override(entry.session_key)
    assert second.lookup_persisted_route_identity(entry.session_key).state == "absent"


def test_other_chat_has_no_pin(tmp_path):
    store = make_store(tmp_path)
    source = SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type="group", user_id="alice")
    entry = store.get_or_create_session(source)
    store.set_model_override(entry.session_key, PIN)
    other = store.get_or_create_session(replace(source, chat_id="999"))
    assert store.lookup_persisted_route_identity(other.session_key).state == "absent"


def test_legacy_channel_wake_resolves_pinned_runtime(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = make_store(tmp_path)
    source = SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type="group", user_id="alice")
    entry = runner.session_store.get_or_create_session(source)
    runner.session_store.set_model_override(entry.session_key, PIN)
    wake = replace(source, chat_type="channel", user_id="system")
    wake_entry = runner.session_store.get_or_create_session(wake)
    monkeypatch.setattr(runner, "_reresolve_model_override_credentials", lambda identity: {**identity, "api_key": "resolved", "base_url": "https://example.test/v1"})
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: {"provider": "openrouter", "api_key": "global"})
    model, runtime = runner._resolve_session_agent_runtime(
        user_config={"model": {"default": "unpinned-model", "provider": "openrouter"}},
        source=wake, session_key=wake_entry.session_key,
    )
    assert model == PIN["model"]
    assert runtime["provider"] == PIN["provider"]
    runner.session_store.clear_model_route_override(entry.session_key)
    model, _ = runner._resolve_session_agent_runtime(
        user_config={"model": {"default": "unpinned-model", "provider": "openrouter"}},
        source=wake, session_key=wake_entry.session_key,
    )
    assert model == "unpinned-model"
