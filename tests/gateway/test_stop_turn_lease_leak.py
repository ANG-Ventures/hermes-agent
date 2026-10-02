"""/stop mid API call must not leak the in-memory turn lease.

Incident 2026-09-27 (Apollo gateway pid 78663, Discord thread
1553930457287102464, session 20260927_183134_b6f8858a). Turn gen 1 held the
per-session turn lease. ``/stop`` landed while the agent was mid API call:

- 23:47:53.340 the gen-1 handler logged "Discarding stale agent result" and
  unwound into ``_handle_message``'s ``finally``, which awaits
  ``_clear_durable_active_turn`` (``asyncio.to_thread``) BEFORE releasing the
  turn lease.
- 23:47:53.366 the adapter delivered the /stop reply and then called
  ``cancel_session_processing`` -> ``task.cancel()`` on that same handler task.

The CancelledError landed on the finally's await and propagated out of the
finally, so ``_release_running_agent_state`` and ``_release_turn_lease`` never
ran. The durable marker had already been cleared by the thread; only the
in-memory registry lease leaked, and every later message on the session timed
out behind the dead gen-1 holder until the gateway restarted.
"""

import asyncio
import logging
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from gateway.turn_lease import SessionTurnLeaseRegistry, TurnLeaseTimeoutError

SESSION_ID = "20260927_183134_b6f8858a"


class _FakeAdapter:
    def __init__(self):
        self._pending_messages = {}
        self._active_sessions = {}

    async def send(self, chat_id, text, **kwargs):
        pass

    async def interrupt_session_activity(self, session_key, chat_id):
        event = self._active_sessions.get(session_key)
        if event is not None:
            event.set()


def _make_runner(registry):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    runner.adapters = {Platform.TELEGRAM: _FakeAdapter()}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._running_agent_tasks = {}
    runner._session_run_generation = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._voice_mode = {}
    runner._background_tasks = set()
    runner._draining = False
    runner._restart_requested = False
    runner._restart_task_started = False
    runner._restart_detached = False
    runner._restart_via_service = False
    runner._restart_drain_timeout = 0.0
    runner._stop_task = None
    runner._exit_code = None
    runner._update_runtime_status = MagicMock()
    runner._is_user_authorized = lambda _source: True
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.session_store = MagicMock()
    runner.delivery_router = MagicMock()
    runner._turn_leases = registry
    registry._is_generation_current = runner._is_session_run_current
    return runner


def _make_event(text):
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="1553930457287102464",
        chat_type="dm", user_id="u1",
    )
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=source)


