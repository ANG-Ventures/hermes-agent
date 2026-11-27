"""Gateway idle-wake poller: a timer-parked /goal in an idle session gets a turn started for it."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from hermes_cli import goals


class _Adapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="wire-1")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from pathlib import Path
    home = tmp_path / ".hermes"; home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    source = SessionSource(platform=Platform.DISCORD, chat_id="77", user_id="1", chat_type="channel")
    key = build_session_key(source)
    adapter = _Adapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.DISCORD)
    received = []

    async def handler(event):
        received.append(event)
        event._goal_execution_started = True
        return None

    adapter.set_message_handler(handler)
    entry = SimpleNamespace(session_key=key, session_id="goal-sess", origin=source, suspended=False)
    runner = object.__new__(GatewayRunner)
    runner.config = {"goals": {"checkin_minutes": 30, "max_idle_checkins": 3}}
    runner._queued_events = {}
    runner.session_store = SimpleNamespace(list_sessions=lambda: [entry])
    runner._restored_source = lambda e: e.origin
    runner._delivery_adapter_for = lambda s: adapter
    runner._run_in_executor_with_context = asyncio.to_thread

    async def _warm(_label):
        return None

    runner._warm_goals_session_db = _warm
    yield runner, adapter, received, key
    goals._DB_CACHE.clear()


async def _drain(adapter):
    for task in list(getattr(adapter, "_session_tasks", {}).values()):
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_timer_parked_goal_is_woken_when_the_deadline_passes(rig):
    runner, adapter, received, key = rig
    mgr = goals.GoalManager("goal-sess")
    mgr.set("ship the feature")
    mgr.wait_for_seconds(600, reason="CI")
    await runner._goal_idle_wake_scan()
    assert received == []                                   # still parked: nothing fired
    mgr.state.waiting_until = time.time() - 1
    mgr._save()
    await runner._goal_idle_wake_scan()
    await _drain(adapter)
    assert len(received) == 1
    assert received[0].internal
    assert received[0].text.startswith("[Continuing toward your standing goal]")
    assert goals.load_goal("goal-sess").waiting_until == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [False, True])
@pytest.mark.parametrize("boundary", ["claim", "recheck", "admission"])
async def test_cancelled_retry_wake_is_refunded(rig, monkeypatch, boundary, edit):
    runner, adapter, received, key = rig
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **kw: ("continue", "more", False, None, False))
    mgr = await asyncio.to_thread(goals.GoalManager, "goal-sess")
    await asyncio.to_thread(mgr.set, "ship it", max_turns=2)  # the arming failure spends one
    clock = mgr.state.created_at
    monkeypatch.setattr(goals.time, "time", lambda: clock)
    await asyncio.to_thread(mgr.evaluate_after_turn, "", turn_completed=False, max_incomplete_turns=1)
    clock = mgr.state.waiting_until
    before = mgr.idle_wake_snapshot()
    reached, release = asyncio.Event(), asyncio.Event()
    real_hop = runner._run_in_executor_with_context
    real_handle = adapter.handle_message

    async def hop(fn, *args):
        result = await real_hop(fn, *args)
        if (boundary == "claim" and fn.__name__ == "_wake") or (
                boundary == "recheck" and fn.__name__ == "_still_active"):
            reached.set()
            await release.wait()
        return result

    async def handle(event):
        reached.set()  # Discord's awaited channel lookup before the base admission check
        await release.wait()
        return await real_handle(event)

    runner._run_in_executor_with_context = hop
    if boundary == "admission":
        adapter.handle_message = handle
    entry = runner.session_store.list_sessions()[0]
    task = asyncio.create_task(runner._goal_idle_wake_one(entry, 0, 3))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        if edit:
            def add_criterion():
                goals.GoalManager("goal-sess").add_subgoal("keep this criterion")
            await asyncio.to_thread(add_criterion)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    # Drain a shielded off-loop claim/refund, if cancellation happened during the write.
    for _ in range(100):
        restored = await asyncio.to_thread(goals.GoalManager, "goal-sess")
        if restored.state.incomplete_retry_wait:
            break
        await asyncio.sleep(0.01)
    assert restored.state.incomplete_retry_wait
    assert restored.state.waiting_until == before["waiting_until"]
    assert received == []
    runner._run_in_executor_with_context = real_hop
    adapter.handle_message = real_handle
    await runner._goal_idle_wake_one(entry, 0, 3)
    await _drain(adapter)
    assert len(received) == 1
    assert not (await asyncio.to_thread(goals.load_goal, "goal-sess")).incomplete_retry_wait


@pytest.mark.asyncio
async def test_retry_wake_rejected_before_admission_is_refunded(rig, monkeypatch):
    runner, adapter, received, key = rig
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **kw: ("continue", "more", False, None, False))
    mgr = await asyncio.to_thread(goals.GoalManager, "goal-sess")
    await asyncio.to_thread(mgr.set, "ship it", max_turns=2)  # the arming failure spends one
    clock = mgr.state.created_at
    monkeypatch.setattr(goals.time, "time", lambda: clock)
    await asyncio.to_thread(mgr.evaluate_after_turn, "", turn_completed=False, max_incomplete_turns=1)
    clock = mgr.state.waiting_until
    before = mgr.idle_wake_snapshot()
    entry = runner.session_store.list_sessions()[0]
    entry.session_key = key + ":stale-route"
    events = []
    real_handle = adapter.handle_message

    async def handle(event):
        events.append(event)
        await real_handle(event)

    adapter.handle_message = handle
    await runner._goal_idle_wake_one(entry, 0, 3)
    assert len(events) == 1
    assert events[0]._gateway_accepted is False
    assert events[0]._admission_check is not None  # real adapter returned before admission
    assert received == []
    restored = await asyncio.to_thread(goals.GoalManager, "goal-sess")
    for field, value in before.items():
        if field != "_row":  # refund persists a new revision, not the old serialized row
            assert restored.idle_wake_snapshot()[field] == value
    assert restored.state.incomplete_retry_wait

    entry.session_key = key  # routing repaired; checkins remain disabled
    await runner._goal_idle_wake_one(entry, 0, 3)
    await _drain(adapter)
    assert len(received) == 1
    assert events[-1]._gateway_accepted is True
    assert not (await asyncio.to_thread(goals.load_goal, "goal-sess")).incomplete_retry_wait
    await runner._goal_idle_wake_one(entry, 0, 3)
    await _drain(adapter)
    assert len(received) == 1  # accepted retries are not refunded


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [False, True])
@pytest.mark.parametrize("outcome", ["cap", "cancel", "execute", "route"])
async def test_retry_wake_tracks_execution_not_adapter_acceptance(rig, monkeypatch, outcome, edit):
    from unittest.mock import AsyncMock, Mock

    runner, adapter, received, key = rig
    judge = Mock(return_value=("continue", "more", False, None, False))
    monkeypatch.setattr(goals, "judge_goal", judge)
    mgr = await asyncio.to_thread(goals.GoalManager, "goal-sess")
    await asyncio.to_thread(mgr.set, "ship it", max_turns=2)  # the arming failure spends one
    clock = mgr.state.created_at
    monkeypatch.setattr(goals.time, "time", lambda: clock)
    await asyncio.to_thread(mgr.evaluate_after_turn, "", turn_completed=False, max_incomplete_turns=1)
    clock = mgr.state.waiting_until
    before = mgr.idle_wake_snapshot()
    entry = runner.session_store.list_sessions()[0]
    runner.config = {"max_concurrent_sessions": 1}
    runner.session_store.lookup_by_session_key = lambda key: None
    runner._hm_admit_event = AsyncMock(side_effect=lambda e: (e, e.source, True))
    runner._hm_estop_gate = lambda *a: None
    runner._hm_pending_reply_intercepts = AsyncMock(return_value=None)
    runner._hm_evict_idle_stale_agent = lambda *a: None
    runner._hm_queue_during_lease_flush = AsyncMock(return_value=None)
    runner._hm_dispatch_idle_commands = AsyncMock(return_value=(False, None))
    # Another session occupies the sole slot; exercise the real inbound cap rejection.
    runner._running_agents["other-session"] = object()
    runner._running_agent_count = lambda: len(runner._running_agents)
    reached, release = asyncio.Event(), asyncio.Event()

    async def handler(event):
        received.append(event)
        if outcome == "cap":
            reply = await runner._handle_message(event)
            assert reply == runner._active_session_limit_message(key)
            assert reply is not None
            reached.set()
            return reply
        reached.set()
        await release.wait()
        if edit:
            await asyncio.to_thread(lambda: goals.GoalManager("goal-sess").add_subgoal("keep criterion"))
        runner.hooks = SimpleNamespace(emit=AsyncMock())
        runner._hmwa_resolve_session = AsyncMock(return_value=(event.source, entry, key))
        if outcome == "route":
            runner._persisted_session_route_identity = lambda k: SimpleNamespace(state="unavailable")
            runner._async_session_store = SimpleNamespace(
                _store=runner.session_store, get_or_create_session=AsyncMock(return_value=entry))
            runner._defer_goal_status_notice_after_delivery = AsyncMock()
            reply = await runner._handle_message_with_agent_admitted(event, event.source, key, 1)
            assert "No provider request" in reply
            await runner._run_post_turn_hooks(agent_result=reply, source=event.source,
                                             is_internal=True, event=event, parts=("goal continuation",))
            return reply
        runner._hmwa_check_persisted_route = AsyncMock(return_value=None)
        runner._hmwa_prepare_turn = AsyncMock(return_value=(
            runner._PreparedTurn([], "", "retry", "retry", None, None), {}))
        runner._pinned_channel_inputs = lambda *a, **kw: ("", event.source)
        runner._clear_session_env = lambda tokens: None

        async def model(**kwargs):
            assert event._goal_execution_started is True
            raise asyncio.CancelledError  # execution began; settlement must not refund it

        runner._run_agent = model
        return await runner._handle_message_with_agent_admitted(event, event.source, key, 1)

    adapter.set_message_handler(handler)
    await runner._goal_idle_wake_one(entry, 0, 3)
    processing = adapter._session_tasks[key]
    if outcome != "cap":
        await asyncio.wait_for(reached.wait(), 5)
        # An accepted turn that has not settled must not be refunded prematurely.
        assert not (await asyncio.to_thread(goals.load_goal, "goal-sess")).incomplete_retry_wait
        if outcome == "cancel":
            if edit:
                await asyncio.to_thread(lambda: goals.GoalManager("goal-sess").add_subgoal("keep criterion"))
            processing.cancel()
        else:
            release.set()
    await asyncio.gather(processing, return_exceptions=True)
    assert reached.is_set()  # cap path reached the real rejection, not an earlier fixture error
    # Settlement may launch the persisted compare-and-refund as a tracked adapter task.
    while adapter._background_tasks:
        await asyncio.gather(*list(adapter._background_tasks), return_exceptions=True)
    assert received[0]._gateway_accepted is True
    restored = await asyncio.to_thread(goals.GoalManager, "goal-sess")
    judge.assert_not_called()
    # Only the executed arming failure is charged; an unexecuted wake would exhaust 2/2 and pause.
    assert restored.state.status == "active" and restored.state.turns_used == 1
    if edit and outcome != "cap":
        assert restored.state.subgoals == ["keep criterion"]
    assert restored.state.incomplete_retry_wait is (outcome != "execute")
    if outcome != "execute":
        assert restored.state.waiting_until == before["waiting_until"]
        # Capacity recovers: the same persisted retry stays due with ordinary checkins off.
        adapter.set_message_handler(lambda e: handler_recovered(e))

        async def handler_recovered(event):
            received.append(event)
            event._goal_execution_started = True

        await runner._goal_idle_wake_one(entry, 0, 3)
        await _drain(adapter)
        assert len(received) == 2
        assert not (await asyncio.to_thread(goals.load_goal, "goal-sess")).incomplete_retry_wait


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["edit", "pause", "clear", "replace", "resume", "wait", "unwait", "new-claim", "evaluate"])
async def test_retry_refund_respects_attempt_owner(rig, monkeypatch, mutation):
    monkeypatch.setattr(goals.time, "time", lambda: 1000.0)

    def scenario():
        mgr = goals.GoalManager("goal-sess")
        mgr.set("g")
        mgr.evaluate_after_turn("", turn_completed=False, max_incomplete_turns=1)
        before = mgr.idle_wake_snapshot()
        assert mgr.idle_wake_prompt(1060, checkin_minutes=0)
        after = mgr.idle_wake_snapshot()
        other = goals.GoalManager("goal-sess")
        if mutation == "edit":
            other.add_subgoal("preserved")
        elif mutation == "pause":
            other.pause()
        elif mutation == "clear":
            goals.clear_goal("goal-sess")
        elif mutation == "replace":
            other.set("g")  # deliberately identical goal + timestamp
        elif mutation == "resume":
            other.pause()
            other.resume()
        elif mutation == "wait":
            other.wait_for_seconds(100, "deployment")
        elif mutation == "unwait":
            other.stop_waiting()
        elif mutation == "new-claim":
            assert other.idle_wake_prompt(3000, checkin_minutes=30)
        else:
            other.evaluate_after_turn("", turn_completed=False, max_incomplete_turns=1)
        saved = goals.load_goal("goal-sess").to_json()
        undone = goals.undo_idle_wake("goal-sess", before, after)
        state = goals.load_goal("goal-sess")
        if mutation == "edit":
            assert undone
            assert state.subgoals == ["preserved"]
            assert state.incomplete_retry_wait and state.waiting_until == before["waiting_until"]
            assert not goals.undo_idle_wake("goal-sess", before, after)  # settle exactly once
        else:
            assert not undone
            assert state.to_json() == saved

    await asyncio.to_thread(scenario)


@pytest.mark.asyncio
async def test_busy_session_is_never_woken(rig):
    runner, adapter, received, key = rig
    await asyncio.to_thread(_due)                         # off-loop: no SessionDB bootstrap window
    runner._running_agents[key] = object()
    await runner._goal_idle_wake_scan()
    assert received == []
    assert (await asyncio.to_thread(goals.load_goal, "goal-sess")).waiting_until > 0  # barrier untouched


@pytest.mark.asyncio
async def test_checkins_off_in_config_disables_the_poller(rig):
    runner, adapter, received, key = rig
    runner.config = {"goals": {"checkin_minutes": 0}}
    mgr = goals.GoalManager("goal-sess")
    mgr.set("g")
    mgr.wait_for_seconds(1, reason="x")
    mgr.state.waiting_until = time.time() - 1
    mgr._save()
    await runner._goal_idle_wake_scan()
    assert received == []


def _due(sid="goal-sess"):
    mgr = goals.GoalManager(sid)
    mgr.set("g")
    mgr.wait_for_seconds(1, reason="x")
    mgr.state.waiting_until = time.time() - 1
    mgr._save()
    return mgr


@pytest.mark.asyncio
async def test_user_stop_latch_blocks_idle_wakes(rig):
    """Ace's /stop (or "stop the goal") must hold: a latched session is never woken by the poller."""
    runner, adapter, received, key = rig
    _due()
    runner._stop_latched = lambda k: k == key                # in-memory /stop latch
    await runner._goal_idle_wake_scan()
    assert received == []
    runner._stop_latched = lambda k: False
    entry = runner.session_store.list_sessions()[0]
    entry.user_stopped_at = time.time()                      # durable marker after a restart
    await runner._goal_idle_wake_scan()
    assert received == []


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["clear", "pause", "done"])
async def test_ended_goals_are_never_woken(rig, end):
    """Goal reached, /goal pause, or /goal clear: the poller has nothing to resume."""
    runner, adapter, received, key = rig
    mgr = _due()
    if end == "done":
        mgr.state.status = "done"
        mgr._save()
    else:
        getattr(mgr, end)()
    await runner._goal_idle_wake_scan()
    await _drain(adapter)
    assert received == []


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["clear", "pause"])
async def test_goal_command_racing_the_idle_wake_is_not_overwritten(rig, end):
    """A /goal pause|clear that lands while the idle wake is hopping to the executor wins: the wake
    must not save its stale active snapshot over it, and must not start a turn."""
    runner, adapter, received, key = rig
    await asyncio.to_thread(_due)                         # off-loop: no SessionDB bootstrap window
    real_hop = runner._run_in_executor_with_context

    async def hop_after_user_command(fn, *args):
        getattr(goals.GoalManager("goal-sess"), end)()     # the user's /goal command commits first
        runner._run_in_executor_with_context = real_hop
        return await real_hop(fn, *args)

    runner._run_in_executor_with_context = hop_after_user_command
    entry = runner.session_store.list_sessions()[0]
    await runner._goal_idle_wake_one(entry, 30, 3)
    await _drain(adapter)
    assert received == []
    assert (await asyncio.to_thread(goals.load_goal, "goal-sess")).status == ("cleared" if end == "clear" else "paused")


