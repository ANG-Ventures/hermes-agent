"""/stop must not leave a zombie turn silently holding the turn lease.

Incident 2026-09-20 (Apollo gateway, Discord #apollo, session
20260920_053351_7c1cde9c). A turn (gen 2) took the per-session turn lease at
~06:34. The user ran ``/stop`` at 07:18:57:
``GatewayRunner._interrupt_and_clear_session`` hard-interrupted the agent,
bumped the run generation and released the per-key busy guard — but the turn's
thread was parked in a foreground ``terminal`` tool call, so it kept the LEASE
(released only in ``_handle_message``'s ``finally`` via
``_release_turn_lease``) until 07:48:47.

Meanwhile the user's next message ("continue", gen 4, 07:32:53) passed the
released busy guard and blocked in
``SessionTurnLeaseRegistry.acquire()`` **silently**, for a
``gateway_turn_lease_timeout`` of 1800s. The only log line said "two routing
keys are mapped to one session_id (#64934)" — which was FALSE: it was the same
routing key at a newer generation. The user saw nothing for 16 minutes.

Three properties are pinned here, one per layer of the fix:

a) a waiter queued behind a STALE-generation holder gets a user-visible
   "your message is queued" notice immediately, within the stale bound;
b) that waiter is bounded by ``gateway_stale_lease_wait`` (not the 1800s
   full lease timeout) and acquires as soon as the zombie's tool unwinds
   and releases;
c) a stale holder past the detector threshold emits the structured
   ``PHASE=stale_lease_holder ...`` line so the class is never invisible again.

Plus the honesty property: a genuinely LIVE alias-key holder is still treated
as alias contention (full timeout, #64934 wording, no queued notice) — the
stale path must not hijack the case the lease was built for.
"""

import asyncio
import logging
import threading
import time
from unittest.mock import MagicMock

import pytest

from gateway.run import GatewayRunner
from gateway.turn_lease import (
    STALE_HOLDER_LOG_AFTER,
    SessionTurnLeaseRegistry,
    TurnLeaseTimeoutError,
)

SESSION_ID = "20260920_053351_7c1cde9c"
ROUTING_KEY = "agent:main:discord:thread:12345:12345"
STOPPED_GENERATION = 2
NEXT_GENERATION = 4


def _run(coro):
    return asyncio.run(coro)


class _FakeForegroundTool:
    """Stands in for a turn parked in a foreground ``terminal`` call.

    ``run()`` blocks on a real thread until either the interrupt bit is
    observed (cooperative unwind, the fixed behavior) or the tool's own
    timeout expires (the zombie behavior). The gateway test never needs a real
    subprocess — what matters is that the lease is held across the block.
    """

    def __init__(self, tool_timeout: float = 30.0):
        self.tool_timeout = tool_timeout
        self.interrupted = threading.Event()
        self.entered = threading.Event()
        self.finished = threading.Event()
        self.name = "terminal"

    def run(self) -> None:
        self.entered.set()
        deadline = time.monotonic() + self.tool_timeout
        while time.monotonic() < deadline:
            if self.interrupted.wait(0.01):
                break
        self.finished.set()


class _StopTestAgent:
    """Minimal agent exposing the ABI ``request_hard_interrupt`` uses."""

    def __init__(self, tool: _FakeForegroundTool):
        self._tool = tool
        self._current_tool = tool.name
        self.hard_interrupt_calls: list = []

    def hard_interrupt(self, message=None, *, tool_reason=None):
        self.hard_interrupt_calls.append((message, tool_reason))
        # The real fix path: the interrupt reaches the tool, which unwinds.
        self._tool.interrupted.set()


def _bare_runner(registry: SessionTurnLeaseRegistry):
    """A GatewayRunner with exactly the collaborators /stop touches.

    Built via ``object.__new__`` per the house convention in
    ``tests/gateway/restart_test_helpers.py`` — the real
    ``_interrupt_and_clear_session`` and ``_is_session_run_current`` methods
    run unmodified against it.
    """
    runner = object.__new__(GatewayRunner)
    runner._turn_leases = registry
    runner._running_agents = {}
    runner._running_agent_tasks = {}
    runner._pending_messages = {}
    runner.session_store = MagicMock()
    runner.session_store._entries = {}
    runner._adapter_for_source = MagicMock(return_value=None)
    runner._thread_metadata_for_source = MagicMock(return_value=None)
    runner._release_running_agent_state = MagicMock()
    runner._evict_cached_agent = MagicMock()
    runner._generations = {ROUTING_KEY: STOPPED_GENERATION}

    def _is_current(session_key, generation):
        return int(runner._generations.get(session_key, 0)) == int(generation)

    def _invalidate(session_key, *, reason=""):
        runner._generations[session_key] = runner._generations.get(session_key, 0) + 1
        return runner._generations[session_key]

    runner._is_session_run_current = _is_current
    runner._invalidate_session_run_generation = _invalidate
    return runner


