"""Post-turn hooks (the /goal judge) run AFTER the reply is sent, as the adapter's post-delivery
callback inside the session task (t_3acac80f). The session stays busy while the judge runs.

Real ``GatewayRunner._handle_message`` + real ``BasePlatformAdapter`` via the flush-window rig.
"""

import asyncio
import time

import pytest

import gateway.platforms.base as base
from gateway.turn_lease import TurnLeaseTimeoutError
from tests.gateway.test_turn_lease_flush_window import KEY, _msg, _Rig, _wait_for
from hermes_cli import goals


def _timed_sends(rig):
    stamps = []
    real_send = rig.adapter.send

    async def send(chat_id, content, reply_to=None, metadata=None):
        if "answer " in str(content):  # the turn's reply, not a notice
            stamps.append(time.monotonic())
        return await real_send(chat_id, content, reply_to=reply_to, metadata=metadata)

    rig.adapter.send = send
    return stamps


def _slow_hooks(rig, seconds, log):
    async def hooks(*, event=None, **_kw):
        log.append(("start", event.text, time.monotonic()))
        await asyncio.sleep(seconds)
        log.append(("end", event.text, time.monotonic()))

    rig.runner._run_post_turn_hooks = hooks


@pytest.mark.asyncio
async def test_reply_is_sent_before_the_goal_judge_starts(monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path)
    sends, log = _timed_sends(rig), []
    _slow_hooks(rig, 3.0, log)  # a judge round trip under load
    await rig.adapter.handle_message(_msg("first"))
    await _wait_for(lambda: len(log) == 2 and sends, "judge and reply", timeout=10)
    assert sends[0] < log[0][2], "reply waited on the /goal judge"


@pytest.mark.asyncio
async def test_message_arriving_mid_judge_is_queued_and_runs_after_it(monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path)
    log = []
    _slow_hooks(rig, 0.5, log)
    await rig.adapter.handle_message(_msg("first"))
    await _wait_for(lambda: log, "judge start")
    assert KEY in rig.adapter._active_sessions, "session guard released before the judge ran"
    await rig.adapter.handle_message(_msg("second"))
    assert [c.split("\n\n")[0] for c in rig.calls] == ["first"], "second turn started during the judge"
    await _wait_for(lambda: len(rig.calls) >= 2, "second turn", timeout=10)
    assert [entry[:2] for entry in log[:2]] == [("start", "first"), ("end", "first")]


@pytest.mark.asyncio
async def test_message_queued_during_the_turn_waits_for_the_judge(monkeypatch, tmp_path):
    """The in-band hand-off to a queued follow-up fires this turn's callback first."""
    rig = _Rig(monkeypatch, tmp_path)
    log = []
    _slow_hooks(rig, 0.5, log)
    gate = rig.hold_flush("first")
    await rig.adapter.handle_message(_msg("first"))
    await asyncio.wait_for(rig.flush_entered["first"].wait(), 5)
    await rig.adapter.handle_message(_msg("second"))
    gate.set()
    await _wait_for(lambda: len(rig.calls) >= 2, "second turn", timeout=10)
    assert [entry[:2] for entry in log[:2]] == [("start", "first"), ("end", "first")]


@pytest.mark.asyncio
async def test_stop_mid_judge_cancels_it(monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path)
    log = []
    _slow_hooks(rig, 5.0, log)
    await rig.adapter.handle_message(_msg("first"))
    await _wait_for(lambda: log, "judge start")
    await rig.adapter.cancel_session_processing(KEY)  # what /stop does to the session task
    await asyncio.sleep(0.1)
    assert [e[0] for e in log] == ["start"], "judge kept running after /stop"