@pytest.mark.asyncio
async def test_goal_paused_after_the_wake_is_recorded_does_not_dispatch(rig):
    """Eligibility is re-checked after the executor hop: a pause committed after the wake was
    recorded (but before dispatch) still prevents the automatic turn."""
    runner, adapter, received, key = rig
    await asyncio.to_thread(_due)                         # off-loop: no SessionDB bootstrap window
    real_hop = runner._run_in_executor_with_context

    async def hop_then_user_pause(fn, *args):
        result = await real_hop(fn, *args)
        await asyncio.to_thread(lambda: goals.GoalManager("goal-sess").pause())
        return result

    runner._run_in_executor_with_context = hop_then_user_pause
    entry = runner.session_store.list_sessions()[0]
    await runner._goal_idle_wake_one(entry, 30, 3)
    await _drain(adapter)
    assert received == []
    assert (await asyncio.to_thread(goals.load_goal, "goal-sess")).status == "paused"


@pytest.mark.asyncio
async def test_user_message_arriving_during_the_hop_is_not_raced(rig):
    """A real message that made the session busy during the executor hop owns the next turn."""
    runner, adapter, received, key = rig
    await asyncio.to_thread(_due)                         # off-loop: no SessionDB bootstrap window
    real_hop = runner._run_in_executor_with_context

    async def hop_then_busy(fn, *args):
        result = await real_hop(fn, *args)
        runner._running_agents[key] = object()
        return result

    runner._run_in_executor_with_context = hop_then_busy
    entry = runner.session_store.list_sessions()[0]
    await runner._goal_idle_wake_one(entry, 30, 3)
    await _drain(adapter)
    assert received == []


