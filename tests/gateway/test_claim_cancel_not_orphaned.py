"""A cancelled poll never orphans a state.db claim it started off the loop (Prism fd214c8b7386, #2325).

#2325 moved the heartbeat ``due_prompt()`` claim and the loop ``fire_tick()`` claim into an executor hop.
A worker thread cannot be stopped once it runs: when the awaiting poll is cancelled (poller stop, gateway
shutdown) while the claim waits on the SessionDB lock, the thread still persists the claim after the lock
frees, and nobody dispatches the event or calls ``abandon_fire``/``abandon_tick``. The heartbeat attempt is
consumed without delivery; the loop tick stays ``awaiting_response`` and never fires again.
"""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from hermes_cli import goals, loops
from hermes_cli.heartbeat import HeartbeatManager, load_heartbeat


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
def env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    db = goals._get_session_db()
    yield db
    goals._DB_CACHE.clear()


def _wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


async def _cancel_while_claim_blocked(make_coro, claim_started, gate):
    """Start ``make_coro()``, cancel it while the claim is blocked in its worker thread (stands in for
    the SessionDB lock wait), then let the worker finish its write."""
    task = asyncio.ensure_future(make_coro())
    try:
        assert await asyncio.to_thread(claim_started.wait, 3), "the claim never reached the worker"
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        gate.set()


def _blocking(real, started, gate, finished):
    def _wrapped(self, *a, **k):
        started.set()
        gate.wait(5)  # the db lock wait: the coroutine is cancelled meanwhile, the thread is not
        try:
            return real(self, *a, **k)
        finally:
            finished.set()
    return _wrapped


@pytest.mark.asyncio
async def test_cancelled_heartbeat_poll_gives_back_the_claim(env, monkeypatch):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", user_id="42", chat_type="dm")
    key = build_session_key(source)
    adapter = _Adapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)
    received = []

    async def handler(event):
        received.append(event)

    adapter.set_message_handler(handler)
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._delivery_adapter_for = lambda s: adapter
    runner._run_in_executor_with_context = asyncio.to_thread
    watch = {key: (source, "hb-sess")}
    await asyncio.to_thread(lambda: HeartbeatManager("hb-sess").set("check status", 60))
    st = await asyncio.to_thread(load_heartbeat, "hb-sess")
    st.last_fired_at = time.time() - 3600  # due now
    from hermes_cli.heartbeat import save_heartbeat
    await asyncio.to_thread(save_heartbeat, "hb-sess", st)
    before = (await asyncio.to_thread(load_heartbeat, "hb-sess")).fire_count

    started, gate, finished = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(
        HeartbeatManager, "due_prompt", _blocking(HeartbeatManager.due_prompt, started, gate, finished))
    await _cancel_while_claim_blocked(lambda: runner._heartbeat_poll_once(watch), started, gate)
    assert await asyncio.to_thread(finished.wait, 5)  # the worker persisted its claim
    claimed = await asyncio.to_thread(load_heartbeat, "hb-sess")
    assert claimed.fire_count in (before, before + 1)

    # The worker finishes its claim after the lock frees; the claim must be given back, not consumed.
    def _settled():
        s = load_heartbeat("hb-sess")
        return s is not None and s.fire_count == before and s.is_due()

    assert await asyncio.to_thread(_wait_until, _settled), (
        f"orphaned heartbeat claim: fire_count {before} -> {load_heartbeat('hb-sess').fire_count}, never dispatched")
    assert received == []


@pytest.mark.asyncio
async def test_cancelled_loop_wakeup_gives_back_the_tick(env, monkeypatch):
    sid = "loop-sess"
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._queued_events = {}
    runner._run_in_executor_with_context = asyncio.to_thread
    handled = []

    class _LoopAdapter:
        async def handle_message(self, event):
            handled.append(event.text)

    runner._adapters_for_profile = lambda profile: {Platform.DISCORD: _LoopAdapter()}
    runner._build_process_event_source = lambda evt: SimpleNamespace(
        platform=Platform.DISCORD, chat_id=evt["chat_id"], chat_type=evt["chat_type"],
        thread_id=evt["thread_id"] or None, user_id=evt["user_id"], user_name=evt["user_name"], profile=None,
    )
    runner._session_key_for_source = lambda source: "agent:main:discord:channel:chat-loop"
    runner._stop_latched = lambda key: False

    def _arm():
        mgr = loops.LoopManager(session_id=sid)
        mgr.set("poll CI", interval_seconds=300, route={"platform": "discord", "chat_id": "chat-loop"})
        mgr.state.next_due_at = time.time() - 1
        loops.save_loop(sid, mgr.state)
        return loops.load_loop(sid)

    state = await asyncio.to_thread(_arm)
    started, gate, finished = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(
        loops.LoopManager, "fire_tick", _blocking(loops.LoopManager.fire_tick, started, gate, finished))
    await _cancel_while_claim_blocked(
        lambda: runner._loop_wakeup_fire_one(sid, state, time.time(), set()), started, gate)
    assert await asyncio.to_thread(finished.wait, 5)  # the worker persisted its claim

    def _settled():
        s = loops.load_loop(sid)
        return s is not None and not s.awaiting_response and s.ticks_fired == 0

    assert await asyncio.to_thread(_wait_until, _settled), (
        f"orphaned loop tick: {vars(loops.load_loop(sid))} (awaiting_response wedges the loop forever)")
    assert handled == []
