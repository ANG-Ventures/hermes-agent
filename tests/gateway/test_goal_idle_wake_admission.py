"""Goal idle wake: refund on a failed post-save reload, and eligibility checked at the adapter claim.

Prism 99ea851df13d / eb13daf988ed (hermes-agent#2132 post-merge review, card t_440984bb).
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.run import GatewayRunner
from hermes_cli import goals

from tests.gateway.test_goal_idle_wake_gateway import _Adapter, _drain, rig  # noqa: F401  (fixture)


def _quiet_due_goal():
    mgr = goals.GoalManager("goal-sess")
    mgr.set("g")
    mgr.state.created_at = mgr.state.last_turn_at = time.time() - 31 * 60
    mgr._save()


@pytest.mark.asyncio
async def test_failed_post_save_reload_refunds_the_recorded_wake(rig):
    """Prism 99ea851df13d: a transient read failure on the post-save reload is not proof another
    writer ended the goal. The wake is not dispatched, and the recorded check-in is given back so the
    still-active goal can be resumed by the next scan."""
    runner, adapter, received, key = rig
    await asyncio.to_thread(_quiet_due_goal)
    real_hop = runner._run_in_executor_with_context
    db = await asyncio.to_thread(goals._get_session_db)
    real_get = db.get_meta_committed
    hops = []

    def _locked(*a, **k):
        raise RuntimeError("database is locked")

    async def hop_then_read_fails(fn, *args):
        result = await real_hop(fn, *args)
        hops.append(fn)
        db.get_meta_committed = _locked if len(hops) == 1 else real_get   # only the reload's read fails
        return result

    runner._run_in_executor_with_context = hop_then_read_fails
    entry = runner.session_store.list_sessions()[0]
    try:
        await runner._goal_idle_wake_one(entry, 30, 1)
    finally:
        db.get_meta_committed = real_get
    await _drain(adapter)
    assert received == []
    state = await asyncio.to_thread(goals.load_goal, "goal-sess")
    assert (state.status, state.idle_wakes, state.last_idle_wake_at) == ("active", 0, 0.0)   # refunded
    runner._run_in_executor_with_context = real_hop
    await runner._goal_idle_wake_one(entry, 30, 1)          # the sole check-in is still available
    await _drain(adapter)
    assert len(received) == 1


class _PreparingAdapter(_Adapter):
    """Discord's shape: an internal event awaits a channel lookup before the base claim."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.in_prepare, self.release = asyncio.Event(), asyncio.Event()

    async def handle_message(self, event):
        if event.internal:
            self.in_prepare.set()
            await self.release.wait()                       # get_chat_info()
        await super().handle_message(event)


def _preparing(runner, received):
    adapter = _PreparingAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.DISCORD)

    async def handler(event):
        received.append(event)

    adapter.set_message_handler(handler)
    runner._delivery_adapter_for = lambda s: adapter
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("claim", ["guard", "stop"])
async def test_checkin_refused_when_the_session_is_claimed_during_adapter_preparation(rig, claim):
    """Prism eb13daf988ed: the final eligibility check runs at the adapter's claim, after the
    adapter's own awaits. A /goal command (guard) or /stop landing during Discord's get_chat_info
    stops the check-in: not started, not queued behind the command, and the wake is refunded."""
    runner, _unused, received, key = rig
    adapter = _preparing(runner, received)
    await asyncio.to_thread(_quiet_due_goal)
    entry = runner.session_store.list_sessions()[0]
    wake = asyncio.create_task(runner._goal_idle_wake_one(entry, 30, 1))
    await asyncio.wait_for(adapter.in_prepare.wait(), 10)
    if claim == "guard":
        adapter._active_sessions[key] = asyncio.Event()     # /goal pause claimed the session
    else:
        runner._stop_latched = lambda k: k == key
    adapter.release.set()
    await asyncio.wait_for(wake, 10)
    await _drain(adapter)
    assert received == []
    assert key not in adapter._pending_messages             # not queued behind the command
    assert not runner._queued_events.get(key)
    state = await asyncio.to_thread(goals.load_goal, "goal-sess")
    assert (state.idle_wakes, state.last_idle_wake_at) == (0, 0.0)