@pytest.mark.asyncio
async def test_goal_command_waits_for_an_in_flight_idle_wake_and_reads_its_save(rig):
    """/goal pause issued while the idle wake holds the session's goal lock runs after it, on the
    wake's saved state (no lost update in either direction)."""
    runner, adapter, received, key = rig
    await asyncio.to_thread(_due)                         # off-loop: no SessionDB bootstrap window
    real_hop = runner._run_in_executor_with_context
    in_hop, release = asyncio.Event(), asyncio.Event()

    async def slow_hop(fn, *args):
        in_hop.set()
        await release.wait()
        runner._run_in_executor_with_context = real_hop
        return await real_hop(fn, *args)

    async def _manager(event):
        return await asyncio.to_thread(goals.GoalManager, "goal-sess"), SimpleNamespace(session_id="goal-sess")

    runner._run_in_executor_with_context = slow_hop
    runner._get_goal_manager_for_event = _manager
    runner._clear_goal_continuations = lambda *a: None
    entry = runner.session_store.list_sessions()[0]
    wake = asyncio.create_task(runner._goal_idle_wake_one(entry, 30, 3))
    await in_hop.wait()
    cmd_event = SimpleNamespace(source=entry.origin, get_command_args=lambda: "pause")
    command = asyncio.create_task(runner._handle_goal_command(cmd_event))
    await asyncio.sleep(0.05)
    assert not command.done()                                # serialized behind the wake
    release.set()
    await asyncio.wait_for(asyncio.gather(wake, command), 10)
    state = await asyncio.to_thread(goals.load_goal, "goal-sess")
    assert state.status == "paused"
    assert state.last_idle_wake_at > 0                       # the wake's save was not clobbered


