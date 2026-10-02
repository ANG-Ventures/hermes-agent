"""FleetReview findings on #1409 (turn-lease leak fix), t_f4919e87.

1. State race: the stopped turn's ``finally`` releases the turn lease before
   awaiting the durable-marker clear. A queued same-key turn can then register
   its running agent, and the old turn's unguarded
   ``_release_running_agent_state`` wiped it, so ``/stop`` found nothing.
2. Dead-holder reclamation handed the lease straight to a NEWCOMER, overtaking
   a turn already queued on the lock (arrival order reversed).
3. A done() handler task does not prove its agent worker thread stopped: the
   lease must not move to a new turn while that thread can still write.
"""

import asyncio
import concurrent.futures
import threading

import pytest

from gateway.run import GatewayRunner
from gateway.session import build_session_key
from gateway.turn_lease import SessionTurnLeaseRegistry, bind_current_token
from tests.gateway.test_stop_turn_lease_leak import (
    SESSION_ID,
    _make_event,
    _make_runner,
    _wait_for,
)
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Finding 2: reclaim must go through the lock's FIFO queue.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dead_holder_reclaim_keeps_queued_turn_ahead_of_newcomer():
    registry = SessionTurnLeaseRegistry()
    die = asyncio.Event()
    order = []

    async def leaking_turn():
        token = await registry.acquire(SESSION_ID, owner_key="a", generation=1, timeout=5)
        token.owner_task = asyncio.current_task()
        await die.wait()  # dies without releasing

    async def turn(name):
        token = await registry.acquire(SESSION_ID, owner_key=name, generation=1, timeout=5)
        order.append(name)
        await asyncio.sleep(0.01)
        registry.release(token)

    holder = asyncio.create_task(leaking_turn())
    await _wait_for(
        lambda: SESSION_ID in registry._leases
        and registry._leases[SESSION_ID].holder is not None,
        "holder acquire",
    )
    b = asyncio.create_task(turn("b"))  # queued on the lock first
    await asyncio.sleep(0.02)
    die.set()
    await holder
    c = asyncio.create_task(turn("c"))  # arrives after the holder died
    await asyncio.wait_for(asyncio.gather(b, c), 5)

    assert order == ["b", "c"], f"later message overtook the queued turn: {order}"
    assert not registry._leases[SESSION_ID].lock.locked()


# ---------------------------------------------------------------------------
# Finding 3: a done() owner task with a live worker thread is NOT dead.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_holder_with_running_worker_is_not_reclaimed_until_worker_exits():
    registry = SessionTurnLeaseRegistry(stale_wait=0.2)
    worker_may_exit = threading.Event()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        async def cancelled_turn():
            token = await registry.acquire(SESSION_ID, owner_key="a", generation=1, timeout=5)
            token.owner_task = asyncio.current_task()
            token.add_worker(pool.submit(worker_may_exit.wait, 5))
            return token

        dead = await asyncio.create_task(cancelled_turn())
        waiter = asyncio.create_task(
            registry.acquire(SESSION_ID, owner_key="b", generation=1, timeout=5)
        )
        await asyncio.sleep(0.1)
        assert not waiter.done(), "lease moved while the old agent thread still runs"
        assert registry._leases[SESSION_ID].holder is dead

        worker_may_exit.set()
        token = await asyncio.wait_for(waiter, 5)
        assert registry._leases[SESSION_ID].holder is token
        assert registry.release(token) is True
    finally:
        worker_may_exit.set()
        pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_stopped_handler_keeps_lease_until_its_executor_worker_exits():
    """Real _handle_message + _run_in_executor_with_context in a child task."""
    registry = SessionTurnLeaseRegistry(stale_wait=0.3)
    runner = _make_runner(registry)
    runner.session_store.clear_turn_active = lambda *_a, **_k: True
    event = _make_event("proceed")
    worker_started = threading.Event()
    worker_may_exit = threading.Event()

    def _agent_thread():
        worker_started.set()
        worker_may_exit.wait(5)
        return {"final_response": "late"}

    async def turn(self_inner, ev, src, qk, generation):
        token = await registry.acquire(SESSION_ID, owner_key=qk, generation=generation, timeout=5)
        token.owner_task = asyncio.current_task()
        state = runner._session_state(qk).turn
        state.lease_tokens[generation] = token  # upstream: tokens keyed by run generation
        bind_current_token(token)
        # _run_agent spawns the executor call as its own task (ensure_future).
        executor_task = asyncio.ensure_future(
            runner._run_in_executor_with_context(_agent_thread)
        )
        await asyncio.wait({executor_task})
        return None

    try:
        with patch.object(GatewayRunner, "_handle_message_with_agent", turn):
            handler = asyncio.create_task(runner._handle_message(event))
            await asyncio.get_running_loop().run_in_executor(None, worker_started.wait, 5)
            handler.cancel()  # adapter cancel after /stop
            with pytest.raises(asyncio.CancelledError):
                await handler

        lease = registry._leases[SESSION_ID]
        assert lease.holder is not None and lease.lock.locked(), (
            "lease released while the stopped turn's agent thread still runs"
        )
        worker_may_exit.set()
        await _wait_for(lambda: lease.holder is None, "deferred lease release")
        assert not lease.lock.locked()
    finally:
        worker_may_exit.set()
        runner._shutdown_executor()


