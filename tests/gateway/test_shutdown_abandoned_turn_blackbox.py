"""Gateway shutdown must leave a blackbox ``turns`` row for turns it abandons.

When the restart drain and the post-interrupt settle window both expire, a
turn blocked in a provider stream or a tool never returns from
``run_conversation`` before the process exits. Neither ``finalize_turn`` nor
the ``run_agent`` backstop fires, so Blackbox kept ``turn_api_calls`` rows with
no parent ``turns`` row (blackbox-orphan-guard, 2026-09-27 13:00:57 /
19:01:53 / 23:57:50 and 2026-09-28 07:27:41: every default-profile orphan
lines up with a ``Gateway drain timed out`` restart; t_8a327955).
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from agent.usage_pricing import CanonicalUsage
from gateway.run import GatewayRunner
from plugins import blackbox
from plugins.blackbox import orphans, store


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(blackbox, "_config", lambda: {
        "enabled": True, "alerts_enabled": False, "record_subagents": True,
        "retention_days": 3650,
    })
    store._connect().close()

    from hermes_cli import lifecycle

    def invoke_hook(name, **kwargs):
        if name == "on_session_end":
            blackbox._on_session_end(**kwargs)
        return []

    monkeypatch.setattr(lifecycle, "invoke_hook", invoke_hook)
    return store._db_path()


def _agent(turn_id, *, children=()):
    return SimpleNamespace(
        session_id=turn_id.split(":")[0], _current_turn_id=turn_id,
        _current_task_id=turn_id.split(":")[1], model="claude-opus-5-5",
        platform="discord", provider="claude-bpr", context_compressor=None,
        _blackbox_turn_calls=(turn_id, []), _session_messages=None,
        _active_children=list(children), _active_children_lock=threading.Lock(),
    )


def _in_flight_call(turn_id, http_status=None):
    blackbox._on_session_start(session_id=turn_id.split(":")[0])
    blackbox.record_api_call(
        turn_id=turn_id, seq=0, ts=100.0, provider="claude-bpr", model="claude-opus-5-5",
        usage=CanonicalUsage(input_tokens=10, output_tokens=5),
        api_mode="anthropic_messages", sub_key=None, attribution="wire",
        http_status=http_status, relay_synthetic=False, route_id=None,
    )


def _orphan_ids(db):
    with sqlite3.connect(db) as conn:
        return sorted(t for t, _ts, _e in orphans.orphan_turns(conn, since=0, settle_before=1e12))


def _runner(monkeypatch, *, restart=True):
    async def _noop(self, *a, **k):
        return None

    monkeypatch.setattr(GatewayRunner, "_finalize_session_off_loop", _noop)
    monkeypatch.setattr(GatewayRunner, "_cleanup_agent_resources_off_loop", _noop)
    runner = object.__new__(GatewayRunner)
    runner._restart_requested = restart
    runner.adapters = {}
    return runner


def test_abandoned_turns_and_subagents_get_interrupted_turn_rows(ledger, monkeypatch):
    parent_tid = "20260921_002254_3f4af45e:0284cf38:5bd9a12b"
    child_tid = "20260921_002254_3f4af45e:sa-0-e6d1a4a8:2dce5ec3"
    _in_flight_call(parent_tid)
    _in_flight_call(child_tid, http_status=200)
    child = _agent(child_tid)
    parent = _agent(parent_tid, children=[child])
    assert _orphan_ids(ledger) == sorted([parent_tid, child_tid]), "precondition"

    asyncio.run(_runner(monkeypatch)._finalize_shutdown_agents({"k": parent}))

    assert _orphan_ids(ledger) == []
    with sqlite3.connect(ledger) as conn:
        rows = dict(conn.execute(
            "SELECT turn_id, interrupted FROM turns WHERE turn_id IN (?, ?)",
            (parent_tid, child_tid),
        ).fetchall())
    assert rows == {parent_tid: 1, child_tid: 1}
    # Provisional: the real per-turn marker stays unset so a late unwind wins.
    assert getattr(parent, "_session_end_emitted_turn_id", None) is None


def test_turn_that_unwinds_after_shutdown_emit_supersedes_it(ledger, monkeypatch):
    from agent.turn_finalizer import emit_unfinalized_session_end

    tid = "20260927_181411_30304c67:3f8c4d10:55b7f62b"
    _in_flight_call(tid, http_status=200)
    agent = _agent(tid)
    asyncio.run(_runner(monkeypatch)._finalize_shutdown_agents({"k": agent}))
    # A second shutdown pass does not re-emit the same provisional row.
    fired = []
    from hermes_cli import lifecycle

    real = lifecycle.invoke_hook
    monkeypatch.setattr(lifecycle, "invoke_hook",
                        lambda name, **kw: (fired.append(name), real(name, **kw))[1])
    asyncio.run(_runner(monkeypatch)._finalize_shutdown_agents({"k": agent}))
    assert fired == []

    # The turn then unwinds through an early return: the run_agent backstop
    # must still fire and replace the provisional interrupted row.
    assert emit_unfinalized_session_end(
        agent, tid, result={"completed": True, "final_response": "done"}
    ) is True
    with sqlite3.connect(ledger) as conn:
        assert conn.execute("SELECT interrupted FROM turns WHERE turn_id = ?",
                            (tid,)).fetchone() == (0,)


def test_abandoned_emit_does_not_settle_billed_responses_of_a_live_turn(ledger, monkeypatch):
    import agent.conversation_loop as loop

    settled = []
    monkeypatch.setattr(loop, "_settle_unaccepted_billed_responses",
                        lambda *a, **k: settled.append(a))
    tid = "20260924_121319_7e96dd9c:7ca4180a:45a30131"
    _in_flight_call(tid)
    asyncio.run(_runner(monkeypatch)._finalize_shutdown_agents({"k": _agent(tid)}))
    assert settled == []
    assert _orphan_ids(ledger) == []


def test_agent_started_after_drain_snapshot_is_covered(ledger, monkeypatch):
    tid = "20260927_135358_92971ac4:2ba7799a:dc65d5fb"
    _in_flight_call(tid, http_status=200)
    runner = _runner(monkeypatch)
    late = _agent(tid)
    runner._snapshot_running_agents = lambda: {"late": late}

    asyncio.run(runner._finalize_shutdown_agents({}))

    assert _orphan_ids(ledger) == []


def test_turn_that_already_emitted_is_not_re_emitted(ledger, monkeypatch):
    fired = []
    from hermes_cli import lifecycle

    monkeypatch.setattr(
        lifecycle, "invoke_hook", lambda name, **kw: fired.append(kw["turn_id"]) or []
    )
    tid = "20260927_135358_92971ac4:2ba7799a:dc65d5fb"
    done = _agent(tid)
    done._session_end_emitted_turn_id = tid
    idle = _agent(tid)
    idle._current_turn_id = None

    asyncio.run(_runner(monkeypatch)._finalize_shutdown_agents({"a": done, "b": idle}))

    assert fired == []


def test_api_server_run_agents_are_covered(ledger, monkeypatch):
    from gateway.config import Platform

    tid = "20260927_181411_30304c67:3f8c4d10:55b7f62b"
    _in_flight_call(tid, http_status=200)
    runner = _runner(monkeypatch, restart=False)
    runner.adapters = {Platform.API_SERVER: SimpleNamespace(_active_run_agents={"r": _agent(tid)})}

    asyncio.run(runner._finalize_shutdown_agents({}))

    assert _orphan_ids(ledger) == []
