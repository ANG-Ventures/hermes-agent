"""A steer that lands after the turn's last steer collection becomes the next turn (t_2b16f0e6).

The finalizer drains ``_pending_steer`` once (``turn_finalizer``), but the gateway keeps the agent
as the session's running agent until ``run_conversation`` has returned and the slot is released.
A ``steer()`` in that window was accepted, so the gateway acked it as "steered" and did not queue
it, yet nothing drained it again: it waited in the finished agent's buffer and reached some later
turn after its first tool batch (out of order), or was lost with the agent.

Fix: ``run_conversation`` closes steering when the turn returns and hands anything still buffered
back as ``result["pending_steer"]``; ``steer()`` refuses while closed, so the gateway queues the
message as the next turn. The next ``run_conversation`` reopens steering.
"""
from __future__ import annotations

import pytest

import agent.conversation_loop as loop
from tests.agent.test_steer import _bare_agent


@pytest.fixture
def bare_turn(monkeypatch):
    """Run ``run_conversation`` around a scripted turn body (no model, no session store)."""
    monkeypatch.setattr(loop, "_close_durable_failed_turn", lambda agent, result: None)
    import agent.turn_context as tc
    monkeypatch.setattr(tc, "export_current_turn_boundary", lambda agent, result, user_message: result)
    import agent.fallback_cause as fc
    monkeypatch.setattr(fc, "flush_banner", lambda agent: None)

    def run(agent, body):
        monkeypatch.setattr(loop, "_run_conversation_turn", lambda a, m, **kw: body(a))
        return loop.run_conversation(agent, "hello")

    return run


def _finalizer_then_late_steer(late_text):
    def body(agent):
        result = {"final_response": "done", "messages": []}
        leftover = agent._drain_pending_steer()          # the finalizer's one drain
        if leftover:
            result["pending_steer"] = leftover
        agent.steer(late_text)                            # arrives after that drain
        return result
    return body


def test_steer_after_the_final_drain_is_handed_back_as_the_next_turn(bare_turn):
    agent = _bare_agent()
    result = bare_turn(agent, _finalizer_then_late_steer("late message"))
    assert result.get("pending_steer") == "late message", (
        "a steer accepted after the finalizer's drain was neither in the turn nor handed back"
    )
    assert agent._pending_steer is None


def test_steer_is_refused_once_the_turn_has_returned(bare_turn):
    agent = _bare_agent()
    bare_turn(agent, _finalizer_then_late_steer("late message"))
    assert agent.steer("after the turn returned") is False
    assert agent._pending_steer is None


def test_late_steer_joins_an_earlier_leftover_in_arrival_order(bare_turn):
    agent = _bare_agent()
    agent.steer("first")                                  # pending when the finalizer drains
    result = bare_turn(agent, _finalizer_then_late_steer("second"))
    assert result.get("pending_steer") == "first\nsecond"


def test_next_turn_reopens_steering(bare_turn):
    agent = _bare_agent()
    bare_turn(agent, _finalizer_then_late_steer("late"))
    seen = {}

    def body(a):
        seen["accepted"] = a.steer("mid next turn")
        return {"final_response": "ok", "messages": []}

    bare_turn(agent, body)
    assert seen["accepted"] is True


def test_steer_mid_turn_is_unchanged(bare_turn):
    """Control for ordinary steers: accepted while the turn runs, drained by the turn."""
    agent = _bare_agent()
    seen = {}

    def body(a):
        seen["accepted"] = a.steer("mid turn")
        seen["drained"] = a._drain_pending_steer()
        return {"final_response": "ok", "messages": []}

    result = bare_turn(agent, body)
    assert seen == {"accepted": True, "drained": "mid turn"}
    assert "pending_steer" not in result


def test_explicit_open_after_close_accepts_again():
    agent = _bare_agent()
    agent._close_steer()
    assert agent.steer("refused") is False
    agent._open_steer()
    assert agent.steer("accepted") is True