@pytest.mark.asyncio
async def test_loop_tick_settles_inside_the_callback(monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path)
    sends, order = _timed_sends(rig), []
    del rig.runner._run_post_turn_hooks  # the real hooks, both of them

    async def judge(**_kw):
        order.append("judge")
        await asyncio.sleep(0.3)

    async def loop_completion(**_kw):
        order.append("loop-after-send" if sends else "loop")

    rig.runner._post_turn_goal_continuation = judge
    rig.runner._post_turn_loop_completion = loop_completion
    await rig.adapter.handle_message(_msg("first"))
    await _wait_for(lambda: "judge" in order, "judge")
    assert order == ["loop", "judge"]


@pytest.mark.asyncio
async def test_loop_tick_fired_during_the_send_is_not_completed_by_this_turn(monkeypatch, tmp_path):
    """Prism bdc3c452d1ec: /loop completion runs before the send, so a tick the wakeup watcher fires
    during the send (session slot already released) is not completed by the previous user turn."""
    rig = _Rig(monkeypatch, tmp_path)
    tick, completed = {"awaiting": False}, []
    real_send = rig.adapter.send

    async def send(chat_id, content, reply_to=None, metadata=None):
        if "answer " in str(content):
            tick["awaiting"] = True  # the watcher injects a due /loop wakeup now
        return await real_send(chat_id, content, reply_to=reply_to, metadata=metadata)

    async def loop_completion(**_kw):
        if tick["awaiting"]:
            completed.append("tick")

    async def judge(**_kw):
        completed.append("judge")

    rig.adapter.send = send
    del rig.runner._run_post_turn_hooks
    rig.runner._post_turn_goal_continuation = judge
    rig.runner._post_turn_loop_completion = loop_completion
    await rig.adapter.handle_message(_msg("first"))
    await _wait_for(lambda: "judge" in completed, "judge")
    assert completed == ["judge"], "the user turn completed a tick fired during its send"


@pytest.mark.asyncio
async def test_stop_during_the_final_send_runs_no_hooks(monkeypatch, tmp_path):
    """Prism a783c95639e5: the adapter's finally fires the callback after /stop; it must not run."""
    rig = _Rig(monkeypatch, tmp_path)
    log, held = [], asyncio.Event()
    _slow_hooks(rig, 0.01, log)
    real_send = rig.adapter.send

    async def send(chat_id, content, reply_to=None, metadata=None):
        if "answer " in str(content):
            held.set()
            await asyncio.Event().wait()  # the final send hangs until /stop cancels it
        return await real_send(chat_id, content, reply_to=reply_to, metadata=metadata)

    async def no_reply(*_a, **_kw):
        return None

    rig.adapter.send = send
    rig.adapter._dispatch_inline_reply = no_reply
    await rig.adapter.handle_message(_msg("first"))
    await asyncio.wait_for(held.wait(), 5)
    await rig.adapter._dispatch_active_session_command(_msg("/stop"), KEY, "stop")
    await asyncio.sleep(0.2)
    assert log == [], "post-turn hooks ran after /stop"


@pytest.mark.asyncio
async def test_hooks_outlive_the_default_callback_bound(monkeypatch, tmp_path):
    monkeypatch.setattr(base, "_POST_DELIVERY_CALLBACK_TIMEOUT_SECONDS", 0.1)
    rig = _Rig(monkeypatch, tmp_path)
    log = []
    _slow_hooks(rig, 0.4, log)
    await rig.adapter.handle_message(_msg("first"))
    await _wait_for(lambda: len(log) == 2, "judge end", timeout=5)


@pytest.mark.asyncio
async def test_lease_rejection_returns_before_any_hook(monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path)
    calls = []

    async def hooks(**_kw):
        calls.append(1)

    async def reject(*_a, **_kw):
        raise TurnLeaseTimeoutError("sid", owner_key=KEY, generation=1, wait_seconds=0.1)

    rig.runner._run_post_turn_hooks = hooks
    rig.runner._handle_message_with_agent = reject
    rig.runner._hm_lease_rejection_reply = lambda *_a: "resend"
    assert await rig.runner._handle_message(_msg("first")) == "resend"
    await asyncio.sleep(0.05)
    assert calls == []
    assert not rig.adapter._post_delivery_callbacks


