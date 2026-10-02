"""Gateway shutdown must leave a blackbox ``turns`` row for turns it abandons.

When the restart drain and the post-interrupt settle window both expire, a
turn blocked in a provider stream or a tool never returns from
``run_conversation`` before the process exits. Neither ``finalize_turn`` nor
the ``run_agent`` backstop fires, so Blackbox kept ``turn_api_calls`` rows with
no parent ``turns`` row (blackbox-orphan-guard, 2026-09-27 13:00:57 /
19:01:53 / 23:57:50 and 2026-09-28 07:27:41: every default-profile orphan
lines up with a ``Gateway drain timed out`` restart; t_8a327955).

Shutdown fires ``on_turn_abandoned`` (observers only), never ``on_session_end``
(whose consumers tear things down). Blackbox writes a provisional interrupted
row from its own per-call ledger; a real row written later supersedes it.
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

HOOKS: list = []


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(blackbox, "_config", lambda: {
        "enabled": True, "alerts_enabled": False, "record_subagents": True,
        "retention_days": 3650,
    })
    store._connect().close()
    monkeypatch.setattr(blackbox, "_provisional_turns", {})
    HOOKS.clear()

    from hermes_cli import lifecycle

    def invoke_hook(name, **kwargs):
        HOOKS.append(name)
        if name == "on_session_end":
            blackbox._on_session_end(**kwargs)
        elif name == "on_turn_abandoned":
            blackbox._on_turn_abandoned(**kwargs)
        return []

    monkeypatch.setattr(lifecycle, "invoke_hook", invoke_hook)
    return store._db_path()


def _agent(turn_id, *, children=()):
    return SimpleNamespace(
        session_id=turn_id.split(":")[0], _current_turn_id=turn_id,
        _current_task_id=turn_id.split(":")[1], model="claude-opus-5-5",
        platform="discord", provider="claude-bpr", context_compressor=None,
        _blackbox_turn_calls=(turn_id, []), _session_messages=None,
        _chat_id="chan-1",
        _active_children=list(children), _active_children_lock=threading.Lock(),
    )


def _in_flight_call(turn_id, http_status=None, *, seq=0, input_tokens=10):
    blackbox._on_session_start(session_id=turn_id.split(":")[0])
    blackbox.record_api_call(
        turn_id=turn_id, seq=seq, ts=100.0 + seq, provider="claude-bpr",
        model="claude-opus-5-5",
        usage=CanonicalUsage(input_tokens=input_tokens, output_tokens=5),
        api_mode="anthropic_messages", sub_key=None, attribution="wire",
        http_status=http_status, relay_synthetic=False, route_id=None,
    )


def _orphan_ids(db):
    with sqlite3.connect(db) as conn:
        return sorted(t for t, _ts, _e in orphans.orphan_turns(conn, since=0, settle_before=1e12))


def _row(db, turn_id, cols):
    with sqlite3.connect(db) as conn:
        return conn.execute(f"SELECT {cols} FROM turns WHERE turn_id = ?", (turn_id,)).fetchone()


def _runner(monkeypatch, *, restart=True):
    async def _noop(self, *a, **k):
        return None

    monkeypatch.setattr(GatewayRunner, "_finalize_session_off_loop", _noop)
    monkeypatch.setattr(GatewayRunner, "_cleanup_agent_resources_off_loop", _noop)
    runner = object.__new__(GatewayRunner)
    runner._restart_requested = restart
    runner.adapters = {}
    return runner


def _shutdown(runner, active):
    asyncio.run(runner._finalize_shutdown_agents(active))


def test_abandoned_turns_and_subagents_get_interrupted_turn_rows(ledger, monkeypatch):
    parent_tid = "20260921_002254_3f4af45e:0284cf38:5bd9a12b"
    child_tid = "20260921_002254_3f4af45e:sa-0-e6d1a4a8:2dce5ec3"
    _in_flight_call(parent_tid)
    _in_flight_call(child_tid, http_status=200)
    child = _agent(child_tid)
    parent = _agent(parent_tid, children=[child])
    assert _orphan_ids(ledger) == sorted([parent_tid, child_tid]), "precondition"

    _shutdown(_runner(monkeypatch), {"k": parent})

    assert _orphan_ids(ledger) == []
    assert _row(ledger, parent_tid, "interrupted")[0] == 1
    assert _row(ledger, child_tid, "interrupted")[0] == 1
    # Provisional: the real per-turn marker stays unset so a late unwind wins.
    assert getattr(parent, "_session_end_emitted_turn_id", None) is None


def test_shutdown_never_fires_on_session_end_for_a_live_turn(ledger, monkeypatch):
    """on_session_end consumers tear down (disk-cleanup, meet hangup)."""
    tid = "20260927_181411_30304c67:3f8c4d10:55b7f62b"
    _in_flight_call(tid, http_status=200)
    _shutdown(_runner(monkeypatch), {"k": _agent(tid)})
    assert HOOKS == ["on_turn_abandoned"]


def test_row_usage_comes_from_the_per_call_ledger(ledger, monkeypatch):
    """Billed calls the loop never accepted are in turn_api_calls; count them."""
    tid = "20260928_113709_739608:4fad7ed1:abea944d"
    _in_flight_call(tid, http_status=200, seq=0, input_tokens=10)
    _in_flight_call(tid, http_status=200, seq=1, input_tokens=32)
    agent = _agent(tid)
    agent._billed_unaccounted = parked = [{"turn_id": tid}]

    _shutdown(_runner(monkeypatch), {"k": agent})

    assert _row(ledger, tid, "api_calls, input_tokens, output_tokens") == (2, 42, 10)
    assert parked == [{"turn_id": tid}], "the live turn still owns the parked list"


def test_subagent_attribution_survives_without_accumulated_calls(ledger, monkeypatch):
    tid = "20260926_015422_37a3b5:sa-1-adeeffc8:856809be"
    _in_flight_call(tid, http_status=200)
    child = _agent(tid)
    child._blackbox_is_subagent = True
    child._blackbox_parent_turn_id = "parent:turn:1"
    child._blackbox_depth = 1

    _shutdown(_runner(monkeypatch), {"k": child})

    assert _row(ledger, tid, "is_subagent, parent_turn_id, depth, api_calls") == (
        1, "parent:turn:1", 1, 1)


def test_provisional_row_keeps_the_channel_latest_turn_pointer(ledger, monkeypatch):
    new_tid = "20260927_183134_b6f8858a:new:00000001"
    blackbox._on_session_start(session_id="20260927_183134_b6f8858a")
    blackbox._on_session_end(session_id="20260927_183134_b6f8858a", turn_id=new_tid,
                             model="m", platform="discord", provider="p",
                             chat_id="chan-1", user_message="u", final_response="f",
                             turn_usage=None)
    old_tid = "20260927_183134_b6f8858a:old:00000000"
    _in_flight_call(old_tid, http_status=200)

    _shutdown(_runner(monkeypatch), {"k": _agent(old_tid)})

    assert _orphan_ids(ledger) == []
    with sqlite3.connect(ledger) as conn:
        assert conn.execute(
            "SELECT turn_id FROM last_turn WHERE platform = ? AND chat_id = ?",
            ("discord", "chan-1")).fetchone() == (new_tid,)


def test_provisional_row_never_replaces_an_existing_real_row(ledger, monkeypatch):
    sid = "20260927_164646_25b828"
    tid = sid + ":4560194f:30f202b5"
    blackbox._on_session_start(session_id=sid)
    blackbox._on_session_end(session_id=sid, turn_id=tid, interrupted=False,
                             model="m", platform="discord", provider="p",
                             user_message="u", final_response="done", turn_usage=None)
    blackbox._on_turn_abandoned(session_id=sid, turn_id=tid, reason="gateway_restart",
                                model="m", platform="discord", provider="p")
    assert _row(ledger, tid, "interrupted, final_text") == (0, "done")


def test_turn_that_already_emitted_is_not_re_emitted(ledger, monkeypatch):
    tid = "20260927_135358_92971ac4:2ba7799a:dc65d5fb"
    done = _agent(tid)
    done._session_end_emitted_turn_id = tid
    idle = _agent(tid)
    idle._current_turn_id = None

    _shutdown(_runner(monkeypatch), {"a": done, "b": idle})

    assert HOOKS == []


def test_turn_that_unwinds_after_shutdown_emit_supersedes_it(ledger, monkeypatch):
    from agent.turn_finalizer import emit_unfinalized_session_end

    tid = "20260927_181411_30304c67:3f8c4d10:55b7f62b"
    _in_flight_call(tid, http_status=200)
    agent = _agent(tid)
    _shutdown(_runner(monkeypatch), {"k": agent})
    # A second shutdown pass does not re-emit the same provisional row.
    HOOKS.clear()
    _shutdown(_runner(monkeypatch), {"k": agent})
    assert HOOKS == []

    # The turn then unwinds through an early return: the run_agent backstop
    # must still fire and replace the provisional interrupted row.
    assert emit_unfinalized_session_end(
        agent, tid, result={"completed": True, "final_response": "done"}
    ) is True
    assert _row(ledger, tid, "interrupted")[0] == 0


def test_provisional_blackbox_record_keeps_live_session_state(ledger, monkeypatch):
    sid = "20260927_164338_fe9027"
    tid = sid + ":91e688c1:3cf9fd87"
    _in_flight_call(tid, http_status=200)
    blackbox._session_state(sid)["tools"].append("terminal")
    blackbox._on_turn_abandoned(session_id=sid, turn_id=tid, reason="gateway_restart",
                                model="m", platform="discord", provider="p")
    assert blackbox._sessions[sid]["tools"] == ["terminal"]


def test_abandoned_emit_does_not_settle_billed_responses_of_a_live_turn(ledger, monkeypatch):
    import agent.conversation_loop as loop

    settled = []
    monkeypatch.setattr(loop, "_settle_unaccepted_billed_responses",
                        lambda *a, **k: settled.append(a))
    tid = "20260924_121319_7e96dd9c:7ca4180a:45a30131"
    _in_flight_call(tid)
    _shutdown(_runner(monkeypatch), {"k": _agent(tid)})
    assert settled == []
    assert _orphan_ids(ledger) == []


def test_agent_started_after_drain_snapshot_is_covered(ledger, monkeypatch):
    tid = "20260927_135358_92971ac4:2ba7799a:dc65d5fb"
    _in_flight_call(tid, http_status=200)
    runner = _runner(monkeypatch)
    late = _agent(tid)
    runner._snapshot_running_agents = lambda: {"late": late}

    _shutdown(runner, {})

    assert _orphan_ids(ledger) == []


@pytest.mark.parametrize("registry", ["_active_run_agents", "_shutdown_interruptible_agents"])
def test_api_server_agents_are_covered(ledger, monkeypatch, registry):
    from gateway.config import Platform

    tid = "20260928_094807_1b51c4:fb588fe4:b677bdac"
    _in_flight_call(tid, http_status=200)
    runner = _runner(monkeypatch, restart=False)
    agent = _agent(tid)
    regs = {"_active_run_agents": {}, "_shutdown_interruptible_agents": {}}
    regs[registry] = {id(agent): agent}
    runner.adapters = {Platform.API_SERVER: SimpleNamespace(**regs)}

    _shutdown(runner, {})

    assert _orphan_ids(ledger) == []


def test_in_flight_cron_agents_are_covered(ledger, monkeypatch):
    from cron import scheduler

    tid = "cron_20e2aab221f3_20260925_223621:b9b2a2fd:bd018edd"
    _in_flight_call(tid, http_status=200)
    agent = _agent(tid)
    monkeypatch.setitem(scheduler._live_cron_agents, id(agent), agent)

    _shutdown(_runner(monkeypatch), {})

    assert _orphan_ids(ledger) == []


def test_in_flight_background_review_of_an_idle_parent_is_covered(ledger, monkeypatch):
    """2026-09-29 14:04: review fork ``...:c8656124`` of the idle #apollo agent
    was mid-turn at SIGTERM; its parent was in no running map, so the fork's
    calls orphaned (t_ab2f510b)."""
    from agent import background_review as br

    tid = "20260927_135034_52aefa:1e881602-d57f-47b0-9ed1-5dc4e5ad213f:c8656124"
    _in_flight_call(tid, http_status=200)
    fork = _agent(tid)
    monkeypatch.setitem(br._live_review_agents, id(fork), fork)

    _shutdown(_runner(monkeypatch), {})

    assert _orphan_ids(ledger) == []
    assert _row(ledger, tid, "interrupted")[0] == 1
    assert HOOKS == ["on_turn_abandoned"]


def test_provisional_emit_waiting_on_a_real_finalize_stands_down(ledger, monkeypatch):
    """Real finalize holds the per-agent emit lock; the shutdown emit must
    block, then see the real marker and write nothing after the real row."""
    from agent import turn_finalizer as tf

    tid = "20260927_164338_fe9027:91e688c1:3cf9fd87"
    agent = _agent(tid)
    lock = tf._session_end_lock(agent)
    result = {}
    with lock:
        t = threading.Thread(target=lambda: result.setdefault(
            "r", tf.emit_abandoned_session_ends([agent], "gateway_restart")))
        t.start()
        t.join(0.3)
        assert t.is_alive(), "provisional emit must wait on the lock"
        # Real finalize, mid-flight: marker set before its hook returns.
        agent._session_end_emitted_turn_id = tid
    t.join(5)
    assert result["r"] == 0
    assert HOOKS == []
    assert getattr(agent, "_session_end_abandoned_turn_id", None) is None


def test_provisional_emit_skips_when_agent_moved_to_a_newer_turn(ledger, monkeypatch):
    from agent import turn_finalizer as tf

    old = "20260927_233124_398b4d:ad3f5e38:2af4faa5"
    agent = _agent(old)
    agent._current_turn_id = old + "-next"
    assert tf.emit_session_end(
        agent, turn_id=old, effective_task_id="t", completed=False, failed=False,
        interrupted=True, turn_exit_reason="gateway_restart",
        original_user_message=None, final_response="", provisional=True,
    ) is False
    assert HOOKS == []


def test_abandoned_turn_is_written_to_the_profile_it_ran_in(ledger, tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    prof = tmp_path / "profiles" / "p1"
    prof.mkdir(parents=True)
    tid = "20260927_122252_9388b5:4a38b91b:1cdf366b"
    tok = set_hermes_home_override(str(prof))
    try:
        store._connect().close()
        prof_db = store._db_path()
        _in_flight_call(tid, http_status=200)
    finally:
        reset_hermes_home_override(tok)
    assert _orphan_ids(prof_db) == [tid], "precondition"
    agent = _agent(tid)
    agent._turn_home = (tid, str(prof))

    _shutdown(_runner(monkeypatch), {"k": agent})

    assert _orphan_ids(prof_db) == []
    assert _row(ledger, tid, "1") is None


def _raw_call(db, turn_id, seq, *, inp, out, lane_family=None, parent_call_id=None):
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO turn_api_calls (turn_id, seq, ts, provider, model, input_tokens, "
            "output_tokens, cache_read, cache_write, reasoning, http_status, lane_family, "
            "parent_call_id) VALUES (?, ?, ?, 'claude-bpr', 'claude-opus-5-5', ?, ?, ?, ?, 0, 200, ?, ?)",
            (turn_id, seq, 100.0 + seq, inp, out, None if inp is None else 0,
             None if inp is None else 0, lane_family, parent_call_id),
        )


def test_ledger_rollup_is_main_lane_only_and_null_is_unknown(ledger, monkeypatch):
    tid = "20260925_184925_583448ba:20260925_184925_583448ba:5ce1d232"
    _raw_call(ledger, tid, 0, inp=10, out=1)
    _raw_call(ledger, tid, 1, inp=500, out=50, lane_family="aux")
    _raw_call(ledger, tid, 2, inp=700, out=70, parent_call_id=1)
    usage = store.ledger_turn_usage(tid)
    assert (usage["api_calls"], usage["input_tokens"], usage["usage_unknown"]) == (1, 10, False)

    _raw_call(ledger, tid, 3, inp=None, out=None)
    usage = store.ledger_turn_usage(tid)
    assert usage["api_calls"] == 2
    assert usage["usage_unknown"] is True and usage["input_tokens_unknown"] is True


def test_call_landing_after_the_provisional_row_refreshes_it(ledger, monkeypatch):
    tid = "20260926_015422_041c59:20260926_015422_041c59:6c20bf72"
    _in_flight_call(tid, http_status=200, seq=0, input_tokens=10)
    _shutdown(_runner(monkeypatch), {"k": _agent(tid)})
    assert _row(ledger, tid, "api_calls, input_tokens") == (1, 10)

    _in_flight_call(tid, http_status=200, seq=1, input_tokens=32)

    assert _row(ledger, tid, "api_calls, input_tokens, interrupted") == (2, 42, 1)


def test_late_call_after_the_real_row_does_not_resurrect_the_provisional_one(ledger, monkeypatch):
    sid = "20260926_015422_3238d7"
    tid = sid + ":sa-2-88858c9a:bde67363"
    _in_flight_call(tid, http_status=200)
    _shutdown(_runner(monkeypatch), {"k": _agent(tid)})
    blackbox._on_session_end(session_id=sid, turn_id=tid, interrupted=False,
                             model="m", platform="discord", provider="p",
                             user_message="u", final_response="done", turn_usage=None)
    assert tid not in blackbox._provisional_turns

    _in_flight_call(tid, http_status=200, seq=1)

    assert _row(ledger, tid, "interrupted, final_text") == (0, "done")


def test_loop_liveness_watchdog_exit_records_in_flight_turns(ledger, monkeypatch):
    """r31 G: Apollo 2026-10-01 23:38:35 -- the loop-liveness watchdog
    os._exit'd mid-turn (no drain), leaving ...:b8249399 with calls and no
    turns row. The watchdog pre-exit hook records it without the event loop."""
    tid = "20260927_135034_52aefa:1ad2ec88-e1ec-4391-affd-e0198584cd45:b8249399"
    _in_flight_call(tid, http_status=200)
    runner = _runner(monkeypatch)
    live = _agent(tid)
    runner._snapshot_running_agents = lambda: {"k": live}
    assert _orphan_ids(ledger) == [tid], "precondition"

    runner._record_abandoned_turns_before_watchdog_exit()

    assert _orphan_ids(ledger) == []
    assert _row(ledger, tid, "interrupted")[0] == 1
    assert HOOKS == ["on_turn_abandoned"]