@pytest.mark.asyncio
async def test_wake_abandoned_for_a_busy_session_does_not_consume_the_checkin(rig):
    """The session turned busy during the hop: no dispatch, and the recorded wake is given back so
    the last allowed check-in is still available once the session is idle again."""
    runner, adapter, received, key = rig

    def _quiet_due():
        mgr = goals.GoalManager("goal-sess")
        mgr.set("g")
        mgr.state.created_at = mgr.state.last_turn_at = time.time() - 31 * 60
        mgr._save()

    await asyncio.to_thread(_quiet_due)
    real_hop = runner._run_in_executor_with_context

    async def hop_then_busy(fn, *args):
        runner._run_in_executor_with_context = real_hop
        result = await real_hop(fn, *args)
        runner._running_agents[key] = object()
        return result

    runner._run_in_executor_with_context = hop_then_busy
    entry = runner.session_store.list_sessions()[0]
    await runner._goal_idle_wake_one(entry, 30, 1)
    await _drain(adapter)
    assert received == []
    state = await asyncio.to_thread(goals.load_goal, "goal-sess")
    assert (state.idle_wakes, state.last_idle_wake_at) == (0, 0.0)
    runner._running_agents.pop(key)
    await runner._goal_idle_wake_one(entry, 30, 1)          # idle again: the sole check-in fires
    await _drain(adapter)
    assert len(received) == 1