async def _wait_for(predicate, what, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_stop_mid_api_call_then_adapter_cancel_does_not_leak_lease():
    """Reproduces the 2026-09-27 leak on the real ``_handle_message`` path."""
    registry = SessionTurnLeaseRegistry(stale_wait=0.3)
    runner = _make_runner(registry)
    event = _make_event("proceed")
    key = build_session_key(event.source)

    api_call_in_flight = asyncio.Event()
    api_call_returns = asyncio.Event()
    marker_clear_entered = threading.Event()
    marker_clear_may_finish = threading.Event()

    def _slow_clear_turn_active(session_key, token):
        # Runs in asyncio.to_thread, as in production. It completes (the
        # durable marker IS cleared), it is just not instant under load.
        marker_clear_entered.set()
        marker_clear_may_finish.wait(5)
        return True

    runner.session_store.clear_turn_active = _slow_clear_turn_active

    async def turn_mid_api_call(self_inner, ev, src, qk, generation):
        # The production acquisition, exactly as _handle_message_with_agent
        # performs it after session resolution.
        token = await registry.acquire(
            SESSION_ID, owner_key=qk, generation=generation, timeout=1800
        )
        token.owner_task = asyncio.current_task()
        # Parity 2026-10-01: tokens are keyed by acquiring run generation (upstream
        # TurnState.lease_tokens) so a displaced turn frees only its own lease.
        state = runner._session_state(qk).turn
        state.lease_tokens[generation] = token
        # _mark_durable_active_turn's carrier attributes.
        setattr(ev, "_gateway_active_turn_session_key", qk)
        setattr(ev, "_gateway_active_turn_token", "durable-token")
        api_call_in_flight.set()
        await api_call_returns.wait()
        # interrupted_during_api_call -> stale generation -> discarded.
        assert not runner._is_session_run_current(qk, generation)
        return None

    with patch.object(GatewayRunner, "_handle_message_with_agent", turn_mid_api_call):
        handler = asyncio.create_task(runner._handle_message(event))
        await asyncio.wait_for(api_call_in_flight.wait(), 5)

        # /stop through the real dispatch path (bumps the generation).
        stop_reply = await runner._handle_message(_make_event("/stop"))
        assert stop_reply is not None

        # The agent thread returns from the interrupted API call and the
        # handler unwinds into its finally, parking on the marker clear.
        api_call_returns.set()
        await _wait_for(marker_clear_entered.is_set, "durable marker clear")

        # base.cancel_session_processing: the adapter cancels the old task
        # right after delivering the /stop reply.
        handler.cancel()
        marker_clear_may_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await handler

    lease = registry._leases[SESSION_ID]
    assert lease.holder is None, (
        "the stopped turn's handler unwound but its turn lease is still held "
        f"by {lease.holder!r}: every later message on this session is rejected"
    )
    assert not lease.lock.locked()
    state = runner._peek_session_state(key)
    assert not state.turn.lease_tokens

    # The user's next message acquires at once instead of being rejected.
    token = await registry.acquire(
        SESSION_ID, owner_key=key, generation=99, timeout=0.5
    )
    assert token is not None
    assert registry.release(token) is True


# ---------------------------------------------------------------------------
# Backstop: a holder whose owning task is done() can never release.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_waiter_reclaims_lease_from_holder_whose_task_is_done(caplog):
    registry = SessionTurnLeaseRegistry(stale_wait=0.3)
    generations = {"k": 2}
    registry._is_generation_current = lambda k, g: generations.get(k) == g

    async def leaking_turn():
        # Acquires and dies without releasing (the incident's end state).
        token = await registry.acquire(
            SESSION_ID, owner_key="k", generation=1, timeout=5
        )
        token.owner_task = asyncio.current_task()
        return token

    dead = await asyncio.create_task(leaking_turn())
    assert registry._leases[SESSION_ID].holder is dead

    with caplog.at_level(logging.WARNING, logger="gateway.turn_lease"):
        loop = asyncio.get_running_loop()
        started = loop.time()
        token = await registry.acquire(
            SESSION_ID, owner_key="k", generation=2, timeout=1800
        )
        elapsed = loop.time() - started

    assert token is not None
    assert registry._leases[SESSION_ID].holder is token
    assert elapsed < 0.3, "a dead holder must be reclaimed without waiting"
    assert dead.released is True
    assert registry.release(dead) is False, "the dead token cannot free the new holder"
    assert any("PHASE=turn_lease_reclaimed" in r.getMessage() for r in caplog.records)
    assert registry.release(token) is True
    assert not registry._leases[SESSION_ID].lock.locked()


@pytest.mark.asyncio
async def test_waiter_reclaims_when_holder_task_dies_during_the_wait():
    registry = SessionTurnLeaseRegistry(stale_wait=0.3)
    generations = {"k": 2}
    registry._is_generation_current = lambda k, g: generations.get(k) == g
    die = asyncio.Event()

    async def leaking_turn():
        token = await registry.acquire(
            SESSION_ID, owner_key="k", generation=1, timeout=5
        )
        token.owner_task = asyncio.current_task()
        await die.wait()

    holder_task = asyncio.create_task(leaking_turn())
    await _wait_for(
        lambda: SESSION_ID in registry._leases
        and registry._leases[SESSION_ID].holder is not None,
        "holder acquire",
    )
    waiter = asyncio.create_task(
        registry.acquire(SESSION_ID, owner_key="k", generation=2, timeout=1800)
    )
    await asyncio.sleep(0.05)
    die.set()
    await holder_task
    token = await asyncio.wait_for(waiter, 5)
    assert registry._leases[SESSION_ID].holder is token
    assert registry.release(token) is True


@pytest.mark.asyncio
async def test_stale_holder_whose_task_is_still_running_is_not_reclaimed():
    registry = SessionTurnLeaseRegistry(stale_wait=0.2)
    generations = {"k": 2}
    registry._is_generation_current = lambda k, g: generations.get(k) == g
    finish = asyncio.Event()
    held = {}

    async def draining_turn():
        held["token"] = await registry.acquire(
            SESSION_ID, owner_key="k", generation=1, timeout=5
        )
        held["token"].owner_task = asyncio.current_task()
        await finish.wait()
        registry.release(held["token"])

    holder_task = asyncio.create_task(draining_turn())
    await _wait_for(lambda: "token" in held, "holder acquire")

    with pytest.raises(TurnLeaseTimeoutError):
        await registry.acquire(SESSION_ID, owner_key="k", generation=2, timeout=1800)
    assert registry._leases[SESSION_ID].holder is held["token"]
    assert held["token"].released is False

    finish.set()
    await holder_task
    token = await registry.acquire(SESSION_ID, owner_key="k", generation=2, timeout=1)
    assert registry.release(token) is True


# ---------------------------------------------------------------------------
# A failed release that leaves the lease held is logged at WARNING.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_turn_lease_mismatch_on_held_token_warns(caplog):
    registry = SessionTurnLeaseRegistry()
    runner = _make_runner(registry)
    key = "agent:main:discord:thread:1:1"
    token = await registry.acquire(SESSION_ID, owner_key=key, generation=1, timeout=1)
    state = runner._session_state(key).turn
    state.lease_tokens[1] = token

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        assert runner._release_turn_lease(key, 2) is False

    assert any(
        "turn lease NOT released" in r.getMessage() and r.levelno == logging.WARNING
        for r in caplog.records
    )
    assert registry._leases[SESSION_ID].holder is token
    assert runner._release_turn_lease(key, 1) is True


# ---------------------------------------------------------------------------
# Honest notice: no "finishing a tool call" when no tool is running.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_notice_receives_holder_tool_name():
    registry = SessionTurnLeaseRegistry(stale_wait=0.1)
    generations = {"k": 2}
    registry._is_generation_current = lambda k, g: generations.get(k) == g
    notices = []

    async def on_stale(**kwargs):
        notices.append(kwargs)

    held = await registry.acquire(SESSION_ID, owner_key="k", generation=1, timeout=5)
    for hint, expected in ((None, None), (lambda: "terminal", "terminal")):
        held.tool_name_hint = hint
        with pytest.raises(TurnLeaseTimeoutError):
            await registry.acquire(
                SESSION_ID, owner_key="k", generation=2, timeout=5,
                on_stale_holder=on_stale,
            )
        assert notices[-1]["tool_name"] == expected
    registry.release(held)


def test_stale_notice_text_is_honest_about_the_tool():
    text = GatewayRunner._stale_lease_notice_text
    assert "tool call" not in text(None)
    assert "terminal" in text("terminal")
