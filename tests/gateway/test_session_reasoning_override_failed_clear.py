"""A failed write-through of a /reasoning CLEAR must not be undone by the next turn's rehydrate.

``_set_session_reasoning_override(key, None)`` clears the in-memory override and then nulls the
persisted copy best-effort. If that persist raises, the old override is still on disk, and the next
``_resolve_session_reasoning_config`` rehydrates it because memory is empty: the user's
``/reasoning reset`` silently reverts one turn later (review on #98901).
"""
import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore

OVERRIDE = {"enabled": True, "effort": "high"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    import hermes_state

    def _raise():
        raise RuntimeError("SQLite disabled in test")

    monkeypatch.setattr(hermes_state, "SessionDB", _raise)
    return SessionStore(sessions_dir=tmp_path, config=GatewayConfig())


def _runner(store):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._session_reasoning_overrides = {}
    runner.session_store = store
    runner._load_reasoning_config = lambda model="": None
    return runner


def _key(store):
    return store.get_or_create_session(SessionSource(
        platform=Platform.TELEGRAM, user_id="u1", chat_id="c1", user_name="t", chat_type="dm")).session_key


def test_failed_clear_is_not_resurrected_by_the_next_rehydrate(store, monkeypatch):
    key = _key(store)
    runner = _runner(store)
    runner._set_session_reasoning_override(key, OVERRIDE)
    assert store.get_reasoning_override(key) == OVERRIDE

    real = store.set_reasoning_override

    def failing(session_key, override):
        raise OSError("disk full")

    monkeypatch.setattr(store, "set_reasoning_override", failing)
    runner._set_session_reasoning_override(key, None)
    assert store.get_reasoning_override(key) == OVERRIDE  # the persisted copy survived the failed clear

    # Next turn, persistence still failing: the cleared override must stay cleared.
    assert runner._resolve_session_reasoning_config(session_key=key) is None

    # Next turn after persistence recovers: the pending clear is retried and lands on disk.
    monkeypatch.setattr(store, "set_reasoning_override", real)
    assert runner._resolve_session_reasoning_config(session_key=key) is None
    assert store.get_reasoning_override(key) is None


def test_a_new_set_after_a_failed_clear_wins(store, monkeypatch):
    key = _key(store)
    runner = _runner(store)
    runner._set_session_reasoning_override(key, OVERRIDE)
    monkeypatch.setattr(store, "set_reasoning_override", lambda *a: (_ for _ in ()).throw(OSError("x")))
    runner._set_session_reasoning_override(key, None)
    monkeypatch.undo()

    low = {"enabled": True, "effort": "low"}
    runner._set_session_reasoning_override(key, low)
    assert runner._resolve_session_reasoning_config(session_key=key) == low
    assert store.get_reasoning_override(key) == low
