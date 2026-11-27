"""The steer ack must not leave a wrong promise behind when the steer becomes the next turn (t_2b16f0e6).

Rig case C1 (live-proof.md): an image sent ~1 s before the turn's last reply was steered and
acked "Steered into current run ... arrives after the next tool call". No tool call followed:
the finalizer handed it back as ``result["pending_steer"]`` and it ran as the next turn. Delivery
was right; the ack said something else. When a leftover steer follows a steer ack in the same
turn, the gateway now says what happened: it is queued as the next turn.
"""
from __future__ import annotations

import pytest

from tests.gateway.test_queued_followup_turn_clock_and_leftover_steer import SESSION_KEY, _drive

NOTICE = "queued as the next turn"


def _leftover_then_answer(idx, queue_followup):
    if idx == 0:
        return {"final_response": "first answer", "messages": [], "api_calls": 1,
                "pending_steer": "late steer text"}
    return {"final_response": f"answer {idx}", "messages": [], "api_calls": 1}


@pytest.mark.asyncio
async def test_leftover_after_a_steer_ack_says_it_is_the_next_turn(monkeypatch, tmp_path):
    def prepare(runner):
        runner._session_state(SESSION_KEY).turn.steer_acked = True

    _runner, adapter, calls, _ = await _drive(monkeypatch, tmp_path, _leftover_then_answer, prepare=prepare)

    assert [c["message"] for c in calls][:2] == ["first", "late steer text"]
    notices = [m for m in adapter.sent if NOTICE in (m or "")]
    assert len(notices) == 1, f"no correction for the 'steered' ack; sent={adapter.sent!r}"


@pytest.mark.asyncio
async def test_leftover_without_a_steer_ack_sends_no_notice(monkeypatch, tmp_path):
    _runner, adapter, calls, _ = await _drive(monkeypatch, tmp_path, _leftover_then_answer)
    assert [c["message"] for c in calls][:2] == ["first", "late steer text"]
    assert not [m for m in adapter.sent if NOTICE in (m or "")]


@pytest.mark.asyncio
async def test_message_after_the_turn_returned_is_queued_with_a_queued_ack(monkeypatch):
    """The window itself, end to end: the turn has returned (finalizer drained, run_conversation
    done) but the session still lists the agent as running. The follow-up must be queued as the
    next turn and acked as queued, not acked "steered" and left in the finished agent."""
    import agent.conversation_loop as loop
    import agent.fallback_cause as fc
    import agent.turn_context as tc
    import gateway.run as _gr
    from gateway.session import build_session_key
    from tests.agent.test_steer import _bare_agent
    from tests.gateway.test_busy_session_ack import _make_adapter, _make_event, _make_runner

    monkeypatch.setattr(loop, "_close_durable_failed_turn", lambda agent, result: None)
    monkeypatch.setattr(tc, "export_current_turn_boundary", lambda agent, result, user_message: result)
    monkeypatch.setattr(fc, "flush_banner", lambda agent: None)
    monkeypatch.setattr(loop, "_run_conversation_turn",
                        lambda a, m, **kw: {"final_response": "done", "messages": []})
    agent = _bare_agent()
    loop.run_conversation(agent, "hello")                 # the turn has returned

    monkeypatch.delenv("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED", raising=False)
    monkeypatch.setattr(_gr, "_load_gateway_config", lambda: {})
    runner, _sentinel = _make_runner()
    runner._busy_input_mode = "steer"
    adapter = _make_adapter()
    event = _make_event(text="sent as the turn closed")
    sk = build_session_key(event.source)
    runner.adapters[event.source.platform] = adapter
    runner._running_agents[sk] = agent                    # slot not yet released

    await runner._handle_active_session_busy_message(event, sk)

    assert agent._pending_steer is None, "the message was steered into a turn that had returned"
    assert adapter._pending_messages.get(sk) is event
    content = adapter._send_with_retry.call_args.kwargs["content"]
    assert "Queued for the next turn" in content and "Steered" not in content
    assert runner._session_state(sk).turn.steer_acked is False


@pytest.mark.asyncio
async def test_a_steered_ack_marks_the_turn(monkeypatch):
    import gateway.run as _gr
    from unittest.mock import MagicMock
    from gateway.session import build_session_key
    from tests.gateway.test_busy_session_ack import _make_adapter, _make_event, _make_runner

    monkeypatch.delenv("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED", raising=False)
    monkeypatch.setattr(_gr, "_load_gateway_config", lambda: {})
    runner, _sentinel = _make_runner()
    runner._busy_input_mode = "steer"
    adapter = _make_adapter()
    event = _make_event(text="mid turn")
    sk = build_session_key(event.source)
    runner.adapters[event.source.platform] = adapter
    agent = MagicMock()
    agent.steer = MagicMock(return_value=True)
    runner._running_agents[sk] = agent

    await runner._handle_active_session_busy_message(event, sk)

    assert "Steered" in adapter._send_with_retry.call_args.kwargs["content"]
    assert runner._session_state(sk).turn.steer_acked is True