@pytest.fixture
def goal_db(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    goals._DB_CACHE.clear()
    yield
    goals._DB_CACHE.clear()


def test_stale_evaluator_save_is_dropped_and_never_continues(goal_db, monkeypatch):
    """Backstop: an evaluator loaded before a pause cannot resurrect the goal."""
    goals.GoalManager("cas").set("finish")
    stale = goals.GoalManager("cas")
    goals.GoalManager("cas").pause()
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("continue", "more", False, None, False))
    decision = stale.evaluate_after_turn("did a thing", made_tool_calls=True)
    assert decision["should_continue"] is False
    assert goals.load_goal("cas").status == "paused"


def test_stale_evaluation_aborts_before_touching_the_replacement_goal(goal_db, monkeypatch):
    """The first lost save ends the evaluation; the old goal's verdict never lands on the new goal."""
    old = goals.GoalManager("abort")
    old.set("old")
    old.state.idle_wakes = 1
    old._save()
    stale = goals.GoalManager("abort")

    def judge(*_a, **_k):  # the user replaces the goal while the judge runs
        goals.GoalManager("abort").set("new")
        return ("blocked", "impossible", False, None, False)

    monkeypatch.setattr(goals, "judge_goal", judge)
    decision = stale.evaluate_after_turn("old reply", real_event=True)
    stored = goals.load_goal("abort")
    assert decision["should_continue"] is False
    assert (stored.goal, stored.status, stored.last_verdict) == ("new", "active", None)


def test_user_verb_after_a_concurrent_save_is_not_lost(goal_db):
    """Prism 14f3a188fdff: only the evaluator may drop a write; an acknowledged gate persists."""
    goals.GoalManager("s").set("ship")
    a, b = goals.GoalManager("s"), goals.GoalManager("s")
    b.set_checkin_minutes(5)
    a.add_gate("false")
    assert [g.command for g in goals.load_goal("s").gates] == ["false"]


@pytest.mark.asyncio
async def test_stop_mid_judge_keeps_a_queued_follow_up(monkeypatch, tmp_path):
    """Prism e5e53ccb615c: a follow-up queued before the judge survives /stop and runs next."""
    rig = _Rig(monkeypatch, tmp_path)
    log, completes = [], []
    _slow_hooks(rig, 5.0, log)

    async def on_complete(event, outcome):
        completes.append((event.text.split("\n\n")[0], outcome))

    rig.adapter.on_processing_complete = on_complete
    gate = rig.hold_flush("first")
    await rig.adapter.handle_message(_msg("first"))
    await asyncio.wait_for(rig.flush_entered["first"].wait(), 5)
    await rig.adapter.handle_message(_msg("second"))  # queued during the turn
    gate.set()
    await _wait_for(lambda: log, "judge start")
    assert [c.split("\n\n")[0] for c in rig.calls] == ["first"]

    async def no_reply(*_a, **_kw):
        return None

    rig.adapter._dispatch_inline_reply = no_reply
    await rig.adapter._dispatch_active_session_command(_msg("/stop"), KEY, "stop")
    await _wait_for(lambda: len(rig.calls) >= 2, "queued follow-up after /stop", timeout=10)
    assert rig.calls[1].split("\n\n")[0] == "second"
    assert [c for c in completes if c[0] == "first"] == [("first", base.ProcessingOutcome.CANCELLED)]


