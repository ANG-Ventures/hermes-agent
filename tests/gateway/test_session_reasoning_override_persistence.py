"""Per-session /reasoning overrides must survive gateway restarts.

The gateway persisted the *model* half of the session-override pair and not
the *reasoning* half: a restart silently reverted ``/reasoning high`` to the
config default while a ``/model`` switch in the same session survived.

``SessionEntry.reasoning_override`` closes that asymmetry — the effort is
written through when /reasoning runs and lazily rehydrated on first use.

Covers:
  - the override survives a simulated restart (a second SessionStore instance
    reading the same sessions dir, and a fresh runner rehydrating from it)
  - /reasoning reset and /new clear the persisted value so a restart cannot
    resurrect it
  - only the reasoning-effort shape is serialized (no smuggled credentials)
  - a pre-existing sessions.json with no reasoning_override key loads clean
"""
import json

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import (
    SessionSource,
    SessionStore,
    sanitize_reasoning_override,
)

OVERRIDE = {"enabled": True, "effort": "high"}


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


@pytest.fixture
def store_factory(tmp_path, monkeypatch):
    """Build SessionStores over a shared sessions dir, without SQLite."""

    def _raise():
        raise RuntimeError("SQLite disabled in test")

    import hermes_state

    monkeypatch.setattr(hermes_state, "SessionDB", _raise)

    def _make() -> SessionStore:
        store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
        assert store._db is None
        return store

    return _make


def _sessions_json(tmp_path) -> str:
    return (tmp_path / "sessions.json").read_text(encoding="utf-8")


def _make_runner(store):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    return runner


def test_override_persists_and_survives_restart(store_factory):
    store = store_factory()
    session_key = store.get_or_create_session(_make_source()).session_key

    store.set_reasoning_override(session_key, OVERRIDE)

    # Simulated restart: a brand-new store instance reads the same dir.
    store2 = store_factory()
    assert store2.get_reasoning_override(session_key) == OVERRIDE


def test_runner_writes_through_and_rehydrates_after_restart(store_factory):
    store = store_factory()
    session_key = store.get_or_create_session(_make_source()).session_key

    # /reasoning high on the live runner.
    _make_runner(store)._set_session_reasoning_override(session_key, OVERRIDE)

    # Simulated restart: fresh store + fresh runner with no in-memory state.
    runner = _make_runner(store_factory())
    assert runner._peek_session_state(session_key) is None

    runner._rehydrate_session_reasoning_override(session_key)

    state = runner._peek_session_state(session_key)
    assert state is not None
    assert state.conversation.reasoning_override == OVERRIDE


def test_resolve_rehydrates_on_first_use(store_factory, monkeypatch):
    store = store_factory()
    session_key = store.get_or_create_session(_make_source()).session_key
    _make_runner(store)._set_session_reasoning_override(session_key, OVERRIDE)

    runner = _make_runner(store_factory())
    monkeypatch.setattr(
        type(runner), "_load_reasoning_config", staticmethod(lambda model="": None)
    )

    # The restarted runner resolves the session's effort without any prior
    # in-memory state — the config default would have been None.
    assert runner._resolve_session_reasoning_config(session_key=session_key) == OVERRIDE


def test_live_in_memory_state_wins_over_persisted(store_factory):
    store = store_factory()
    session_key = store.get_or_create_session(_make_source()).session_key
    store.set_reasoning_override(session_key, OVERRIDE)

    runner = _make_runner(store)
    live = {"enabled": True, "effort": "low"}
    runner._session_state(session_key).conversation.reasoning_override = live

    runner._rehydrate_session_reasoning_override(session_key)

    state = runner._peek_session_state(session_key)
    assert state is not None
    assert state.conversation.reasoning_override == live


def test_override_set_before_the_entry_exists_is_not_lost(store_factory):
    """/reasoning runs before the routing entry exists on a fresh chat.

    Its handler derives the session key but never calls
    get_or_create_session, so a write-through that required an existing entry
    would silently no-op — the effort would be gone at the next restart.
    """
    store = store_factory()
    source = _make_source()
    session_key = store._generate_session_key(source)
    assert session_key not in store._entries

    _make_runner(store)._set_session_reasoning_override(session_key, OVERRIDE)

    # The first real message creates the entry; the override rides along.
    entry = store.get_or_create_session(source)
    assert entry.session_key == session_key
    assert entry.reasoning_override == OVERRIDE
    assert store.get_reasoning_override(session_key) == OVERRIDE

    # And it survives the restart, which is the whole point.
    assert store_factory().get_reasoning_override(session_key) == OVERRIDE


