"""Regression test: /stop cancels pending clarify prompts immediately.

Incident 2026-07-19: a turn was blocked on a ``clarify`` question when the
user ``/stop``'d it.  ``_interrupt_and_clear_session`` interrupted the agent
and invalidated the generation but left the clarify entry registered in
``tools.clarify_gateway`` (cleanup only ran in the turn's ``finally``, which
a thread parked in ``wait_for_response`` had not reached).  The user's NEXT
message was then intercepted by the gateway's clarify hook and fed to the
dead turn — whose output was suppressed by the invalidated generation — so
the message was silently swallowed and the user had to double-message.

The fix: ``_interrupt_and_clear_session`` calls ``clarify_gateway
.clear_session`` for the session, cancelling every pending entry (which also
unblocks the waiting agent thread via the empty-string sentinel).
"""

import logging
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.platforms.base import Platform
from hermes_state import SessionDB
from tools import clarify_gateway


SESSION_KEY = "agent:main:discord:thread:12345:12345"


def _source():
    return SessionSource(
        platform=Platform.DISCORD,
        chat_type="group",
        chat_id="12345",
        thread_id="12345",
        user_id="u1",
    )


def _bare_runner():
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agent_tasks = {}
    runner._draining_turns = {}
    runner._pending_messages = {}
    runner.session_store = MagicMock()
    runner.session_store._entries = {}
    runner._invalidate_session_run_generation = MagicMock()
    runner._adapter_for_source = MagicMock(return_value=None)
    runner._thread_metadata_for_source = MagicMock(return_value=None)
    runner._release_running_agent_state = MagicMock()
    runner._evict_cached_agent = MagicMock()
    return runner


@pytest.fixture(autouse=True)
def _clean_clarify_state():
    clarify_gateway.clear_session(SESSION_KEY)
    yield
    clarify_gateway.clear_session(SESSION_KEY)


@pytest.mark.asyncio
async def test_stop_cancels_pending_clarify():
    """A pending clarify must not survive _interrupt_and_clear_session."""
    entry = clarify_gateway.register(
        clarify_id="stopclr0001",
        session_key=SESSION_KEY,
        question="rotate now or defer?",
        choices=[],
    )
    assert clarify_gateway.get_pending_for_session(SESSION_KEY) is not None

    runner = _bare_runner()
    await runner._interrupt_and_clear_session(
        SESSION_KEY,
        _source(),
        interrupt_reason="stop_command",
        invalidation_reason="stop_command",
    )

    # Entry is gone: the next inbound message cannot be intercepted as a
    # clarify answer for the dead turn.
    assert clarify_gateway.get_pending_for_session(SESSION_KEY) is None
    assert not clarify_gateway.has_pending(SESSION_KEY)
    # The blocked agent thread was unblocked via the cancellation sentinel.
    assert entry.event.is_set()


@pytest.mark.asyncio
async def test_stop_without_pending_clarify_is_noop():
    """No clarify pending → stop path must not raise or log-error."""
    runner = _bare_runner()
    await runner._interrupt_and_clear_session(
        SESSION_KEY,
        _source(),
        interrupt_reason="stop_command",
        invalidation_reason="stop_command",
    )
    assert clarify_gateway.get_pending_for_session(SESSION_KEY) is None


@pytest.mark.asyncio
async def test_stop_consumes_resume_pending_recovery_state():
    """The next real message after /stop must not replay the restart note."""
    runner = _bare_runner()
    runner._startup_resume_modes = {SESSION_KEY: {"mode": "auto"}}
    runner._resumed_this_boot = {SESSION_KEY}
    # The clear goes through the awaited async_session_store facade (the
    # AST contract in tests/gateway/test_async_session_store.py forbids raw
    # session_store calls inside async gateway code). The facade is a
    # read-only property wrapping session_store, so mock the sync store and
    # let the real boundary route the call off-loop.
    clear_resume_pending = MagicMock(return_value=True)
    runner.session_store.clear_resume_pending = clear_resume_pending

    await runner._interrupt_and_clear_session(
        SESSION_KEY,
        _source(),
        interrupt_reason="stop_command",
        invalidation_reason="stop_command",
    )

    clear_resume_pending.assert_called_once_with(SESSION_KEY)
    assert SESSION_KEY not in runner._startup_resume_modes
    assert SESSION_KEY not in runner._resumed_this_boot


@pytest.mark.asyncio
async def test_stop_keeps_memory_resume_markers_when_durable_clear_fails(caplog):
    runner = _bare_runner()
    startup_marker = {"mode": "auto"}
    runner._startup_resume_modes = {SESSION_KEY: startup_marker}
    runner._resumed_this_boot = {SESSION_KEY}
    runner.session_store.clear_resume_pending = MagicMock(
        side_effect=OSError("state db unavailable")
    )

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        await runner._interrupt_and_clear_session(
            SESSION_KEY,
            _source(),
            interrupt_reason="stop_command",
            invalidation_reason="stop_command",
        )

    assert runner._startup_resume_modes[SESSION_KEY] is startup_marker
    assert SESSION_KEY in runner._resumed_this_boot
    assert "Failed to clear resume-pending state" in caplog.text


@pytest.mark.asyncio
async def test_stop_releases_registered_turn_lease_before_cache_eviction(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("session-1", source="test")
    active = f"pid={os.getpid()}:turn=stopped:platform=discord"
    waiter = f"pid={os.getpid()}:turn=next:platform=discord"
    assert db.try_acquire_session_turn_lease("session-1", active, ttl_seconds=300)

    runner = _bare_runner()
    agent = SimpleNamespace(
        session_id="session-1",
        _session_db=db,
        _active_session_turn_lease_holder=active,
        interrupt=MagicMock(),
    )
    runner._session_state(SESSION_KEY).turn.agent = agent

    await runner._interrupt_and_clear_session(
        SESSION_KEY,
        _source(),
        interrupt_reason="stop_command",
        invalidation_reason="stop_command",
    )

    assert db.try_acquire_session_turn_lease("session-1", waiter, ttl_seconds=300)
    # Keep the stale agent attribute as the late-flush ownership fence.
    assert agent._active_session_turn_lease_holder == active
    db.release_session_turn_lease("session-1", waiter)
    db.close()


@pytest.mark.asyncio
async def test_stop_clears_clarify_even_when_release_state_false():
    """The clarify cancel runs before the release_running_state branch."""
    clarify_gateway.register(
        clarify_id="stopclr0002",
        session_key=SESSION_KEY,
        question="pick one",
        choices=["a", "b"],
    )
    runner = _bare_runner()
    await runner._interrupt_and_clear_session(
        SESSION_KEY,
        _source(),
        interrupt_reason="new_command",
        invalidation_reason="new_command",
        release_running_state=False,
    )
    assert not clarify_gateway.has_pending(SESSION_KEY)