# ---------------------------------------------------------------------------
# Finding 1: the old turn's unwind must not clear a newer turn's state.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_turn_unwind_does_not_clear_newer_turns_running_agent():
    registry = SessionTurnLeaseRegistry(stale_wait=5)
    runner = _make_runner(registry)
    first = _make_event("first")
    key = build_session_key(first.source)

    clear_entered = threading.Event()
    clear_may_finish = threading.Event()
    calls = {"n": 0}

    def _slow_clear(session_key, token):
        calls["n"] += 1
        if calls["n"] == 1:  # only the OLD turn's clear is slow
            clear_entered.set()
            clear_may_finish.wait(5)
        return True

    runner.session_store.clear_turn_active = _slow_clear
    a_in_turn = asyncio.Event()
    a_may_return = asyncio.Event()
    b_registered = asyncio.Event()
    b_may_return = asyncio.Event()
    b_agent = object()

    async def turn(self_inner, ev, src, qk, generation):
        token = await registry.acquire(SESSION_ID, owner_key=qk, generation=generation, timeout=5)
        token.owner_task = asyncio.current_task()
        state = runner._session_state(qk).turn
        state.lease_tokens[generation] = token  # upstream: tokens keyed by run generation
        setattr(ev, "_gateway_active_turn_session_key", qk)
        setattr(ev, "_gateway_active_turn_token", f"durable-{generation}")
        if ev.text == "first":
            a_in_turn.set()
            await a_may_return.wait()
        else:
            state.agent = b_agent  # _run_agent registering the real agent
            b_registered.set()
            await b_may_return.wait()
        return None

    try:
        with patch.object(GatewayRunner, "_handle_message_with_agent", turn):
            a = asyncio.create_task(runner._handle_message(first))
            await asyncio.wait_for(a_in_turn.wait(), 5)
            assert await runner._handle_message(_make_event("/stop")) is not None
            b = asyncio.create_task(runner._handle_message(_make_event("second")))
            await asyncio.sleep(0.05)  # B claimed the slot, queued on the lease
            a_may_return.set()
            await asyncio.get_running_loop().run_in_executor(None, clear_entered.wait, 5)
            await asyncio.wait_for(b_registered.wait(), 5)
            clear_may_finish.set()
            await asyncio.wait_for(a, 5)

            state = runner._peek_session_state(key)
            assert state is not None and state.turn.agent is b_agent, (
                "the stopped turn's unwind cleared the newer turn's running agent"
            )
            b_may_return.set()
            await asyncio.wait_for(b, 5)
    finally:
        clear_may_finish.set()
        a_may_return.set()
        b_may_return.set()
