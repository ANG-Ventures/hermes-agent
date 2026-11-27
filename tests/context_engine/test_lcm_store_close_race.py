"""t_055f9d8d: LCM store close() must never race a live execute on the same connection.

Incident 2026-11-02 06:50:30 PST, Apollo gateway pid 42841: SIGSEGV (EXC_BAD_ACCESS 0x8 in
``_pysqlite_query_execute``). faulthandler showed, at the same instant, an eviction thread inside
``MessageStore.close()`` (``run_agent_cache._release_evicted_agent_soft`` -> ``release_clients`` ->
``_shutdown_context_engine`` -> ``LCMEngine.shutdown``) while two live turns of the SAME agent's
engine were in ``_append_protected_batch`` (preflight ingest) and ``write_metadata_json``
(``update_from_response`` telemetry). The connection is ``check_same_thread=False``; freeing it
under a running statement is a use-after-free in ``_sqlite3``.

How the evicted engine was still in use: ``/stop`` at 06:50:27 ran ``_drop_turn_slot`` ->
``_release_running_agent_state`` (clears ``turn.agent``) THEN ``_evict_cached_agent``. The
mid-turn guard in ``_evict_cached_agent`` reads ``turn.agent``, which was just cleared, so the
still-draining turn's agent was released on a daemon thread while its executor thread kept
running. ``test_stop_releases_engine_of_a_still_running_turn`` pins that path.

Two halves of the fix, one test each:
  * store: ``close()`` is mutually exclusive with every connection use; after close a call raises
    the typed ``StoreClosed`` (a ``sqlite3.Error``) and never touches the dead connection;
  * agent: ``release_clients()`` during an in-flight ``run_conversation`` defers the engine
    shutdown to the end of that turn, so the turn never sees a closed store.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
from collections import OrderedDict
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Runs in a child interpreter: on the unfixed store the race is a SIGSEGV, which must fail this
# test instead of killing the runner. Exit 0 = clean, 1 = an untyped exception reached a caller.
_HAMMER = textwrap.dedent(
    """
    import sqlite3, sys, threading, time
    from plugins.context_engine.lcm import store as store_mod
    from plugins.context_engine.lcm.config import LCMConfig

    db = sys.argv[1]
    closed_type = getattr(store_mod, "StoreClosed", None)
    bad = []
    msgs = [{"role": "user", "content": "x" * 200, "timestamp": 1.0} for _ in range(8)]

    def writer(store, stop, kind, i):
        n = 0
        while not stop.is_set():
            try:
                if kind == "batch":
                    store._append_protected_batch("s", msgs)
                else:
                    store.write_metadata_json([f"k{i}"], str(n))
                    store.read_metadata_json(f"k{i}")
            except Exception as exc:
                if closed_type is not None and isinstance(exc, closed_type):
                    return
                bad.append(repr(exc))
                return
            n += 1

    for _round in range(int(sys.argv[2])):
        store = store_mod.MessageStore(db, ingest_protection_config=LCMConfig(database_path=db))
        stop = threading.Event()
        threads = [threading.Thread(target=writer, args=(store, stop, k, i))
                   for i, k in enumerate(["batch", "meta"] * 4)]
        for t in threads:
            t.start()
        time.sleep(0.02)
        closer = threading.Thread(target=lambda: [store.close() for _ in range(50)])
        closer.start()
        closer.join()
        stop.set()
        for t in threads:
            t.join(10)
        if bad:
            break
    print("\\n".join(bad[:5]))
    sys.exit(1 if bad else 0)
    """
)


@pytest.mark.slow_test(180, reason="40 rounds x 8 writer threads x 50 close() calls in a faulthandler subprocess: "
                                   "7 s on a quiet host, ~70 s on a loaded one (ci at load 350 tripped the 60 s guard, "
                                   "2026-11-02); the race needs the volume")
def test_close_racing_writers_never_crashes_or_leaks_untyped_errors(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", _HAMMER, str(tmp_path / "lcm.db"), "40"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    assert proc.returncode != -11, f"SIGSEGV: close() freed the connection under a live execute\n{proc.stderr[-2000:]}"
    assert proc.returncode == 0, (
        f"rc={proc.returncode}; an exception other than StoreClosed reached a store caller:\n"
        f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    )


def test_closed_store_raises_typed_error_and_never_touches_dead_conn(tmp_path):
    from plugins.context_engine.lcm.config import LCMConfig
    from plugins.context_engine.lcm.store import MessageStore, StoreClosed

    db = str(tmp_path / "lcm.db")
    store = MessageStore(db, ingest_protection_config=LCMConfig(database_path=db))
    store.append("s", {"role": "user", "content": "hi"})
    store.close()
    store.close()  # idempotent
    assert issubclass(StoreClosed, sqlite3_error())
    for call in (
        lambda: store.append("s", {"role": "user", "content": "hi"}),
        lambda: store._append_protected_batch("s", [{"role": "user", "content": "hi"}]),
        lambda: store.get_session_messages("s"),
        lambda: store.search("hi"),
    ):
        with pytest.raises(StoreClosed):
            call()
    # Best-effort metadata paths keep their documented closed-store answers.
    assert store.read_metadata_json("k") is None
    assert store.write_metadata_json(["k"], "1") is False


def sqlite3_error():
    import sqlite3

    return sqlite3.Error


# -- agent half: an engine in use by a running turn is not shut down under it ---------------------


@pytest.fixture
def lcm_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("LCM_DATABASE_PATH", raising=False)
    return home


def _agent_with_lcm(home: Path, session_id: str):
    from plugins.context_engine import load_context_engine
    from run_agent import AIAgent

    engine = load_context_engine("lcm")
    engine.on_session_start(session_id, hermes_home=str(home), platform="discord")
    agent = AIAgent(
        model="anthropic/claude-sonnet-4", api_key="test",
        base_url="https://openrouter.ai/api/v1", provider="openrouter",
        max_iterations=5, quiet_mode=True, skip_context_files=True, skip_memory=True,
    )
    agent.context_compressor = engine
    return agent, engine


def _turn_that_uses_engine_after(agent, engine, monkeypatch, *, in_turn, done, seen):
    """Patch the loop body run_conversation forwards to: signal, wait for the release, then
    ingest through the engine the way preflight compaction does."""
    import agent.conversation_loop as loop

    def fake_loop(self, *a, **k):
        in_turn.set()
        assert done.wait(10)
        seen["store_live"] = engine._store.connection is not None
        engine.ingest([{"role": "user", "content": "after release"}])
        seen["rows"] = len(engine._store.get_session_messages("mid-turn-1"))
        return {"final_response": "ok", "messages": []}

    monkeypatch.setattr(loop, "run_conversation", fake_loop)


def test_release_clients_mid_turn_defers_engine_shutdown_to_turn_end(lcm_home, monkeypatch):
    agent, engine = _agent_with_lcm(lcm_home, "mid-turn-1")
    in_turn, released = threading.Event(), threading.Event()
    seen: dict = {}
    _turn_that_uses_engine_after(agent, engine, monkeypatch, in_turn=in_turn, done=released, seen=seen)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("r", agent.run_conversation("hi")))
    t.start()
    assert in_turn.wait(10)
    agent.release_clients()  # the eviction thread, while the turn is live
    released.set()
    t.join(15)
    assert not t.is_alive()
    assert seen.get("store_live") is True, "release_clients closed the store under the running turn"
    assert seen.get("rows", 0) >= 1, seen  # the turn's ingest landed
    assert result["r"]["final_response"] == "ok", result
    assert agent.context_compressor is None, "engine is still released, at the end of the turn"
    assert getattr(engine, "_closed", False), "the deferred shutdown ran when the turn finished"
    assert engine._store.connection is None


def test_stop_releases_engine_of_a_still_running_turn(lcm_home, monkeypatch):
    """Mechanism proof (b): /stop's _drop_turn_slot clears turn.agent BEFORE _evict_cached_agent,
    so the mid-turn guard misses and the live turn's agent is soft-released."""
    from gateway.run import GatewayRunner

    agent, engine = _agent_with_lcm(lcm_home, "stop-1")
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._agent_cache = OrderedDict()
    runner._agent_cache_lock = threading.Lock()
    runner._running_agents = {}
    key = "agent:main:discord:thread:1:1"
    runner._agent_cache[key] = (agent, "sig")
    runner._session_state(key).turn.agent = agent  # turn is running
    monkeypatch.setattr(runner, "_persist_active_agents", lambda: None)
    monkeypatch.setattr(runner, "_arm_deferred_restart_after_release", lambda *a, **k: None)

    released_agents = []
    monkeypatch.setattr(runner, "_spawn_release_thread",
                        lambda target, args, name, **k: released_agents.append(args[0]))
    runner._evict_cached_agent(key)
    assert released_agents == [], "premise: a plain eviction skips a mid-turn agent"

    runner._agent_cache[key] = (agent, "sig")
    runner._drop_turn_slot(key)
    assert released_agents == [agent], "the /stop path releases the still-running turn's agent"


