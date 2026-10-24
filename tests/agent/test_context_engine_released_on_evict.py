"""Gateway cache eviction must release the per-agent context engine's resources.

Incident 2026-10-23 (Apollo's gateway, pid 89466): every idle-TTL eviction called
``release_clients()``, which closed the HTTP clients and the Codex child but never
touched ``agent.context_compressor``. Each LCM engine instance holds ~5 sqlite
connections to ``lcm.db``; ~175 evictions over 16 h left 1,996 open handles on one
file, the process hit launchd's 4,096 soft fd ceiling, and every file open failed
(``state.db`` writes, provider calls, the drain-request read). These tests pin the
contract: ``release_clients()`` and ``close()`` both call ``shutdown()`` on an engine
that has one, exactly once across repeated calls, and tolerate engines without it.
"""

import threading

from run_agent import AIAgent


class _EngineWithShutdown:
    def __init__(self):
        self.shutdown_calls = 0

    def shutdown(self):
        self.shutdown_calls += 1


class _EngineRaising(_EngineWithShutdown):
    def shutdown(self):
        super().shutdown()
        raise RuntimeError("store already closed")


class _EngineWithoutShutdown:
    pass


def _bare_agent(session_id: str, engine) -> AIAgent:
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = session_id
    agent.client = None
    agent._active_children_lock = threading.Lock()
    agent._active_children = set()
    agent._end_session_on_close = False
    agent._session_messages = []
    agent.context_compressor = engine
    return agent


def test_release_clients_shuts_down_context_engine_once():
    engine = _EngineWithShutdown()
    agent = _bare_agent("evict-1", engine)

    agent.release_clients()
    agent.release_clients()

    assert engine.shutdown_calls == 1
    # The engine reference is dropped so a resumed session rebuilds a fresh one
    # (init_agent re-selects the engine) instead of using closed connections.
    assert getattr(agent, "context_compressor", None) is None


def test_close_shuts_down_context_engine_once():
    engine = _EngineWithShutdown()
    agent = _bare_agent("close-1", engine)

    agent.close()
    agent.close()

    assert engine.shutdown_calls == 1


def test_release_then_close_shuts_down_once():
    engine = _EngineWithShutdown()
    agent = _bare_agent("both-1", engine)

    agent.release_clients()
    agent.close()

    assert engine.shutdown_calls == 1


def test_engine_without_shutdown_is_tolerated():
    agent = _bare_agent("noshutdown-1", _EngineWithoutShutdown())
    agent.release_clients()
    agent.close()
    assert getattr(agent, "context_compressor", None) is None


def test_engine_shutdown_error_never_blocks_release():
    engine = _EngineRaising()
    agent = _bare_agent("raise-1", engine)
    agent.release_clients()  # must not raise
    assert engine.shutdown_calls == 1
    assert getattr(agent, "context_compressor", None) is None


def test_no_engine_is_a_noop():
    agent = _bare_agent("none-1", None)
    agent.release_clients()
    agent.close()
