"""An explicit /steer refused because the turn already returned must become the next turn
(t_2b16f0e6, Prism ed7e5f5d5936).

``steer()`` now returns False once ``run_conversation`` has closed steering, while the finished
agent still owns the busy slot. The /steer handler answered "Steer rejected (empty payload)" and
queued nothing, so a non-empty prompt was lost.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gateway.session import build_session_key
from tests.gateway.test_steer_command import _make_event, _make_runner, _make_source, _session_entry


@pytest.mark.asyncio
async def test_steer_refused_by_a_closed_turn_is_queued_as_the_next_turn():
    runner, adapter = _make_runner(_session_entry())
    sk = build_session_key(_make_source())
    closed_agent = MagicMock()
    closed_agent.steer.return_value = False   # the turn returned: steering is closed
    runner._running_agents[sk] = closed_agent

    reply = await runner._handle_message(_make_event("/steer use the revised requirements"))

    assert sk in adapter._pending_messages, f"the /steer text was dropped; reply={reply!r}"
    assert adapter._pending_messages[sk].text == "use the revised requirements"
    assert "queued" in (reply or "").lower() and "empty" not in (reply or "").lower(), reply


@pytest.mark.asyncio
async def test_empty_steer_still_gets_the_usage_line():
    """Control: an empty /steer is still answered with usage and queues nothing."""
    runner, adapter = _make_runner(_session_entry())
    sk = build_session_key(_make_source())
    runner._running_agents[sk] = MagicMock()
    reply = await runner._handle_message(_make_event("/steer"))
    assert sk not in adapter._pending_messages
    assert "usage" in (reply or "").lower()