@pytest.mark.asyncio
async def test_deferred_hooks_run_in_the_routed_profile_scope(monkeypatch, tmp_path):
    """Prism 2363a8f52a0d: the hooks read profile B's state.db, not the launch profile's."""
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override

    home_a, home_b = tmp_path / "a", tmp_path / "b"
    home_a.mkdir(), home_b.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home_a))
    goals._DB_CACHE.clear()
    rig = _Rig(monkeypatch, tmp_path)

    def set_goal_in_b():
        token = set_hermes_home_override(str(home_b))
        try:
            goals.GoalManager("routed").set("goal in B")
        finally:
            reset_hermes_home_override(token)

    await asyncio.to_thread(set_goal_in_b)
    seen = []

    async def hooks(**_kw):
        state = await asyncio.to_thread(goals.load_goal, "routed")
        seen.append((str(get_hermes_home()), state.goal if state else None))

    rig.runner._run_post_turn_hooks = hooks
    real_handler = rig.runner._handle_message

    async def routed_handler(event):  # what run_adapters does: enter B's scope for the turn only
        token = set_hermes_home_override(str(home_b))
        try:
            return await real_handler(event)
        finally:
            reset_hermes_home_override(token)

    rig.adapter.set_message_handler(routed_handler)
    try:
        await rig.adapter.handle_message(_msg("first"))
        await _wait_for(lambda: seen, "deferred hooks", timeout=10)
    finally:
        goals._DB_CACHE.clear()
    assert seen == [(str(home_b), "goal in B")]


@pytest.mark.asyncio
async def test_slow_quality_gate_is_not_cut_by_a_callback_bound(monkeypatch, tmp_path):
    """Prism 9355f9ec7aa1: parity with the old inline await; a 125 s gate is not cancelled."""
    rig = _Rig(monkeypatch, tmp_path)
    loop = asyncio.get_running_loop()
    real_time, offset = loop.time, [0.0]
    monkeypatch.setattr(loop, "time", lambda: real_time() + offset[0])
    started, release, outcome = asyncio.Event(), asyncio.Event(), []

    async def gate_hooks(**_kw):
        started.set()
        try:
            await release.wait()
            outcome.append("done")
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise

    rig.runner._run_post_turn_hooks = gate_hooks
    await rig.adapter.handle_message(_msg("first"))
    await asyncio.wait_for(started.wait(), 5)
    offset[0] = 125.0  # the clock passes 125 s while the gate runs
    for _ in range(20):
        await asyncio.sleep(0)
    release.set()
    for _ in range(200):
        if outcome:
            break
        await asyncio.sleep(0)
    assert outcome == ["done"]


def test_cached_manager_evaluates_after_another_instance_wrote(goal_db, monkeypatch):
    """Prism f64b5ac573b7: the CAS baseline is the row at evaluation start, not at construction."""
    cached = goals.GoalManager("cli")
    cached.set("finish")
    goals.GoalManager("cli").set_checkin_minutes(5)  # another manager writes the row (CLI loop tick)
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("continue", "more", False, None, False))
    decision = cached.evaluate_after_turn("did a thing", made_tool_calls=True)
    assert decision["verdict"] != "stale" and decision["should_continue"] is True


@pytest.mark.asyncio
async def test_goal_set_during_the_send_is_not_judged_on_the_old_reply(goal_db, monkeypatch):
    """Prism 49118239454b: no goal at turn end, /goal B during the send -> B untouched."""
    from tests.gateway.test_goal_continuation_turn_effect import _runner

    runner, adapter, entry, src = _runner()
    pin = (goals._load_goal_raw(entry.session_id)[1],)  # captured at turn end: no goal
    goals.GoalManager(entry.session_id, default_max_turns=1).set("B")
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("continue", "more", False, None, False))
    await runner._post_turn_goal_continuation(
        session_entry=entry, source=src, final_response="old reply", goal_pin=pin,
    )
    stored = goals.load_goal(entry.session_id)
    assert (stored.goal, stored.status, stored.turns_used) == ("B", "active", 0)
    assert not adapter._pending_messages