@pytest.mark.parametrize("path", ["immediate", "deferred"])
def test_turn_admitted_during_shutdown_waits_for_it(path):
    """Prism cbdcad969e21 / d6e160ee056a: shutdown and turn admission are atomic. A turn that
    enters while the engine is being torn down must not start until the teardown is finished."""
    from agent.context_engine_owners import enter_turn, exit_turn, shutdown_or_defer

    class _A:
        pass

    agent = _A()
    in_shutdown, let_finish = threading.Event(), threading.Event()
    log: list[str] = []

    def shutdown():
        in_shutdown.set()
        assert let_finish.wait(10)
        log.append("shutdown-done")

    if path == "immediate":
        teardown = threading.Thread(target=shutdown_or_defer, args=(agent, shutdown))
    else:
        enter_turn(agent)
        assert shutdown_or_defer(agent, shutdown) is False  # deferred to the turn's exit
        teardown = threading.Thread(target=exit_turn, args=(agent, shutdown))
    teardown.start()
    assert in_shutdown.wait(10)

    def second_turn():
        enter_turn(agent)
        log.append("turn-admitted")
        exit_turn(agent, shutdown)

    turn = threading.Thread(target=second_turn)
    turn.start()
    turn.join(0.3)
    assert turn.is_alive(), "a turn was admitted while the engine was being shut down"
    let_finish.set()
    teardown.join(10)
    turn.join(10)
    assert log == ["shutdown-done", "turn-admitted"]