@pytest.mark.asyncio
async def test_idle_wake_undo_never_reactivates_a_goal_paused_from_another_surface(rig):
    """The give-back is one transaction against the stored row: a pause from a surface outside the
    gateway lock that landed after the wake was recorded is kept (no stale active state saved)."""
    def _scenario():
        mgr = goals.GoalManager("goal-sess")
        mgr.set("g")
        mgr.state.created_at = mgr.state.last_turn_at = time.time() - 31 * 60
        mgr._save()
        before = mgr.idle_wake_snapshot()
        assert mgr.idle_wake_prompt(checkin_minutes=30, max_checkins=1)
        after = mgr.idle_wake_snapshot()
        goals.GoalManager("goal-sess").pause()                # CLI / other surface
        undone = goals.undo_idle_wake("goal-sess", before, after)
        return undone, goals.load_goal("goal-sess")

    undone, state = await asyncio.to_thread(_scenario)
    assert undone is False
    assert state.status == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("claim", ["guard", "stop"])
@pytest.mark.parametrize("busy_after_hop", [1, 2])
async def test_checkin_is_never_handed_off_while_the_session_is_active(rig, busy_after_hop, claim):
    """Prism d8319143ee50 / 3f5d9f5e1df7: the final busy/stop check runs after the LAST executor
    await. A /goal command that claims the session's active guard during any pre-dispatch hop (here:
    after hop 1 or hop 2) must stop the wake; it must never be queued behind the command, where the
    pause/clear continuation sweep does not recognise a check-in. The abandoned wake is refunded."""
    runner, adapter, received, key = rig

    def _quiet_due():
        mgr = goals.GoalManager("goal-sess")
        mgr.set("g")
        mgr.state.created_at = mgr.state.last_turn_at = time.time() - 31 * 60
        mgr._save()

    await asyncio.to_thread(_quiet_due)
    real_hop = runner._run_in_executor_with_context
    hops = []

    async def hop_then_command_claims_session(fn, *args):
        result = await real_hop(fn, *args)
        hops.append(fn)
        if len(hops) == busy_after_hop:
            if claim == "guard":
                adapter._active_sessions[key] = asyncio.Event()   # /goal pause took the guard
            else:
                runner._stop_latched = lambda k: k == key         # Prism 5bf6d1a6c65b: a late /stop
        return result

    handed_off = []
    real_handle = adapter.handle_message

    async def spy_handle(event):
        handed_off.append(key in adapter._active_sessions or runner._stop_latched(key))
        return await real_handle(event)

    adapter.handle_message = spy_handle
    runner._run_in_executor_with_context = hop_then_command_claims_session
    entry = runner.session_store.list_sessions()[0]
    await runner._goal_idle_wake_one(entry, 30, 1)
    assert True not in handed_off                            # never queued behind the command
    if not handed_off:
        state = await asyncio.to_thread(goals.load_goal, "goal-sess")
        assert (state.idle_wakes, state.last_idle_wake_at) == (0, 0.0)   # refunded


