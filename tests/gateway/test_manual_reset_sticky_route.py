"""Manual reset preserves deliberate route preferences, and only those."""

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource, SessionStore, build_session_key


MODEL_IDENTITY = {
    "model": "gpt-5.6-sol",
    "provider": "openai-codex",
    "api_mode": "codex_responses",
}
REASONING_NONE = {"enabled": False, "effort": "none"}


def _source(user_id="u1", chat_id="c1"):
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id=user_id,
        chat_id=chat_id,
        chat_type="dm",
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    def _no_db():
        raise RuntimeError("SQLite disabled in focused routing test")

    import hermes_state

    monkeypatch.setattr(hermes_state, "SessionDB", _no_db)
    return SessionStore(tmp_path / "sessions", GatewayConfig())


def _seed_preferences(store, source=None):
    entry = store.get_or_create_session(source or _source())
    with store._lock:
        entry.model_override_identity = dict(MODEL_IDENTITY)
        entry.reasoning_override = dict(REASONING_NONE)
        entry.last_served_identity = {
            "model": "temporary-fallback",
            "provider": "openrouter",
        }
        store._save()
    return entry


def test_session_store_manual_reset_rotates_transcript_and_preserves_exact_preferences(store):
    old = _seed_preferences(store)

    new = store.reset_session(
        old.session_key,
        preserve_route_preferences=True,
    )

    assert new.session_id != old.session_id
    assert new.is_fresh_reset is True
    assert new.model_override_identity == MODEL_IDENTITY
    assert new.reasoning_override == REASONING_NONE
    assert new.last_served_identity is None
    assert store.load_transcript(new.session_id) == []

    persisted = json.loads((store.sessions_dir / "sessions.json").read_text())
    route_payload = persisted[old.session_key]
    assert route_payload["model_override_identity"] == MODEL_IDENTITY
    assert route_payload["reasoning_override"] == REASONING_NONE
    raw = json.dumps(route_payload)
    assert "api_key" not in raw
    assert "access_token" not in raw


def test_automatic_reset_default_still_clears_route_preferences(store):
    old = _seed_preferences(store)

    new = store.reset_session(old.session_key)

    assert new.model_override_identity is None
    assert new.reasoning_override is None
    assert new.last_served_identity is None


def test_real_sqlite_transcript_stays_on_old_session(tmp_path, monkeypatch):
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr("hermes_state.SessionDB", lambda: db)
    session_store = SessionStore(tmp_path / "sessions", GatewayConfig())
    old = session_store.get_or_create_session(_source())
    db.append_message(old.session_id, "user", "old transcript sentinel")
    old.model_override_identity = dict(MODEL_IDENTITY)
    old.reasoning_override = dict(REASONING_NONE)
    session_store.persist()

    new = session_store.reset_session(
        old.session_key,
        preserve_route_preferences=True,
    )

    assert session_store.load_transcript(old.session_id)[0]["content"] == (
        "old transcript sentinel"
    )
    assert session_store.load_transcript(new.session_id) == []
    assert new.model_override_identity == MODEL_IDENTITY
    assert new.reasoning_override == REASONING_NONE
    db.close()


def test_route_preference_carryover_is_scoped_to_existing_routing_lane(store):
    first = _source(user_id="u1", chat_id="c1")
    second = _source(user_id="u2", chat_id="c2")
    first_entry = store.get_or_create_session(first)
    second_entry = store.get_or_create_session(second)
    assert first_entry.session_key != second_entry.session_key
    first_entry.model_override_identity = dict(MODEL_IDENTITY)
    store.persist()

    store.reset_session(first_entry.session_key, preserve_route_preferences=True)

    assert store.entry_for(first_entry.session_key).model_override_identity == MODEL_IDENTITY
    assert store.entry_for(second_entry.session_key).model_override_identity is None

    shared_a = SessionSource(
        platform=Platform.DISCORD,
        user_id="alice",
        chat_id="thread-1",
        thread_id="thread-1",
        chat_type="thread",
    )
    shared_b = SessionSource(
        platform=Platform.DISCORD,
        user_id="bob",
        chat_id="thread-1",
        thread_id="thread-1",
        chat_type="thread",
    )
    assert build_session_key(shared_a) == build_session_key(shared_b)


@pytest.mark.parametrize(
    ("model_identity", "reasoning", "expected_model", "expected_reasoning"),
    [
        (MODEL_IDENTITY, None, MODEL_IDENTITY, None),
        (None, REASONING_NONE, None, REASONING_NONE),
        (None, None, None, None),
    ],
)
def test_manual_reset_copies_each_preference_independently(
    store, model_identity, reasoning, expected_model, expected_reasoning
):
    old = store.get_or_create_session(_source())
    with store._lock:
        old.model_override_identity = model_identity
        old.reasoning_override = reasoning
        store._save()

    new = store.reset_session(old.session_key, preserve_route_preferences=True)

    assert new.model_override_identity == expected_model
    assert new.reasoning_override == expected_reasoning


def test_explicit_preference_clear_is_not_resurrected(store):
    old = _seed_preferences(store)
    with store._lock:
        old.model_override_identity = None
        old.reasoning_override = None
        store._save()

    new = store.reset_session(old.session_key, preserve_route_preferences=True)

    assert new.model_override_identity is None
    assert new.reasoning_override is None


