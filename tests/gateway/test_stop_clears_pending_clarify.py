"""/stop and /new must cancel a pending clarify prompt immediately.

A turn blocked in ``clarify_gateway.wait_for_response`` does not observe the
hard interrupt (the wait polls only its own event), so the clarify entry stays
registered until the turn's ``finally`` runs — which the parked thread never
reaches. The user's NEXT message is then intercepted by ``_hm_clarify_reply``
("Gateway intercepted clarify text response") and handed to the dead turn,
whose output the invalidated generation suppresses: the message is silently
swallowed and the user has to send it again.

``_interrupt_and_clear_session`` therefore clears the session's clarify
entries itself, which also wakes the parked thread (empty-string sentinel).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource, build_session_key
from tools import clarify_gateway


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    runner.adapters = {Platform.TELEGRAM: SimpleNamespace(send=AsyncMock())}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._invalidate_session_run_generation = lambda *a, **kw: None
    runner._release_running_agent_state = lambda *a, **kw: None
    return runner


@pytest.fixture
def session_key():
    key = build_session_key(_make_source())
    clarify_gateway.clear_session(key)
    yield key
    clarify_gateway.clear_session(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("release_running_state", [True, False])
async def test_interrupt_cancels_pending_clarify(session_key, release_running_state):
    entry = clarify_gateway.register(
        clarify_id=f"stopclr-{release_running_state}",
        session_key=session_key,
        question="rotate now or defer?",
        choices=[],
    )
    assert clarify_gateway.get_pending_for_session(session_key) is not None

    await _make_runner()._interrupt_and_clear_session(
        session_key,
        _make_source(),
        interrupt_reason="user_stop",
        invalidation_reason="stop_command",
        release_running_state=release_running_state,
    )

    # The next inbound message can no longer be routed to the dead turn ...
    assert clarify_gateway.get_pending_for_session(
        session_key, include_choice_prompts=True
    ) is None
    # ... and the thread parked in wait_for_response was woken.
    assert entry.event.is_set()


@pytest.mark.asyncio
async def test_interrupt_without_pending_clarify_is_noop(session_key):
    await _make_runner()._interrupt_and_clear_session(
        session_key,
        _make_source(),
        interrupt_reason="user_stop",
        invalidation_reason="stop_command",
    )
    assert clarify_gateway.get_pending_for_session(session_key) is None