@pytest.mark.asyncio
async def test_unclaimed_session_still_receives_the_checkin_through_a_preparing_adapter(rig):
    """The admission check does not refuse an eligible wake."""
    runner, _unused, received, key = rig
    adapter = _preparing(runner, received)
    await asyncio.to_thread(_quiet_due_goal)
    entry = runner.session_store.list_sessions()[0]
    adapter.release.set()
    await runner._goal_idle_wake_one(entry, 30, 1)
    await _drain(adapter)
    assert len(received) == 1 and received[0].text.startswith(goals.GOAL_CHECKIN_PREFIX)
    assert (await asyncio.to_thread(goals.load_goal, "goal-sess")).idle_wakes == 1


@pytest.mark.asyncio
async def test_gateway_goal_pause_cannot_complete_during_adapter_preparation(rig):
    """The goal lock is held through admission: a gateway /goal pause issued while the adapter
    prepares the check-in runs after the hand-off, so its continuation sweep sees the check-in."""
    runner, _unused, received, key = rig
    adapter = _preparing(runner, received)
    await asyncio.to_thread(_quiet_due_goal)

    async def _manager(event):
        return await asyncio.to_thread(goals.GoalManager, "goal-sess"), SimpleNamespace(session_id="goal-sess")

    runner._get_goal_manager_for_event = _manager
    runner._clear_goal_continuations = lambda *a: None
    entry = runner.session_store.list_sessions()[0]
    wake = asyncio.create_task(runner._goal_idle_wake_one(entry, 30, 1))
    await asyncio.wait_for(adapter.in_prepare.wait(), 10)
    assert runner._goal_session_lock("goal-sess").locked()   # held while the adapter prepares
    command = asyncio.create_task(
        runner._handle_goal_command(SimpleNamespace(source=entry.origin, get_command_args=lambda: "pause")))
    await asyncio.sleep(0.5)
    assert not command.done()                                # serialized behind the admission
    adapter.release.set()
    await asyncio.wait_for(asyncio.gather(wake, command), 10)
    assert (await asyncio.to_thread(goals.load_goal, "goal-sess")).status == "paused"


def test_goal_checkins_are_recognised_as_goal_synthetic_events():
    """Prism eb13daf988ed (cleanup half): /goal pause|clear sweep queued check-ins too, not only
    continuations; a real /queue item is still spared."""
    is_goal = GatewayRunner._is_goal_continuation_event
    checkin = goals.GOAL_CHECKIN_PROMPT_TEMPLATE.format(n=1, max=3, idle_min=31, goal="g")
    stuck = goals.GOAL_STUCK_CHECKIN_PROMPT_TEMPLATE.format(
        n=1, max=3, age_min=31, target="pid 1", reason="r", goal="g")
    assert is_goal(SimpleNamespace(text=checkin)) and is_goal(stuck)
    assert is_goal(goals.CONTINUATION_PROMPT_TEMPLATE.format(goal="g"))
    assert not is_goal(SimpleNamespace(text="please check the PR")) and not is_goal(None)


@pytest.mark.asyncio
async def test_loop_tick_refused_by_a_stop_landing_during_adapter_preparation(rig):
    """Same class in /loop wakeups: the /stop check precedes the fire_tick hop and the adapter's own
    awaits, so it is re-checked at the claim; a refused tick is rolled back and stays due."""
    from hermes_cli import loops

    runner, _unused, received, key = rig
    adapter = _preparing(runner, received)
    sid = "loop-sess"
    runner._adapters_for_profile = lambda profile: {Platform.DISCORD: adapter}
    runner._build_process_event_source = lambda evt: rig_source(runner)
    runner._session_key_for_source = lambda source: key
    runner._stop_latched = lambda k: False

    def _arm():
        mgr = loops.LoopManager(session_id=sid)
        mgr.set("poll CI", interval_seconds=300, route={"platform": "discord", "chat_id": "77"})
        mgr.state.next_due_at = time.time() - 1
        loops.save_loop(sid, mgr.state)
        return loops.load_loop(sid)

    state = await asyncio.to_thread(_arm)
    fire = asyncio.create_task(runner._loop_wakeup_fire_one(sid, state, time.time(), set()))
    await asyncio.wait_for(adapter.in_prepare.wait(), 10)
    runner._stop_latched = lambda k: k == key                # /stop lands during get_chat_info
    adapter.release.set()
    await asyncio.wait_for(fire, 10)
    await _drain(adapter)
    assert received == []
    assert key not in adapter._pending_messages
    after = await asyncio.to_thread(loops.load_loop, sid)
    assert (after.awaiting_response, after.ticks_fired) == (False, 0)   # rolled back


def rig_source(runner):
    return runner.session_store.list_sessions()[0].origin
