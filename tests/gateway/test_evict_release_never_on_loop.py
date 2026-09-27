# fork-only: the static half reads source (upstream AGENTS.md forbids that); do not port.
"""Soft release of an evicted agent must never run on the event-loop thread.

``_evict_cached_agent`` is called synchronously from ~30 gateway coroutines
(/model, /reasoning, /compress, the config toggles, ...).  The release it
schedules (``_release_evicted_agent_soft`` -> ``release_clients``) can fall
back to a child ``close()`` -> ``cleanup_browser`` -> CDP discovery
(``requests.get``, 10 s timeout), so it runs on a daemon thread.

The hole (t_fc4d28db): when ``Thread.start()`` raised -- thread exhaustion,
``RuntimeError: can't start new thread``, a condition this fleet has hit --
every eviction site released INLINE, i.e. on the loop.  The same shape sat in
the cross-process eviction and the memory-pressure valve.  All three now go
through ``GatewayRunner._release_agent_off_loop``.

When the thread cannot start, the helper still releases inline.  Dropping the
release would lose the pressure valve's end-of-session memory commit (#11205)
and its ``trim_memory``; a slow release under thread exhaustion is the lesser
harm.  The static walker does not see that fallback because it calls the
``target`` parameter, not a named release body.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.gateway._loop_atomic_write_reachability import (
    derive_scanned_modules,
    find_onloop_sink_sites,
)

SCAN_ROOTS = (
    "gateway",
    "plugins/platforms",
    "utils.py",
    "hermes_cli",
    "agent",
    "tools",
    "run_agent.py",
)
START_ROOTS = ("gateway", "plugins/platforms")

# The release bodies.  A coroutine that reaches any of them does the release
# on the loop thread.
RELEASE_SINKS = frozenset({
    "_release_evicted_agent_soft",
    "_commit_then_release_soft",
    "_release_pressure_batch",
})


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def modules():
    repo = _repo_root()
    return repo, derive_scanned_modules(repo, SCAN_ROOTS)


def test_no_coroutine_reaches_an_agent_release_body(modules):
    repo, mods = modules
    live = find_onloop_sink_sites(
        repo, mods, sink_names=RELEASE_SINKS, sink_dotted=(),
        start_roots=START_ROOTS,
    )
    assert not live, (
        "a gateway coroutine can run an evicted agent's release on the event "
        "loop (release_clients -> close -> cleanup_browser -> requests.get). "
        "Schedule it via GatewayRunner._release_agent_off_loop:\n"
        + "\n".join(f"  {s}" for s in live)
    )


def test_eviction_is_reachable_from_coroutines(modules):
    """Non-vacuity: the walker does see the ~30 synchronous eviction doors."""
    repo, mods = modules
    doors = find_onloop_sink_sites(
        repo, mods, sink_names={"_evict_cached_agent"}, sink_dotted=(),
        start_roots=START_ROOTS,
    )
    assert len(doors) >= 20, doors


# ---------------------------------------------------------------------------
# Behavioral: which thread runs the release when a coroutine evicts.
# ---------------------------------------------------------------------------


def _runner():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._running_agents = {}
    runner._peek_session_state = lambda key: None
    runner._running_agent_items = lambda: []
    return runner


def _evict_from_a_coroutine(runner, key):
    """Call _evict_cached_agent from a coroutine; return (loop_thread, release_threads)."""
    ran_on: list[int] = []
    done = threading.Event()

    def _soft(agent):
        ran_on.append(threading.get_ident())
        done.set()

    runner._release_evicted_agent_soft = _soft

    async def _slash():
        runner._evict_cached_agent(key)
        return threading.get_ident()

    loop_thread = asyncio.run(_slash())
    done.wait(timeout=2.0)
    return loop_thread, ran_on


def test_release_runs_off_the_loop_thread():
    runner = _runner()
    runner._agent_cache["discord:s1"] = (MagicMock(), "sig", 1)
    loop_thread, ran_on = _evict_from_a_coroutine(runner, "discord:s1")
    assert ran_on, "evicted agent was never released"
    assert loop_thread not in ran_on


def _no_threads(monkeypatch):
    def _boom(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", _boom)


def test_thread_exhaustion_still_releases(monkeypatch):
    """Thread.start() failing must not drop the release (review of #1314)."""
    _no_threads(monkeypatch)
    runner = _runner()
    runner._agent_cache["discord:s1"] = (MagicMock(), "sig", 1)
    _loop_thread, ran_on = _evict_from_a_coroutine(runner, "discord:s1")
    assert ran_on, "Thread.start() failed and the evicted agent was never released"
    assert "discord:s1" not in runner._agent_cache


def test_thread_exhaustion_pressure_valve_still_commits_and_trims(monkeypatch):
    """The pressure valve's fallback must keep the memory commit and the trim."""
    from collections import OrderedDict

    import gateway.agent_cache_pressure as acp
    import hermes_cli.mem_trim as mem_trim
    from gateway.agent_cache_pressure import AgentCacheBounds
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._agent_cache = OrderedDict()
    runner._agent_cache_lock = threading.Lock()
    runner._running_agents = {}
    runner._agent_cache_bounds_cache = AgentCacheBounds(
        memory_high_mb=1000, max_evictions_per_pass=8, protect_recent=1
    )
    monkeypatch.setattr(acp, "read_anon_rss_mb", lambda: 4000)

    committed: list = []
    released: list = []
    runner._commit_memory_before_soft_evict = (
        lambda agent, key: committed.append(key)
    )
    runner._release_evicted_agent_soft = lambda agent: released.append(agent)
    trims: list = []
    monkeypatch.setattr(
        mem_trim, "trim_memory", lambda **kw: trims.append(kw) or 0,
    )

    for i in range(3):
        agent = MagicMock()
        agent._session_messages = [{"role": "user", "content": "x"}] * 2
        agent._last_flushed_db_idx = 2
        runner._agent_cache[f"s{i}"] = (agent, "sig")

    _no_threads(monkeypatch)
    assert runner._sweep_agent_cache_under_pressure() == 2

    assert committed == ["s0", "s1"], "end-of-session memory commit was dropped"
    assert len(released) == 2
    assert trims and trims[0].get("force") is True, "trim_memory was skipped"