@pytest.mark.asyncio
async def test_pause_during_the_status_send_drops_the_continuation(goal_db, monkeypatch):
    """Prism a3be91a7b685: /goal pause completing during the status notice wins."""
    from tests.gateway.test_goal_continuation_turn_effect import _runner

    runner, adapter, entry, src = _runner()
    goals.GoalManager(entry.session_id).set("finish")
    real_send = adapter.send

    async def send(chat_id, content, reply_to=None, metadata=None):
        await asyncio.to_thread(lambda: goals.GoalManager(entry.session_id).pause())  # /goal pause lands
        return await real_send(chat_id, content, reply_to=reply_to, metadata=metadata)

    adapter.send = send
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("continue", "more", False, None, False))
    await runner._post_turn_goal_continuation(session_entry=entry, source=src, final_response="ok")
    assert adapter.sends, "no status notice was sent"
    assert goals.load_goal(entry.session_id).status == "paused"
    assert not adapter._pending_messages


@pytest.mark.asyncio
async def test_replacement_goal_at_the_same_revision_gets_no_continuation(goal_db, monkeypatch):
    """Prism f648365853c9: /goal clear + /goal set B + gates during the status send; B reaches A's
    revision. Revision equality is not identity: A's continuation is not enqueued onto B."""
    from tests.gateway.test_goal_continuation_turn_effect import _runner

    runner, adapter, entry, src = _runner()
    sid = entry.session_id
    goals.GoalManager(sid).set("A")
    real_send = adapter.send

    def replace():
        target = goals.load_goal(sid).revision  # what A's evaluation wrote
        goals.GoalManager(sid).clear()
        b = goals.GoalManager(sid)
        b.set("B")
        while b.state.revision < target:
            b.add_gate("true")

    async def send(chat_id, content, reply_to=None, metadata=None):
        await asyncio.to_thread(replace)
        return await real_send(chat_id, content, reply_to=reply_to, metadata=metadata)

    adapter.send = send
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("continue", "more", False, None, False))
    await runner._post_turn_goal_continuation(session_entry=entry, source=src, final_response="ok")
    assert adapter.sends, "no status notice was sent"
    assert goals.load_goal(sid).goal == "B"
    assert not adapter._pending_messages


def test_transient_refresh_failure_keeps_the_loaded_goal(goal_db, monkeypatch):
    """Prism 338d8a0072da: a 'database is locked' at evaluation start is not 'no goal'."""
    import sqlite3

    mgr = goals.GoalManager("locked")
    mgr.set("finish")
    db, fails = goals._get_session_db(), [1]
    real_get = db.get_meta_committed  # the evaluation refresh reads the WAL pool (#2377), not get_meta

    def flaky(key):
        if fails:
            fails.pop()
            raise sqlite3.OperationalError("database is locked")
        return real_get(key)

    monkeypatch.setattr(db, "get_meta_committed", flaky)
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("continue", "more", False, None, False))
    decision = mgr.evaluate_after_turn("did a thing", made_tool_calls=True)
    assert not fails, "the refresh read never ran"
    assert decision["should_continue"] is True
    assert goals.load_goal("locked").status == "active"


@pytest.mark.asyncio
async def test_unbounded_goal_hook_does_not_unbound_a_chained_callback(monkeypatch):
    """Prism fc4a550234d2: chaining the unbounded /goal hook made every callback in the chain unbounded,
    so a stalled callback registered beside it held the session forever. Each link keeps its own bound."""
    monkeypatch.setattr(base, "_POST_DELIVERY_CALLBACK_TIMEOUT_SECONDS", 0.2)
    ran = []

    async def stalled():
        await asyncio.Event().wait()

    async def goal_hook():
        ran.append("goal")

    goal_hook.post_delivery_timeout = None
    chained = base.BasePlatformAdapter._chain_callbacks(stalled, goal_hook)
    assert chained.post_delivery_timeout is None  # the chain itself stays unbounded for the judge
    await asyncio.wait_for(chained(), timeout=5)  # the stalled link times out alone; the goal hook runs
    assert ran == ["goal"]