def test_clear_before_the_entry_exists_drops_the_parked_value(store_factory):
    store = store_factory()
    source = _make_source()
    session_key = store._generate_session_key(source)
    runner = _make_runner(store)

    runner._set_session_reasoning_override(session_key, OVERRIDE)
    runner._set_session_reasoning_override(session_key, None)

    assert store.get_reasoning_override(session_key) is None
    assert store.get_or_create_session(source).reasoning_override is None


def test_reset_clears_persisted_override(store_factory):
    store = store_factory()
    session_key = store.get_or_create_session(_make_source()).session_key
    runner = _make_runner(store)
    runner._set_session_reasoning_override(session_key, OVERRIDE)

    # /reasoning reset goes through the same door.
    runner._set_session_reasoning_override(session_key, None)

    assert store.get_reasoning_override(session_key) is None
    assert store_factory().get_reasoning_override(session_key) is None


def test_new_session_clears_persisted_override(store_factory, tmp_path):
    store = store_factory()
    session_key = store.get_or_create_session(_make_source()).session_key
    _make_runner(store)._set_session_reasoning_override(session_key, OVERRIDE)
    assert store.get_reasoning_override(session_key) == OVERRIDE

    # /new rotates the entry — a conversation boundary drops the override.
    store.reset_session(session_key)

    assert store.get_reasoning_override(session_key) is None
    assert "reasoning_override" not in _sessions_json(tmp_path)
    # And it does not come back after a restart.
    assert store_factory().get_reasoning_override(session_key) is None


def test_expiry_finalization_clears_persisted_override(store_factory):
    store = store_factory()
    entry = store.get_or_create_session(_make_source())
    store.set_reasoning_override(entry.session_key, OVERRIDE)

    store.set_expiry_finalized(entry)

    assert store.get_reasoning_override(entry.session_key) is None
    assert store_factory().get_reasoning_override(entry.session_key) is None


def test_expiry_giveup_path_preserves_override(store_factory):
    store = store_factory()
    entry = store.get_or_create_session(_make_source())
    store.set_reasoning_override(entry.session_key, OVERRIDE)

    store.set_expiry_finalized(entry, clear_session_overrides=False)

    assert store.get_reasoning_override(entry.session_key) == OVERRIDE


def test_only_the_effort_shape_is_serialized(store_factory, tmp_path):
    """Nothing beyond {enabled, effort} reaches disk."""
    store = store_factory()
    session_key = store.get_or_create_session(_make_source()).session_key

    store.set_reasoning_override(
        session_key,
        {"enabled": True, "effort": "high", "api_key": "«redacted:sk-…»"},
    )

    assert store.get_reasoning_override(session_key) == OVERRIDE
    raw = _sessions_json(tmp_path)
    assert "api_key" not in raw
    assert "«redacted:sk-…»" not in raw
    persisted = json.loads(raw)[session_key]["reasoning_override"]
    assert set(persisted) <= {"enabled", "effort"}


def test_disabled_override_round_trips(store_factory):
    """``/reasoning none`` is {"enabled": False} — a falsy-but-real override."""
    store = store_factory()
    session_key = store.get_or_create_session(_make_source()).session_key

    store.set_reasoning_override(session_key, {"enabled": False})

    assert store_factory().get_reasoning_override(session_key) == {"enabled": False}


def test_legacy_sessions_json_without_the_key_loads(store_factory, tmp_path):
    """A file written before this field must load without error."""
    store = store_factory()
    session_key = store.get_or_create_session(_make_source()).session_key

    raw = json.loads(_sessions_json(tmp_path))
    assert "reasoning_override" not in raw[session_key]

    reloaded = store_factory()
    assert reloaded.get_reasoning_override(session_key) is None
    assert reloaded.get_or_create_session(_make_source()).session_key == session_key


def test_sanitize_reasoning_override():
    assert sanitize_reasoning_override(None) is None
    assert sanitize_reasoning_override({}) is None
    assert sanitize_reasoning_override("high") is None  # type: ignore[arg-type]
    # No enabled flag: not a reasoning override.
    assert sanitize_reasoning_override({"effort": "high"}) is None
    assert sanitize_reasoning_override({"enabled": False}) == {"enabled": False}
    assert sanitize_reasoning_override(OVERRIDE) == OVERRIDE
    assert sanitize_reasoning_override(
        {"enabled": True, "effort": "high", "api_key": "sk-x"}
    ) == OVERRIDE