async def _stop_the_turn(runner, agent):
    """Drive the REAL /stop path on the runner."""
    state = MagicMock()
    state.turn.agent = agent
    state.persistent.pending_command_text = None
    runner._peek_session_state = MagicMock(return_value=state)
    source = MagicMock()
    source.chat_id = "12345"
    source.platform = "discord"
    await GatewayRunner._interrupt_and_clear_session(
        runner,
        ROUTING_KEY,
        source,
        interrupt_reason="user stop",
        invalidation_reason="stop_command",
        release_running_state=False,
    )


# ---------------------------------------------------------------------------
# (a) + (b): visible notice, and a SHORT bound that ends when the tool unwinds
# ---------------------------------------------------------------------------


def test_stop_then_next_message_gets_queued_notice_and_lease_released():
    """The full incident, reproduced end to end against the real /stop path."""

    async def scenario():
        registry = SessionTurnLeaseRegistry(stale_wait=5.0)
        runner = _bare_runner(registry)
        registry._is_generation_current = runner._is_session_run_current

        tool = _FakeForegroundTool(tool_timeout=30.0)
        agent = _StopTestAgent(tool)

        # The zombie-to-be takes the lease at gen 2 and parks in the tool.
        held = await registry.acquire(
            SESSION_ID,
            owner_key=ROUTING_KEY,
            generation=STOPPED_GENERATION,
            timeout=1800,
        )
        assert held is not None
        held.tool_name_hint = lambda: agent._current_tool

        tool_thread = threading.Thread(target=tool.run, daemon=True)
        tool_thread.start()
        assert tool.entered.wait(timeout=5)

        # /stop: hard-interrupts and BUMPS the generation. The lease is NOT
        # released here — that only happens in the turn's own finally.
        await _stop_the_turn(runner, agent)
        assert agent.hard_interrupt_calls, "/stop never reached the agent"
        assert not runner._is_session_run_current(ROUTING_KEY, STOPPED_GENERATION)
        assert registry._leases[SESSION_ID].holder is held, (
            "the stopped turn must still hold the lease — that IS the bug class"
        )

        # The user's next message on the SAME routing key at a newer
        # generation now contends with the zombie.
        notices: list = []

        async def on_stale(**kwargs):
            notices.append(kwargs)

        async def zombie_unwind():
            """The tool notices the interrupt and the turn's finally releases."""
            await asyncio.get_running_loop().run_in_executor(
                None, tool.finished.wait, 10
            )
            registry.release(held)

        unwind = asyncio.create_task(zombie_unwind())

        started = time.monotonic()
        token = await registry.acquire(
            SESSION_ID,
            owner_key=ROUTING_KEY,
            generation=NEXT_GENERATION,
            timeout=1800,
            on_stale_holder=on_stale,
        )
        elapsed = time.monotonic() - started
        await unwind

        # (a) the user was told, immediately, before any waiting happened.
        assert notices, (
            "a message queued behind a /stop'd zombie turn must produce a "
            "user-visible notice — silence was the incident's worst symptom"
        )
        assert notices[0]["owner_key"] == ROUTING_KEY
        assert notices[0]["generation"] == STOPPED_GENERATION
        # The wait bound handed to the waiter is the STALE bound, not 1800s.
        assert notices[0]["wait_seconds"] <= 5.0

        # (b) the lease was acquired once the interrupted tool let go.
        assert token is not None
        assert elapsed < 10.0, f"waiter took {elapsed:.1f}s"
        assert registry.release(token) is True

    _run(scenario())


def test_stale_holder_wait_is_bounded_and_rejects_with_resend_path():
    """A zombie that never unwinds must be rejected on the SHORT bound.

    The existing ``TurnLeaseTimeoutError`` rejection path (which already sends
    the user a resend notice) applies — but after ~seconds, not 1800s.
    """

    async def scenario():
        registry = SessionTurnLeaseRegistry(stale_wait=0.3)
        runner = _bare_runner(registry)
        registry._is_generation_current = runner._is_session_run_current

        held = await registry.acquire(
            SESSION_ID,
            owner_key=ROUTING_KEY,
            generation=STOPPED_GENERATION,
            timeout=1800,
        )
        assert held is not None
        await _stop_the_turn(runner, _StopTestAgent(_FakeForegroundTool()))

        started = time.monotonic()
        with pytest.raises(TurnLeaseTimeoutError) as excinfo:
            # Caller passes the FULL 1800s budget; the stale path must clamp it.
            await registry.acquire(
                SESSION_ID,
                owner_key=ROUTING_KEY,
                generation=NEXT_GENERATION,
                timeout=1800,
            )
        elapsed = time.monotonic() - started

        assert elapsed < 5.0, (
            f"waiter behind a stale holder blocked {elapsed:.1f}s — the caller's "
            "1800s budget must be clamped to the stale bound"
        )
        assert excinfo.value.wait_seconds <= 0.3
        registry.release(held)

    _run(scenario())


# ---------------------------------------------------------------------------
# (c) the structured detector line
# ---------------------------------------------------------------------------