@pytest.mark.asyncio
async def test_failed_turn_end_pin_skips_the_deferred_judge(goal_db, monkeypatch):
    """Prism 'Unpinned Evaluation' (run_goals.py:455): a transient read failure at turn end used to leave
    the judge unpinned, so a /goal set during the send was judged on the old reply. Skip instead; the
    idle check-in re-evaluates later."""
    from tests.gateway.test_goal_continuation_turn_effect import _runner

    runner, adapter, entry, src = _runner()
    goals.GoalManager(entry.session_id, default_max_turns=1).set("B")
    judged = []
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: judged.append(1) or ("done", "ok", False, None, False))
    await runner._post_turn_goal_continuation(
        session_entry=entry, source=src, final_response="old reply", goal_pin=runner_goal_pin_failed(),
    )
    stored = goals.load_goal(entry.session_id)
    assert (stored.goal, stored.status, stored.turns_used) == ("B", "active", 0)
    assert not judged


def runner_goal_pin_failed():
    from gateway.run_goals import GOAL_PIN_FAILED
    return GOAL_PIN_FAILED


def _continue_judge(monkeypatch, seen=None, during=None):
    def judge(goal, _reply, subgoals=None, **_k):
        if seen is not None:
            seen.append(list(subgoals or []))
        if during is not None and during:
            during.pop()()
        return ("continue", "more", False, None, False)

    monkeypatch.setattr(goals, "judge_goal", judge)


@pytest.mark.asyncio
async def test_subgoal_during_the_final_send_still_judges_and_continues(goal_db, monkeypatch):
    """Prism c768988ba6e0: /subgoal after the turn-end pin is a nonterminal edit of the same active goal.
    It used to skip the judge (``should_continue=False``) and, with checkin_minutes 0, stall the goal."""
    from tests.gateway.test_goal_continuation_turn_effect import _runner

    runner, adapter, entry, src = _runner({"checkin_minutes": 0})
    sid = entry.session_id
    goals.GoalManager(sid).set("A")
    pin = (goals._load_goal_raw(sid)[1],)  # turn end
    goals.GoalManager(sid).add_subgoal("include tests")  # lands during the final send
    seen = []
    _continue_judge(monkeypatch, seen)
    await runner._post_turn_goal_continuation(session_entry=entry, source=src, final_response="ok", goal_pin=pin)
    assert seen == [["include tests"]], "the edited goal was not judged"
    stored = goals.load_goal(sid)
    assert (stored.status, stored.turns_used, stored.subgoals) == ("active", 1, ["include tests"])
    assert adapter._pending_messages, "no continuation queued: the goal stalls while reporting active"


@pytest.mark.asyncio
async def test_subgoal_during_the_status_send_keeps_the_continuation(goal_db, monkeypatch):
    """Same class at the enqueue recheck: a nonterminal edit during the status notice keeps the loop going."""
    from tests.gateway.test_goal_continuation_turn_effect import _runner

    runner, adapter, entry, src = _runner({"checkin_minutes": 0})
    sid = entry.session_id
    goals.GoalManager(sid).set("A")
    real_send = adapter.send

    async def send(chat_id, content, reply_to=None, metadata=None):
        await asyncio.to_thread(lambda: goals.GoalManager(sid).add_subgoal("include tests"))
        return await real_send(chat_id, content, reply_to=reply_to, metadata=metadata)

    adapter.send = send
    _continue_judge(monkeypatch)
    await runner._post_turn_goal_continuation(session_entry=entry, source=src, final_response="ok")
    assert adapter.sends, "no status notice was sent"
    assert goals.load_goal(sid).subgoals == ["include tests"]
    assert adapter._pending_messages, "continuation dropped for a nonterminal edit"


def test_subgoal_during_the_judge_reevaluates_the_edited_goal(goal_db, monkeypatch):
    """Same class inside the CAS: an edit while the judge runs re-judges the edited row instead of stopping."""
    goals.GoalManager("rj").set("A")
    mgr, seen = goals.GoalManager("rj"), []
    _continue_judge(monkeypatch, seen, during=[lambda: goals.GoalManager("rj").add_subgoal("include tests")])
    decision = mgr.evaluate_after_turn("did a thing", made_tool_calls=True)
    assert seen == [[], ["include tests"]]
    assert decision["should_continue"] is True and decision["verdict"] != "stale"
    stored = goals.load_goal("rj")
    assert (stored.turns_used, stored.subgoals) == (1, ["include tests"])


