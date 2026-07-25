"""Regression: an interrupt-and-clear must not leave a clarify prompt armed.

A turn parked in ``clarify_gateway.wait_for_response`` is blocked on a
``threading.Event``, so the cooperative ``agent.interrupt()`` issued by
``/stop`` (and ``/new``) cannot reach it and the turn's ``finally`` — the only
place that cancelled the clarify — never runs at interrupt time.  The entry
therefore survives the stop, and the user's NEXT message is intercepted by the
gateway's clarify hook and routed into the already-invalidated turn, whose
reply is suppressed as stale.  Net effect: the message is silently swallowed
and the user has to send it twice.

``_interrupt_and_clear_session`` is the shared chokepoint for every such path,
so the cancellation belongs there.
"""

from unittest.mock import MagicMock

import pytest

from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.platforms.base import Platform
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
async def test_interrupt_cancels_pending_clarify_and_unblocks_the_waiter():
    """The user-visible contract: after a stop, no clarify can intercept.

    Asserts both halves of the symptom — the entry is gone (so the next
    inbound message reaches the agent normally instead of being swallowed),
    and the parked agent thread is released rather than waiting out the full
    clarify timeout.
    """
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

    # The next inbound message cannot be intercepted as an answer to the
    # dead turn — this is the swallowed-message symptom.
    assert clarify_gateway.get_pending_for_session(SESSION_KEY) is None
    assert not clarify_gateway.has_pending(SESSION_KEY)
    # The blocked agent thread was released via the cancellation sentinel.
    assert entry.event.is_set()


@pytest.mark.asyncio
async def test_multi_choice_clarify_is_cancelled_too():
    """A choice-prompt clarify is armed on the same interception path.

    ``get_pending_for_session(include_choice_prompts=True)`` is what the
    gateway consults when the user types instead of tapping a choice, so a
    surviving choice entry swallows the next message just as an open-ended
    one does.
    """
    clarify_gateway.register(
        clarify_id="stopclr0002",
        session_key=SESSION_KEY,
        question="pick one",
        choices=["a", "b"],
    )
    assert clarify_gateway.get_pending_for_session(
        SESSION_KEY, include_choice_prompts=True
    ) is not None

    runner = _bare_runner()
    await runner._interrupt_and_clear_session(
        SESSION_KEY,
        _source(),
        interrupt_reason="stop_command",
        invalidation_reason="stop_command",
    )

    assert clarify_gateway.get_pending_for_session(
        SESSION_KEY, include_choice_prompts=True
    ) is None


@pytest.mark.asyncio
async def test_clarify_cancelled_on_the_release_state_false_path():
    """Same contract on the sibling call path.

    ``_interrupt_and_clear_session`` is also invoked with
    ``release_running_state=False``; the cancellation must not be tucked
    inside that branch, or the bug stays reachable through it.
    """
    clarify_gateway.register(
        clarify_id="stopclr0003",
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


@pytest.mark.asyncio
async def test_no_pending_clarify_is_a_clean_noop():
    """The common case (nothing pending) must not raise or misbehave."""
    runner = _bare_runner()
    await runner._interrupt_and_clear_session(
        SESSION_KEY,
        _source(),
        interrupt_reason="stop_command",
        invalidation_reason="stop_command",
    )
    assert clarify_gateway.get_pending_for_session(SESSION_KEY) is None
    assert not clarify_gateway.has_pending(SESSION_KEY)


@pytest.mark.asyncio
async def test_other_sessions_are_untouched():
    """Cancellation is scoped to the interrupted session only.

    A gateway serves many concurrent sessions; stopping one must not cancel
    another's in-flight question.
    """
    other_key = "agent:main:discord:thread:99999:99999"
    clarify_gateway.clear_session(other_key)
    try:
        clarify_gateway.register(
            clarify_id="stopclr0004",
            session_key=SESSION_KEY,
            question="mine",
            choices=[],
        )
        other = clarify_gateway.register(
            clarify_id="stopclr0005",
            session_key=other_key,
            question="theirs",
            choices=[],
        )

        runner = _bare_runner()
        await runner._interrupt_and_clear_session(
            SESSION_KEY,
            _source(),
            interrupt_reason="stop_command",
            invalidation_reason="stop_command",
        )

        assert not clarify_gateway.has_pending(SESSION_KEY)
        assert clarify_gateway.has_pending(other_key)
        assert not other.event.is_set()
    finally:
        clarify_gateway.clear_session(other_key)
