"""LCM must not accumulate sqlite handles on lcm.db across agent builds/evictions.

2026-10-23: the default gateway EMFILE-wedged with 1987 open handles on lcm.db.
Every AIAgent build calls ``load_context_engine("lcm")``, which re-runs the
plugin's ``register()`` and so constructs a fresh LCMEngine (MessageStore +
SummaryDAG + LifecycleStateStore = 3 connections). ``register()`` also appended
a ``post_llm_call`` closure that captured that engine onto the process-global
PluginManager, so no evicted agent's engine could ever be collected and its
connections stayed open for the life of the process.
"""

from __future__ import annotations

import gc
import os
import sqlite3
from pathlib import Path

import pytest

N_AGENTS = 200
# The newest engine may still be alive (the hook's weak fallback, the active
# engine registry) plus slack for a background maintenance connection.
MAX_LCM_DB_HANDLES = 12


def _fd_path(fd: int) -> str | None:
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        pass
    try:
        import fcntl

        f_getpath = getattr(fcntl, "F_GETPATH", None)
        if f_getpath is None:
            return None
        return fcntl.fcntl(fd, f_getpath, bytes(1024)).split(b"\0", 1)[0].decode()
    except OSError:
        return None


def _open_handles_on(db_path: Path) -> int:
    fd_dir = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    if not os.path.isdir(fd_dir):
        pytest.skip("no per-process fd directory on this platform")
    target = os.path.realpath(db_path)
    count = 0
    for name in os.listdir(fd_dir):
        path = _fd_path(int(name))
        if path and os.path.realpath(path) == target:
            count += 1
    return count


def _load_lcm_engine():
    from plugins.context_engine import load_context_engine

    engine = load_context_engine("lcm")
    assert engine is not None and engine.name == "lcm"
    return engine


def _lcm_post_llm_hooks():
    from hermes_cli.plugins import get_plugin_manager

    hooks = get_plugin_manager()._hooks.get("post_llm_call", [])
    return [h for h in hooks if getattr(h, "__module__", "") == "plugins.context_engine.lcm"]


@pytest.fixture
def lcm_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("LCM_DATABASE_PATH", raising=False)
    return home


def test_agent_build_and_evict_cycles_keep_lcm_db_handles_bounded(lcm_home):
    db_path = lcm_home / "lcm.db"
    for i in range(N_AGENTS):
        engine = _load_lcm_engine()
        assert Path(engine._store.db_path) == db_path
        engine.on_session_start(f"evict-{i}", hermes_home=str(lcm_home), platform="discord")
        # Eviction: the gateway drops its only reference to the agent (and so
        # to the engine) without any context-engine teardown call.
        del engine
    gc.collect()

    handles = _open_handles_on(db_path)
    assert handles <= MAX_LCM_DB_HANDLES, (
        f"{handles} open handles on lcm.db after {N_AGENTS} agent build/evict cycles "
        f"(bound {MAX_LCM_DB_HANDLES}); evicted engines are being kept alive"
    )
    assert len(_lcm_post_llm_hooks()) <= 1, "register() must replace, not stack, its post_llm_call hook"


def test_post_llm_call_hook_still_falls_back_to_the_live_registered_engine(lcm_home):
    """Hosts that pass no context_compressor and no bound session id still ingest
    through the engine register() built, for as long as that engine is alive."""
    engine = _load_lcm_engine()
    seen = []
    engine.ingest = lambda history: seen.append(list(history))
    (hook,) = _lcm_post_llm_hooks()
    history = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    hook(conversation_history=history, session_id="", platform="")
    assert seen == [history]


def test_every_connection_opened_by_an_engine_is_closed_by_its_owner(lcm_home, monkeypatch):
    """Each sqlite3.connect made for an engine is closed explicitly by the
    helper that owns it, not left to Connection deallocation: the test holds a
    strong ref to every connection, so only an owner's close() can close it."""
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    for i in range(5):
        engine = _load_lcm_engine()
        engine.on_session_start(f"owner-{i}", hermes_home=str(lcm_home), platform="cli")
        engine.ingest([
            {"role": "user", "content": f"question {i}"},
            {"role": "assistant", "content": f"answer {i}"},
        ])
        drain = getattr(engine, "drain_rollup_maintenance", None)
        if callable(drain):
            drain(timeout=10)
        del engine
    gc.collect()
    monkeypatch.setattr(sqlite3, "connect", real_connect)

    assert opened, "premise: the engine lifecycle opened no connections"
    still_open = []
    for conn in opened:
        try:
            conn.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            continue
        still_open.append(conn)
    # The newest engine may legitimately survive (weak hook fallback); it owns
    # at most a handful of connections. Everything older must be closed.
    assert len(still_open) <= 6, (
        f"{len(still_open)} of {len(opened)} connections opened for 5 dropped engines are still open"
    )
