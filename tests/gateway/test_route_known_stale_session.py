"""Route-note ``Known`` sink vs ``/new``-``/reset`` (Prism 15b8d310fde7 on #2456).

The sink ``GatewayRunner._bind_route_known`` hands the agent is bound once per turn. A call that
commits after the session key was rotated to a fresh conversation must not write the OLD
conversation's ``Known`` into the new entry; it fails, so the agent's in-memory copy does not
advance either.
"""

from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore


def _source() -> SessionSource:
    return SessionSource(platform=Platform.TELEGRAM, user_id="u1", chat_id="c1", user_name="t", chat_type="dm")


@pytest.fixture
def store(tmp_path, monkeypatch):
    import hermes_state

    def _raise():
        raise RuntimeError("SQLite disabled in test")

    monkeypatch.setattr(hermes_state, "SessionDB", _raise)
    st = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    assert st._db is None
    return st


def _bind(store, session_key):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    agent = SimpleNamespace()
    runner._bind_route_known(agent, session_key)
    return agent


def test_commit_after_reset_does_not_write_into_the_new_conversation(store):
    entry = store.get_or_create_session(_source())
    key, old_sid = entry.session_key, entry.session_id
    store.set_route_known(key, {"intended": {"model": "seed"}})
    agent = _bind(store, key)  # turn on session A captures A's id

    new_entry = store.reset_session(key)  # /new: same key, session B
    assert new_entry.session_id != old_sid and new_entry.route_known is None

    stale = {"intended": {"model": "stale-A"}, "serving": {"model": "stale-A"}}
    with pytest.raises(RuntimeError):
        agent._route_known_sink(stale)  # A's late commit
    assert store._entries[key].route_known is None  # B untouched

    # B's own turn binds against B and writes normally.
    agent_b = _bind(store, key)
    agent_b._route_known_sink({"intended": {"model": "B"}})
    assert store._entries[key].route_known == {"intended": {"model": "B"}}


def test_sink_writes_when_the_session_is_unchanged(store):
    entry = store.get_or_create_session(_source())
    agent = _bind(store, entry.session_key)
    agent._route_known_sink({"intended": {"model": "m"}})
    assert store._entries[entry.session_key].route_known == {"intended": {"model": "m"}}


def test_bind_is_fail_open_without_a_session_store():
    """Operator round 4 on #2456: ``run_turn_runner`` calls the bind on every turn, and runners built
    without ``session_store`` (gateway rigs, some construction paths) must still complete the turn.
    No store -> no sink -> Known stays None (the note is simply re-sent; designed degraded mode)."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    assert not hasattr(runner, "session_store")
    agent = SimpleNamespace()
    runner._bind_route_known(agent, "agent:main:telegram:dm:u1")  # must not raise
    assert agent._route_known is None
    assert agent._route_known_sink is None


def test_bind_is_fail_open_when_the_store_raises():
    from gateway.run import GatewayRunner

    class _Broken:
        @property
        def _entries(self):
            raise RuntimeError("store exploded")

    runner = object.__new__(GatewayRunner)
    runner.session_store = _Broken()
    agent = SimpleNamespace()
    runner._bind_route_known(agent, "k")
    assert agent._route_known is None and agent._route_known_sink is None