@pytest.mark.asyncio
async def test_pause_and_resume_during_the_send_is_not_a_nonterminal_edit(goal_db, monkeypatch):
    """resume() queues its own continuation; the old reply's judge must not queue a second one."""
    from tests.gateway.test_goal_continuation_turn_effect import _runner

    runner, adapter, entry, src = _runner()
    sid = entry.session_id
    goals.GoalManager(sid).set("A")
    pin = (goals._load_goal_raw(sid)[1],)
    goals.GoalManager(sid).pause()
    goals.GoalManager(sid).resume()
    judged = []
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: judged.append(1) or ("continue", "more", False, None, False))
    await runner._post_turn_goal_continuation(session_entry=entry, source=src, final_response="ok", goal_pin=pin)
    assert not judged and not adapter._pending_messages
    assert goals.load_goal(sid).turns_used == 0


def test_pause_and_resume_during_the_judge_stops_the_stale_evaluation(goal_db, monkeypatch):
    goals.GoalManager("pr").set("A")
    mgr, seen = goals.GoalManager("pr"), []

    def flip():
        goals.GoalManager("pr").pause()
        goals.GoalManager("pr").resume()

    _continue_judge(monkeypatch, seen, during=[flip])
    decision = mgr.evaluate_after_turn("did a thing", made_tool_calls=True)
    assert seen == [[]] and decision["should_continue"] is False and decision["verdict"] == "stale"


def test_a_newer_evaluation_during_the_judge_stops_the_older_one(goal_db, monkeypatch):
    """Prism 74493a4898d5: a competing evaluation keeps goal/created_at/resumed_at, so it must not be
    retried like a /subgoal edit. Newer turn B saves ``continue`` while older turn A's judge runs; A's
    ``done`` loses the CAS and stays lost instead of retrying onto B's row."""
    goals.GoalManager("ce").set("A")
    older = goals.GoalManager("ce")
    calls = []

    def judge(goal, reply, subgoals=None, **_k):
        calls.append(reply)
        if reply == "A":  # older turn's judge: the newer turn B is evaluated and saved meanwhile
            goals.GoalManager("ce").evaluate_after_turn("B", made_tool_calls=True)
            return ("done", "finished", False, None, False)
        return ("continue", "unfinished", False, None, False)

    monkeypatch.setattr(goals, "judge_goal", judge)
    decision = older.evaluate_after_turn("A", made_tool_calls=True)
    assert calls == ["A", "B"], "the stale evaluation was retried"
    assert decision["should_continue"] is False and decision["verdict"] == "stale"
    stored = goals.load_goal("ce")
    assert (stored.status, stored.last_verdict, stored.turns_used) == ("active", "continue", 1)


@pytest.mark.asyncio
async def test_a_newer_evaluation_after_the_pin_skips_the_older_judge(goal_db, monkeypatch):
    """Same class at the turn-end pin: a row another evaluation advanced is not a nonterminal edit."""
    from tests.gateway.test_goal_continuation_turn_effect import _runner

    runner, adapter, entry, src = _runner({"checkin_minutes": 0})
    sid = entry.session_id
    goals.GoalManager(sid).set("A")
    pin = (goals._load_goal_raw(sid)[1],)
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("continue", "unfinished", False, None, False))
    goals.GoalManager(sid).evaluate_after_turn("B", made_tool_calls=True)  # newer turn, already judged
    judged = []
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: judged.append(1) or ("done", "x", False, None, False))
    await runner._post_turn_goal_continuation(session_entry=entry, source=src, final_response="A", goal_pin=pin)
    assert not judged and not adapter._pending_messages
    assert goals.load_goal(sid).status == "active"