def test_manual_reset_strips_tampered_secrets_from_model_identity(store):
    old = store.get_or_create_session(_source())
    with store._lock:
        old.model_override_identity = {
            **MODEL_IDENTITY,
            "api_key": "sk-must-not-survive",
            "access_token": "oauth-must-not-survive",
            "base_url": "https://user:pass@example.invalid/v1",
        }
        store._save()

    new = store.reset_session(old.session_key, preserve_route_preferences=True)

    assert new.model_override_identity == MODEL_IDENTITY
    raw = (store.sessions_dir / "sessions.json").read_text(encoding="utf-8")
    assert "sk-must-not-survive" not in raw
    assert "oauth-must-not-survive" not in raw
    assert "user:pass" not in raw


def _runner_for_manual_reset(store):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = GatewayConfig()
    runner.adapters = {}
    runner.session_store = store
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._last_resolved_model = {}
    runner._queued_events = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._running_agents = {}
    runner._pending_approvals = {}
    runner._background_tasks = set()
    runner._session_db = None
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._invalidate_session_run_generation = MagicMock()
    runner._release_running_agent_state = MagicMock()
    runner._evict_cached_agent = MagicMock()
    runner._clear_session_boundary_security_state = MagicMock()
    runner._reset_notice_session_info = MagicMock(return_value="")
    runner._telegram_topic_new_header = MagicMock(return_value="")
    runner._is_telegram_topic_lane = MagicMock(return_value=False)
    runner._reresolve_model_override_credentials = MagicMock(
        return_value={
            **MODEL_IDENTITY,
            "api_key": "fresh-runtime-secret",
            "base_url": "https://chatgpt.com/backend-api/codex",
        }
    )
    return runner


@pytest.mark.asyncio
async def test_manual_reset_persisted_entry_wins_map_divergence(store, monkeypatch):
    old = _seed_preferences(store)
    runner = _runner_for_manual_reset(store)
    runner._session_model_overrides[old.session_key] = {
        "model": "stale-map-model",
        "provider": "openrouter",
        "api_mode": "chat_completions",
        "api_key": "stale-secret",
    }
    runner._session_reasoning_overrides[old.session_key] = {
        "enabled": True,
        "effort": "low",
    }
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_runtime_config",
        lambda: {"session_reset": {"preserve_route_preferences_on_manual_reset": True}},
    )

    event = MessageEvent(text="/new", source=_source(), message_id="m1")
    await runner._handle_reset_command(event)

    new = store.entry_for(old.session_key)
    assert new.session_id != old.session_id
    assert new.model_override_identity == MODEL_IDENTITY
    assert new.reasoning_override == REASONING_NONE
    assert runner._session_model_overrides[old.session_key]["model"] == "gpt-5.6-sol"
    assert runner._session_model_overrides[old.session_key]["api_key"] == "fresh-runtime-secret"
    assert runner._session_reasoning_overrides[old.session_key] == REASONING_NONE
    assert old.session_key not in runner._pending_model_notes


@pytest.mark.asyncio
async def test_runtime_kill_switch_false_restores_legacy_clear(store, monkeypatch):
    old = _seed_preferences(store)
    runner = _runner_for_manual_reset(store)
    runner._session_model_overrides[old.session_key] = {
        **MODEL_IDENTITY,
        "api_key": "runtime-only",
    }
    runner._session_reasoning_overrides[old.session_key] = dict(REASONING_NONE)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_runtime_config",
        lambda: {"session_reset": {"preserve_route_preferences_on_manual_reset": False}},
    )

    await runner._handle_reset_command(
        MessageEvent(text="/reset", source=_source(), message_id="m2")
    )

    new = store.entry_for(old.session_key)
    assert new.model_override_identity is None
    assert new.reasoning_override is None
    assert old.session_key not in runner._session_model_overrides
    assert old.session_key not in runner._session_reasoning_overrides


@pytest.mark.asyncio
async def test_unreadable_kill_switch_cannot_wedge_reset_and_defaults_true(
    store, monkeypatch
):
    old = _seed_preferences(store)
    runner = _runner_for_manual_reset(store)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_runtime_config",
        MagicMock(side_effect=OSError("unreadable config")),
    )

    await runner._handle_reset_command(
        MessageEvent(text="/new", source=_source(), message_id="m3")
    )

    new = store.entry_for(old.session_key)
    assert new.session_id != old.session_id
    assert new.model_override_identity == MODEL_IDENTITY
    assert new.reasoning_override == REASONING_NONE


def test_default_config_enables_manual_reset_route_preference_carryover():
    from hermes_cli.config import DEFAULT_CONFIG

    assert (
        DEFAULT_CONFIG["session_reset"][
            "preserve_route_preferences_on_manual_reset"
        ]
        is True
    )


def test_malformed_yaml_defaults_true_and_logs_warning(tmp_path, monkeypatch, caplog):
    (tmp_path / "config.yaml").write_text("session_reset: [unterminated", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        preserved = gateway_run.GatewayRunner._preserve_route_preferences_on_manual_reset()

    assert preserved is True
    assert "config" in caplog.text.lower()
    assert "warning" in caplog.text.lower() or "could not" in caplog.text.lower()