# -- t_35b4c55d: the same race in every other long-lived LCM connection ---------------------------
#
# Gateway SIGSEGV 2026-11-10 18:10:59 PST (EXC_BAD_ACCESS 0x8 in _pysqlite_query_execute), faulting
# thread in LifecycleStateStore.get_by_session <- bind_session <- on_session_start <- the LCM
# post_llm_call hook. #2540 serialized MessageStore only; LifecycleStateStore.close() (and the
# close() of SummaryDAG, RollupStore, VectorStore) never took the lock its executes hold, and raw
# users of ``.connection`` take no lock at all. The post_llm_call hook runs on an abandonable
# hook-timeout worker thread (plugins_dispatch._runner), so it can still be executing after the
# turn left run_conversation and exit_turn ran the deferred engine shutdown.

_LIFECYCLE_HAMMER = textwrap.dedent(
    """
    import sys, threading, time
    from plugins.context_engine.lcm.lifecycle_state import LifecycleStateStore
    try:
        from plugins.context_engine.lcm.sqlite_util import StoreClosed
    except ImportError:  # pre-fix tree: no typed error, so any exception counts as bad
        class StoreClosed(Exception):
            pass

    db = sys.argv[1]
    bad = []

    def binder(store, stop, i):
        n = 0
        while not stop.is_set():
            try:
                store.bind_session(f"s{i}-{n}")
                store.get_by_session(f"s{i}-{n}")
                store.get_by_conversation(f"s{i}-{n}")
            except StoreClosed:
                return
            except Exception as exc:
                bad.append(repr(exc))
                return
            n += 1

    for _round in range(int(sys.argv[2])):
        store = LifecycleStateStore(db)
        stop = threading.Event()
        threads = [threading.Thread(target=binder, args=(store, stop, i)) for i in range(8)]
        for t in threads:
            t.start()
        time.sleep(0.02)
        closer = threading.Thread(target=lambda: [store.close() for _ in range(50)])
        closer.start()
        closer.join()
        stop.set()
        for t in threads:
            t.join(10)
        if bad:
            break
    print("\\n".join(bad[:5]))
    sys.exit(1 if bad else 0)
    """
)


