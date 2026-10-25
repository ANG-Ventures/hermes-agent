"""t_0d987578: a gateway-evicted agent's LCM engine releases its lcm.db handles deterministically.

Live 2026-10-24 15:1x PT: 130 descriptors on lcm.db in one gateway process. #2237 added
``_shutdown_context_engine`` to ``release_clients()``/``close()``, pinned there with stub engines
only. These tests drive a REAL ``LCMEngine`` on a scratch lcm.db through the REAL gateway paths
(``_evict_cached_agent`` -> release thread -> ``release_clients``; ``_cleanup_agent_resources`` ->
``close``) and count kernel descriptors on the db inode. Each runs with ``gc.disable()`` too, so a
pass shows the handles were closed explicitly and not left to the garbage collector.
"""

from __future__ import annotations

import gc
import os
import threading
from collections import OrderedDict
from pathlib import Path

import pytest

from gateway.status import count_fds_on


def _fds_on(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    st = os.stat(db_path)
    n = count_fds_on({(st.st_dev, st.st_ino)})
    if n is None:
        pytest.skip("no /dev/fd on this platform")
    return n


@pytest.fixture
def lcm_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("LCM_DATABASE_PATH", raising=False)
    return home


@pytest.fixture(params=["gc-enabled", "gc-disabled"])
def gc_mode(request):
    disabled = request.param == "gc-disabled"
    was_enabled = gc.isenabled()
    if disabled:
        gc.disable()
    try:
        yield disabled
    finally:
        if was_enabled:
            gc.enable()


def _lcm_engine(home: Path, session_id: str):
    from plugins.context_engine import load_context_engine

    engine = load_context_engine("lcm")
    assert engine is not None and engine.name == "lcm"
    engine.on_session_start(session_id, hermes_home=str(home), platform="discord")
    engine.ingest([
        {"role": "user", "content": f"question for {session_id}"},
        {"role": "assistant", "content": f"answer for {session_id}"},
    ])
    return engine


def _agent_with(engine):
    from run_agent import AIAgent

    agent = AIAgent(
        model="anthropic/claude-sonnet-4", api_key="test",
        base_url="https://openrouter.ai/api/v1", provider="openrouter",
        max_iterations=5, quiet_mode=True, skip_context_files=True, skip_memory=True,
    )
    agent.context_compressor = engine
    return agent


def _runner():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._agent_cache = OrderedDict()
    runner._agent_cache_lock = threading.Lock()
    runner._running_agents = {}
    return runner


def _join_release_threads(prefix: str, timeout: float = 10.0) -> None:
    for t in [t for t in threading.enumerate() if t.name.startswith(prefix)]:
        t.join(timeout)
        assert not t.is_alive(), f"release thread {t.name} still running after {timeout}s"


def test_evict_cached_agent_returns_lcm_db_fds_to_baseline(lcm_home, gc_mode):
    db_path = lcm_home / "lcm.db"
    baseline = _fds_on(db_path)
    agent = _agent_with(_lcm_engine(lcm_home, "evict-real-1"))
    held = _fds_on(db_path)
    assert held > baseline, f"premise: a live LCM engine holds lcm.db handles ({held} vs baseline {baseline})"

    runner = _runner()
    runner._agent_cache["discord:evict-real-1"] = (agent, "sig")
    runner._evict_cached_agent("discord:evict-real-1")
    _join_release_threads("agent-evict-")

    # The agent object is still alive here (this frame holds it): only explicit shutdown can pass.
    assert agent.context_compressor is None
    assert _fds_on(db_path) == baseline


def test_session_boundary_cleanup_returns_lcm_db_fds_to_baseline(lcm_home, gc_mode):
    db_path = lcm_home / "lcm.db"
    baseline = _fds_on(db_path)
    agent = _agent_with(_lcm_engine(lcm_home, "boundary-real-1"))
    assert _fds_on(db_path) > baseline

    _runner()._cleanup_agent_resources(agent)

    assert _fds_on(db_path) == baseline


def test_evicted_session_rebuilds_engine_and_reads_its_dag(lcm_home):
    """Resume after eviction: a fresh engine on the same session id sees the persisted messages."""
    db_path = lcm_home / "lcm.db"
    agent = _agent_with(_lcm_engine(lcm_home, "resume-real-1"))
    runner = _runner()
    runner._agent_cache["discord:resume-real-1"] = (agent, "sig")
    runner._evict_cached_agent("discord:resume-real-1")
    _join_release_threads("agent-evict-")
    assert _fds_on(db_path) == 0

    from plugins.context_engine import load_context_engine

    rebuilt = load_context_engine("lcm")
    rebuilt.on_session_start("resume-real-1", hermes_home=str(lcm_home), platform="discord")
    try:
        rows = rebuilt._store.get_session_messages("resume-real-1")
        contents = [r.get("content") if isinstance(r, dict) else getattr(r, "content", None) for r in rows]
        assert "question for resume-real-1" in contents
        assert "answer for resume-real-1" in contents
    finally:
        rebuilt.shutdown()


# -- D2/D3: idempotent shutdown + __del__ backstop -------------------------------------------------


def _count_closes(engine, monkeypatch) -> dict[str, int]:
    counts: dict[str, int] = {}
    for attr in ("_store", "_dag", "_lifecycle", "_assertions", "_query_views", "_adaptive_retrieval"):
        helper = getattr(engine, attr, None)
        if helper is None:
            continue
        real = helper.close

        def counting(_real=real, _attr=attr):
            counts[_attr] = counts.get(_attr, 0) + 1
            return _real()

        monkeypatch.setattr(helper, "close", counting)
    return counts


def test_shutdown_twice_is_a_noop_the_second_time(lcm_home, monkeypatch):
    engine = _lcm_engine(lcm_home, "twice-1")
    counts = _count_closes(engine, monkeypatch)

    engine.shutdown()
    after_first = dict(counts)
    engine.shutdown()  # must not raise

    assert after_first.get("_store") == 1 and after_first.get("_dag") == 1 and after_first.get("_lifecycle") == 1
    assert counts == after_first, "second shutdown() must not touch the stores again"
    assert _fds_on(lcm_home / "lcm.db") == 0


def test_store_close_tolerates_double_close(lcm_home):
    engine = _lcm_engine(lcm_home, "double-close-1")
    for attr in ("_store", "_dag", "_lifecycle", "_assertions", "_query_views", "_adaptive_retrieval"):
        helper = getattr(engine, attr, None)
        if helper is not None:
            helper.close()
            helper.close()
    engine.shutdown()


def test_del_on_never_shut_engine_closes_its_stores(lcm_home, monkeypatch):
    engine = _lcm_engine(lcm_home, "del-1")
    counts = _count_closes(engine, monkeypatch)

    engine.__del__()

    assert counts.get("_store") == 1 and counts.get("_dag") == 1 and counts.get("_lifecycle") == 1
    assert _fds_on(lcm_home / "lcm.db") == 0
    engine.shutdown()  # after the finalizer ran: still a no-op, still no raise
    assert counts.get("_store") == 1


def test_del_never_raises_on_half_built_engine():
    from plugins.context_engine.lcm.engine import LCMEngine

    engine = LCMEngine.__new__(LCMEngine)  # no stores bound, no _closed flag
    engine.__del__()