def test_stale_holder_emits_structured_phase_line(caplog):
    """PHASE=stale_lease_holder is logged once, with key/gen/held/tool."""

    async def scenario():
        registry = SessionTurnLeaseRegistry(stale_wait=0.2)
        runner = _bare_runner(registry)
        registry._is_generation_current = runner._is_session_run_current

        held = await registry.acquire(
            SESSION_ID,
            owner_key=ROUTING_KEY,
            generation=STOPPED_GENERATION,
            timeout=1800,
        )
        assert held is not None
        held.tool_name_hint = lambda: "terminal"
        await _stop_the_turn(runner, _StopTestAgent(_FakeForegroundTool()))

        # Backdate the acquisition past the detector threshold, as a real
        # 30-minute zombie would be.
        registry._leases[SESSION_ID].acquired_at = (
            time.time() - STALE_HOLDER_LOG_AFTER - 30
        )

        for generation in (NEXT_GENERATION, NEXT_GENERATION + 1):
            with pytest.raises(TurnLeaseTimeoutError):
                await registry.acquire(
                    SESSION_ID,
                    owner_key=ROUTING_KEY,
                    generation=generation,
                    timeout=0.2,
                )
        registry.release(held)

    with caplog.at_level(logging.WARNING, logger="gateway.turn_lease"):
        _run(scenario())

    phase_lines = [
        r.getMessage()
        for r in caplog.records
        if "PHASE=stale_lease_holder" in r.getMessage()
    ]
    assert len(phase_lines) == 1, (
        f"expected exactly one detector line (one-shot latch), got {phase_lines}"
    )
    line = phase_lines[0]
    assert f"session={SESSION_ID}" in line
    assert f"key={ROUTING_KEY}" in line
    assert f"gen={STOPPED_GENERATION}" in line
    assert "held=" in line and "s tool=terminal" in line

    # The MISLEADING alias-key wording must not be used for a same-key zombie.
    stale_warnings = [
        r.getMessage()
        for r in caplog.records
        if "turn lease" in r.getMessage() and "PHASE=" not in r.getMessage()
    ]
    assert stale_warnings, "the stale contention warning must still be logged"
    assert all(
        "two routing keys are mapped to one session_id" not in m
        for m in stale_warnings
    ), "a same-key stale holder must NOT be reported as alias-key contention"
    assert any("STALE" in m and "still draining" in m for m in stale_warnings)


# ---------------------------------------------------------------------------
# Honesty control: a LIVE alias-key holder keeps the original semantics
# ---------------------------------------------------------------------------


def test_live_alias_key_holder_is_not_treated_as_stale(caplog):
    """The #64934 case must keep the full timeout, its wording, and no notice."""

    async def scenario():
        registry = SessionTurnLeaseRegistry(stale_wait=0.2)
        runner = _bare_runner(registry)
        registry._is_generation_current = runner._is_session_run_current

        # Holder's generation IS current: a genuinely live turn.
        held = await registry.acquire(
            SESSION_ID,
            owner_key=ROUTING_KEY,
            generation=STOPPED_GENERATION,
            timeout=1800,
        )
        assert held is not None
        assert runner._is_session_run_current(ROUTING_KEY, STOPPED_GENERATION)

        notices: list = []

        async def on_stale(**kwargs):
            notices.append(kwargs)

        waiter = asyncio.create_task(
            registry.acquire(
                SESSION_ID,
                owner_key="agent:main:discord:thread:99999:99999",
                generation=1,
                timeout=5,
                on_stale_holder=on_stale,
            )
        )
        # The stale bound (0.2s) must NOT apply to a live holder.
        await asyncio.sleep(1.0)
        assert not waiter.done(), (
            "a live alias-key waiter must not be clamped to the stale bound"
        )
        assert not notices, "no stale notice for a genuinely live holder"

        registry.release(held)
        token = await waiter
        assert token is not None
        registry.release(token)

    with caplog.at_level(logging.WARNING, logger="gateway.turn_lease"):
        _run(scenario())

    messages = [r.getMessage() for r in caplog.records]
    assert any("two routing keys are mapped to one session_id" in m for m in messages)
    assert not any("PHASE=stale_lease_holder" in m for m in messages)


def test_registry_without_predicate_keeps_pre_fix_behavior():
    """Standalone use (no runner predicate) must never shorten a wait."""

    async def scenario():
        registry = SessionTurnLeaseRegistry(stale_wait=0.2)
        held = await registry.acquire(
            SESSION_ID, owner_key=ROUTING_KEY, generation=1, timeout=5
        )
        waiter = asyncio.create_task(
            registry.acquire(
                SESSION_ID, owner_key=ROUTING_KEY, generation=2, timeout=5
            )
        )
        await asyncio.sleep(0.8)
        assert not waiter.done(), "fail-open: no predicate means no stale path"
        registry.release(held)
        token = await waiter
        registry.release(token)

    _run(scenario())