@pytest.mark.asyncio
async def test_idle_wake_undo_never_writes_into_a_replacement_goal(rig):
    """Prism 3897e56402cb: the give-back is bound to the exact row the wake saved. A replacement goal
    whose wait fields equal the post-wake values must not inherit the old goal's deadline/reason."""
    def _scenario():
        mgr = goals.GoalManager("goal-sess")
        mgr.set("old goal")
        mgr.wait_for_seconds(1, reason="CI")
        mgr.state.waiting_until = time.time() - 1          # expired timer park
        mgr._save()
        before = mgr.idle_wake_snapshot()
        assert mgr.idle_wake_prompt(checkin_minutes=30, max_checkins=3)
        after = mgr.idle_wake_snapshot()
        assert after["waiting_until"] == 0.0 and before["waiting_until"] > 0
        goals.GoalManager("goal-sess").set("replacement")  # another surface replaces the goal
        undone = goals.undo_idle_wake("goal-sess", before, after)
        return undone, goals.load_goal("goal-sess")

    undone, state = await asyncio.to_thread(_scenario)
    assert undone is False
    assert state.goal == "replacement"
    assert (state.waiting_until, state.waiting_reason) == (0.0, None)


@pytest.mark.asyncio
async def test_idle_wake_undo_still_refunds_an_untouched_row(rig):
    """The identity guard does not disable the give-back: the row the wake saved is restored."""
    def _scenario():
        mgr = goals.GoalManager("goal-sess")
        mgr.set("g")
        mgr.state.created_at = mgr.state.last_turn_at = time.time() - 31 * 60
        mgr._save()
        before = mgr.idle_wake_snapshot()
        assert mgr.idle_wake_prompt(checkin_minutes=30, max_checkins=1)
        after = mgr.idle_wake_snapshot()
        return goals.undo_idle_wake("goal-sess", before, after), goals.load_goal("goal-sess")

    undone, state = await asyncio.to_thread(_scenario)
    assert undone is True
    assert (state.idle_wakes, state.last_idle_wake_at, state.status) == (0, 0.0, "active")