def _run_hammer(script: str, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", script, *args],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )


@pytest.mark.slow_test(180, reason="40 rounds x 8 binder threads x 50 close() calls in a faulthandler subprocess; "
                                   "the race needs the volume (same shape as the MessageStore hammer above)")
def test_lifecycle_close_racing_binds_never_crashes_or_leaks_untyped_errors(tmp_path):
    proc = _run_hammer(_LIFECYCLE_HAMMER, str(tmp_path / "lcm.db"), "40")
    assert proc.returncode != -11, f"SIGSEGV: lifecycle close() freed the connection under a live execute\n{proc.stderr[-2000:]}"
    assert proc.returncode == 0, (
        f"rc={proc.returncode}; an exception other than StoreClosed reached a lifecycle caller:\n"
        f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    )


def test_closed_lifecycle_store_raises_typed_error(tmp_path):
    from plugins.context_engine.lcm.lifecycle_state import LifecycleStateStore
    from plugins.context_engine.lcm.sqlite_util import StoreClosed

    store = LifecycleStateStore(tmp_path / "lcm.db")
    store.bind_session("s1")
    raw = store.connection
    store.close()
    store.close()  # idempotent
    assert store.connection is None
    for call in (
        lambda: store.get_by_session("s1"),
        lambda: store.get_by_conversation("s1"),
        lambda: store.bind_session("s2"),
        lambda: store.row_count(),
        lambda: store.clear_debt("s1"),
        lambda: store.delete_safe_rows_for_sessions({"s1"}),
        lambda: raw.execute("SELECT 1"),  # a handle taken before close() never reaches the freed connection
    ):
        with pytest.raises(StoreClosed):
            call()


# Every long-lived LCM connection, reached the way diagnostics reach it (``store.connection`` /
# ``_conn``), raced against the owning store's close(). Exit 0 = only StoreClosed was raised.
_RAW_HANDLE_HAMMER = textwrap.dedent(
    """
    import sys, threading, time
    try:
        from plugins.context_engine.lcm.sqlite_util import StoreClosed
    except ImportError:  # pre-fix tree: no typed error, so any exception counts as bad
        class StoreClosed(Exception):
            pass

    kind, db, rounds = sys.argv[1], sys.argv[2], int(sys.argv[3])

    def make():
        if kind == "dag":
            from plugins.context_engine.lcm.dag import SummaryDAG
            return SummaryDAG(db)
        if kind == "rollup":
            from plugins.context_engine.lcm.rollup_store import RollupStore
            return RollupStore(db)
        if kind == "vector":
            from plugins.context_engine.lcm.vector_store import VectorStore
            return VectorStore(db)
        if kind == "query_views":
            from plugins.context_engine.lcm.query_view_store import QueryViewStore
            return QueryViewStore(db)
        if kind == "assertions":
            from plugins.context_engine.lcm.assertion_store import AssertionStore
            return AssertionStore(db)
        if kind == "lifecycle":
            from plugins.context_engine.lcm.lifecycle_state import LifecycleStateStore
            return LifecycleStateStore(db)
        if kind == "messages":
            from plugins.context_engine.lcm.config import LCMConfig
            from plugins.context_engine.lcm.store import MessageStore
            return MessageStore(db, ingest_protection_config=LCMConfig(database_path=db))
        raise SystemExit(f"unknown kind {kind}")

    # Dependent stores (query views, assertions) need the base schema the message store creates.
    from plugins.context_engine.lcm.config import LCMConfig as _Cfg
    from plugins.context_engine.lcm.store import MessageStore as _MS
    _MS(db, ingest_protection_config=_Cfg(database_path=db)).close()

    bad = []

    def reader(conn, stop):
        while not stop.is_set():
            try:
                for _row in conn.execute("SELECT name FROM sqlite_master"):
                    pass
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            except StoreClosed:
                return
            except Exception as exc:
                bad.append(repr(exc))
                return

    for _round in range(rounds):
        store = make()
        conn = store._conn
        stop = threading.Event()
        threads = [threading.Thread(target=reader, args=(conn, stop)) for _ in range(6)]
        for t in threads:
            t.start()
        time.sleep(0.01)
        store.close()
        stop.set()
        for t in threads:
            t.join(10)
        if bad:
            break
    print("\\n".join(bad[:5]))
    sys.exit(1 if bad else 0)
    """
)


@pytest.mark.slow_test(180, reason="7 store kinds x 15 rounds x 6 reader threads in a faulthandler subprocess")
@pytest.mark.parametrize("kind", ["dag", "rollup", "vector", "query_views", "assertions", "lifecycle", "messages"])
def test_raw_connection_handle_racing_store_close_is_memory_safe(tmp_path, kind):
    proc = _run_hammer(_RAW_HANDLE_HAMMER, kind, str(tmp_path / "lcm.db"), "15")
    assert proc.returncode != -11, f"SIGSEGV: {kind} close() freed the connection under a live execute\n{proc.stderr[-2000:]}"
    assert proc.returncode == 0, (
        f"rc={proc.returncode}; {kind}: a raw-handle use racing close() raised something other than StoreClosed:\n"
        f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    )


# -- source contract: no new unguarded long-lived connection in plugins/context_engine/lcm -------

# Connections opened, used and closed inside ONE function call on ONE thread: no other thread can
# reach them, so close() cannot race an execute. Anything stored on ``self`` or returned to a
# caller is shared and must be wrapped by ``sqlite_util.guard_connection``. Adding a row here is a
# review decision: say why the handle cannot escape its function.
_FUNCTION_LOCAL_CONNECTIONS = {
    ("aux_session.py", "_is_live_auxiliary_child_session"): "read-only state.db probe, closed in finally",
    ("aux_session.py", "_session_has_auxiliary_ancestor"): "read-only state.db probe, closed in finally",
    ("command.py", "_classify_schema_stamp"): "doctor read, closed before return",
    ("command.py", "_doctor_repair_schema_stamp_apply_text"): "doctor repair, closed before return",
    ("command.py", "_embedding_read_connection"): "context-managed read handle for one doctor command",
    ("db_bootstrap.py", "verify_temporal_rollup_schema"): "in-memory scratch DB",
    ("db_bootstrap.py", "_expected_assertion_schema_contract"): "in-memory scratch DB",
    ("db_bootstrap.py", "_run_background_integrity_scan"): "owned by the one background scan thread",
    ("db_bootstrap.py", "_dispatch_background_integrity_scan"): "claim row write, closed before return",
    ("db_bootstrap.py", "_run_background_fts_rebuild"): "owned by the one background rebuild thread",
    ("engine.py", "_session_has_foreground_branch_marker"): "read-only state.db probe, closed in finally",
    ("lifecycle_state.py", "get_fragmentation_stats"): "read-only state.db probe, closed in finally",
    ("maintenance.py", "backup_database"): "backup destination, closed before return",
    ("maintenance.py", "rotate_backup_database"): "backup destination, closed before return",
    ("sqlite_util.py", "connect_read"): "read-connection factory; each caller is checked here itself",
    ("retrieval_core.py", "hydrate_chunk_hits"): "deadline-bounded read, closed in finally",
    ("retrieval_core.py", "hydrate_semantic_nodes"): "deadline-bounded read, closed in finally",
    ("tools.py", "invoke"): "deadline-bounded lcm_grep readers, closed in finally",
    ("tools.py", "_lcm_recall_summary_source_hits"): "deadline-bounded read, closed in finally",
    ("trajectory_store.py", "backup_to"): "backup destination, closed before return",
    ("vector_store.py", "_load_binary_with_deadline"): "per-call reader copy, closed in finally",
}


def _unguarded_connects():
    import ast

    lcm = REPO_ROOT / "plugins" / "context_engine" / "lcm"
    found = []
    for path in sorted(lcm.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

        def is_guard(node):
            return isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "guard_connection"

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and (
                    (isinstance(node.func, ast.Attribute) and node.func.attr == "connect"
                     and getattr(node.func.value, "id", "") == "sqlite3")
                    or getattr(node.func, "id", "") == "connect_read")):  # sqlite_util read helper (t_188b19e8)
                continue
            if is_guard(parents.get(node)):
                continue
            func = node
            while func in parents and not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func = parents[func]
            fname = getattr(func, "name", "<module>")
            # ``conn = sqlite3.connect(...)`` ... ``return guard_connection(conn)`` in the same function
            target = parents.get(node)
            name = target.targets[0].id if isinstance(target, ast.Assign) and isinstance(target.targets[0], ast.Name) else None
            if name and any(
                isinstance(r, ast.Return) and is_guard(r.value)
                and any(isinstance(a, ast.Name) and a.id == name for a in r.value.args)
                for r in ast.walk(func)
            ):
                continue
            rel = str(path.relative_to(lcm))
            if (rel, fname) in _FUNCTION_LOCAL_CONNECTIONS:
                continue
            found.append(f"{rel}:{node.lineno} in {fname}()")
    return found


def test_every_shared_lcm_connection_is_guarded():
    """t_35b4c55d: a ``sqlite3.connect(`` in the LCM plugin is wrapped by ``guard_connection``
    (its close() and every execute share one lock; use after close raises StoreClosed), or it is
    a reviewed function-local handle in ``_FUNCTION_LOCAL_CONNECTIONS``."""
    assert _unguarded_connects() == []


def test_source_contract_catches_an_unguarded_connect(tmp_path, monkeypatch):
    """The contract is not vacuous: an unguarded shared connection in a new module is reported."""
    fake = tmp_path / "plugins" / "context_engine" / "lcm"
    fake.mkdir(parents=True)
    (fake / "new_store.py").write_text(
        "import sqlite3\nclass S:\n    def __init__(self, p):\n        self._conn = sqlite3.connect(p, check_same_thread=False)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)
    assert _unguarded_connects() == ["new_store.py:4 in __init__()"]
