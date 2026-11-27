"""A message that reaches a turn after it has closed steering becomes the next turn (t_2b16f0e6).

``finalize_turn`` drains the steer buffer and closes steering in one lock acquisition; the finished
agent still owns the busy slot until ``run_conversation`` returns. A steer in that window is refused:
- a plain busy follow-up is "unsteerable" and takes main's existing queue path (Prism ed7e5f5d5936
  shape for text);
- an explicit ``/steer <text>`` is queued with "Queued for the next turn." instead of the old
  "Steer rejected (empty payload)" (the text was lost);
- ``/steer /<command>`` is refused by the existing queue guard, never queued to run as a command
  at dequeue (Prism 028df965e393).
The first three tests use an agent that went through the real ``finalize_turn`` and are red on main.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import build_session_key
from tests.agent.test_steer_turn_closed import _SteerableStub, finished_agent
from tests.gateway.test_steer_command import _make_event, _make_runner, _make_source, _session_entry


@pytest.mark.asyncio
async def test_steer_command_after_the_turn_closed_is_queued_as_the_next_turn():
    runner, adapter = _make_runner(_session_entry())
    sk = build_session_key(_make_source())
    runner._running_agents[sk] = finished_agent()

    reply = await runner._handle_message(_make_event("/steer use the revised requirements"))

    assert sk in adapter._pending_messages, f"the /steer text was not queued; reply={reply!r}"
    assert adapter._pending_messages[sk].text == "use the revised requirements"
    assert reply == "Queued for the next turn.", reply


@pytest.mark.asyncio
async def test_steer_command_carrying_a_slash_command_after_the_turn_closed_is_refused():
    runner, adapter = _make_runner(_session_entry())
    sk = build_session_key(_make_source())
    runner._running_agents[sk] = finished_agent()

    reply = await runner._handle_message(_make_event("/steer /new"))

    assert sk not in adapter._pending_messages, "a slash command was queued to run at dequeue"
    assert "new" in (reply or "") and "Queued for the next turn" not in (reply or ""), reply


@pytest.mark.asyncio
async def test_busy_text_after_the_turn_closed_is_not_steered():
    """The plain busy path: a refused steer is "unsteerable", so the caller queues it (main's path)
    and acks "Queued for the next turn" instead of "Steered into current run"."""
    runner, _adapter = _make_runner(_session_entry())
    sk = build_session_key(_make_source())
    agent = finished_agent()
    runner._running_agents[sk] = agent
    event = MessageEvent(text="arrived after the last drain", message_type=MessageType.TEXT,
                         source=_make_source(), message_id="m2")

    outcome = await runner._steer_busy_followup(event, sk, agent)

    assert outcome == "unsteerable", outcome
    assert agent._pending_steer is None


@pytest.mark.asyncio
async def test_busy_text_mid_turn_is_still_steered():
    """Control: an ordinary mid-turn text steer is unchanged."""
    runner, _adapter = _make_runner(_session_entry())
    sk = build_session_key(_make_source())
    agent = _SteerableStub()
    runner._running_agents[sk] = agent
    event = MessageEvent(text="mid-turn note", message_type=MessageType.TEXT,
                         source=_make_source(), message_id="m2")

    outcome = await runner._steer_busy_followup(event, sk, agent)

    assert outcome == "steered", outcome
    assert "mid-turn note" in (agent._pending_steer or "")


@pytest.mark.asyncio
async def test_empty_steer_still_gets_the_usage_line():
    """Control: an empty /steer is still answered with usage and queues nothing."""
    runner, adapter = _make_runner(_session_entry())
    sk = build_session_key(_make_source())
    runner._running_agents[sk] = MagicMock()
    reply = await runner._handle_message(_make_event("/steer"))
    assert sk not in adapter._pending_messages
    assert "usage" in (reply or "").lower()