@pytest.mark.asyncio
async def test_reload_refreshes_the_stored_row_with_the_state(rig):
    """Prism 6c9984a79951: reload() re-reads the state AND the stored row together. A manager built
    before another writer saved must not keep the pre-save row as its write/compare token."""
    def _scenario():
        mgr = goals.GoalManager("goal-sess")
        mgr.set("g")
        stale = goals.GoalManager("goal-sess")                # e.g. built before waiting on the lock
        mgr.state.created_at = mgr.state.last_turn_at = time.time() - 31 * 60
        mgr._save()
        assert mgr.idle_wake_prompt(checkin_minutes=30, max_checkins=1)   # the wake saves meanwhile
        stale.reload()
        row = goals._get_session_db().get_meta_committed(goals._meta_key("goal-sess"))
        return stale, row

    stale, row = await asyncio.to_thread(_scenario)
    assert stale.state.idle_wakes == 1
    assert stale._stored == row                              # token matches the state it was read with
    assert stale.idle_wake_snapshot()["_row"] == row


@pytest.mark.asyncio
async def test_idle_wake_undo_holds_the_write_lock_across_read_compare_write(rig):
    """Prism 78812616985c: undo_idle_wake's SELECT, compare and UPDATE run in ONE BEGIN IMMEDIATE
    transaction. A writer on another connection (another process: CLI/TUI) that tries to land
    between the read and the write is refused with SQLITE_BUSY, so it can only commit before
    (the row no longer matches: no give-back) or after (it sees the refunded row)."""
    import sqlite3

    def _scenario():
        mgr = goals.GoalManager("goal-sess")
        mgr.set("g")
        mgr.state.created_at = mgr.state.last_turn_at = time.time() - 31 * 60
        mgr._save()
        before = mgr.idle_wake_snapshot()
        assert mgr.idle_wake_prompt(checkin_minutes=30, max_checkins=1)
        after = mgr.idle_wake_snapshot()
        db_path = str(goals._get_session_db().db_path)
        probes = []
        real_from_json = goals.GoalState.from_json
        orig_attr = goals.GoalState.__dict__["from_json"]

        def from_json_then_foreign_write(raw):                # runs between the txn's SELECT and UPDATE
            other = sqlite3.connect(db_path, timeout=0)
            try:
                other.execute("UPDATE state_meta SET value = value WHERE key = ?", (goals._meta_key("goal-sess"),))
                other.commit()
                probes.append("landed")
            except sqlite3.OperationalError as exc:
                probes.append(str(exc))
            finally:
                other.close()
            return real_from_json(raw)

        goals.GoalState.from_json = staticmethod(from_json_then_foreign_write)
        try:
            undone = goals.undo_idle_wake("goal-sess", before, after)
        finally:
            goals.GoalState.from_json = orig_attr
        return undone, probes, goals.load_goal("goal-sess")

    undone, probes, state = await asyncio.to_thread(_scenario)
    assert probes and all("locked" in p or "busy" in p for p in probes), probes
    assert undone is True
    assert (state.idle_wakes, state.last_idle_wake_at, state.status) == (0, 0.0, "active")
