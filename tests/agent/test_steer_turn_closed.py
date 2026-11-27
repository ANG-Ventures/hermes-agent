"""A steer that arrives after the turn's last steer collection is refused, not buffered (t_2b16f0e6).

``finalize_turn`` drains ``_pending_steer`` once, but the gateway keeps the agent as the session's
running agent until ``run_conversation`` has returned and the slot is released. On main a
``steer()`` in that window was accepted: the gateway acked it as "steered" and did not queue it,
yet nothing drained it again, so it waited in the finished agent's buffer and reached a later turn
out of order, or was lost with the agent.

Fix: the finalizer's drain also closes steering, in the same lock acquisition, so a later
``steer()`` returns False and the surface queues the message as the next turn. The next turn
reopens steering. These run the real ``finalize_turn``; the first is red on main.
"""
from __future__ import annotations

import threading

import pytest

import agent.conversation_loop as loop
from agent.interrupt_control import InterruptControlMixin
from tests.agent.test_turn_finalizer_cleanup_guard import _run, _StubAgent


class _SteerableStub(_StubAgent):
    """The finalizer's stub agent with the real steer buffer methods (the rest stays stubbed)."""

    steer = InterruptControlMixin.steer
    _drain_pending_steer = InterruptControlMixin._drain_pending_steer
    # getattr: on main these do not exist, so the red run fails on the behaviour, not at import.
    _drain_pending_steer_and_close = getattr(InterruptControlMixin, "_drain_pending_steer_and_close", None)
    _close_steer = getattr(InterruptControlMixin, "_close_steer", None)
    _open_steer = getattr(InterruptControlMixin, "_open_steer", None)

    def __init__(self):
        super().__init__(raise_in=())
        self._pending_steer = None
        self._pending_steer_lock = threading.Lock()


def finished_agent():
    """An agent whose turn has just been through the real ``finalize_turn``."""
    agent = _SteerableStub()
    result = _run(agent, final_response="done")
    assert "pending_steer" not in result
    return agent


def test_a_steer_after_the_finalizer_drain_is_refused_not_buffered():
    agent = finished_agent()
    assert agent.steer("arrived after the last drain") is False, (
        "accepted into a finished agent: acked as steered, then never read by this turn"
    )
    assert agent._pending_steer is None


def test_a_steer_before_the_finalizer_drain_is_still_handed_back():
    """Main's path, unchanged: a steer the last API call missed becomes result["pending_steer"]."""
    agent = _SteerableStub()
    assert agent.steer("landed during the final API call") is True
    result = _run(agent, final_response="done")
    assert result.get("pending_steer") == "landed during the final API call"
    assert agent._pending_steer is None


def test_the_finalizer_drain_still_goes_through_an_instance_wrapper():
    """The gateway and the delegate ledger wrap ``_drain_pending_steer`` on the instance to keep the
    drained text's provenance; the closing drain must pass through that wrapper (Prism d1e7cd8d823d)."""
    agent = _SteerableStub()
    seen = []
    inner = agent._drain_pending_steer

    def wrapper():
        text = inner()
        seen.append(text)
        return text

    agent._drain_pending_steer = wrapper
    agent.steer("landed during the final API call")
    result = _run(agent, final_response="done")
    assert seen == ["landed during the final API call"]
    assert result.get("pending_steer") == "landed during the final API call"
    assert agent.steer("after the drain") is False


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


def test_next_turn_reopens_steering(bare_turn):
    agent = finished_agent()
    seen = {}

    def body(a):
        seen["accepted"] = a.steer("mid next turn")
        return {"final_response": "ok", "messages": []}

    bare_turn(agent, body)
    assert seen["accepted"] is True


def test_steer_mid_turn_is_unchanged(bare_turn):
    """Control for ordinary steers: accepted while the turn runs, drained by the turn."""
    agent = _SteerableStub()
    seen = {}

    def body(a):
        seen["accepted"] = a.steer("mid turn")
        seen["drained"] = a._drain_pending_steer()
        return {"final_response": "ok", "messages": []}

    result = bare_turn(agent, body)
    assert seen == {"accepted": True, "drained": "mid turn"}
    assert "pending_steer" not in result


def test_an_exit_that_skips_the_finalizer_closes_without_draining(bare_turn):
    """A turn body that raises never reaches finalize_turn: steering still closes, and text
    already buffered stays for the next turn's first drain (main's behaviour)."""
    agent = _SteerableStub()

    def body(a):
        a.steer("buffered before the raise")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        bare_turn(agent, body)
    assert agent.steer("after the raise") is False
    assert agent._pending_steer == "buffered before the raise"


def test_explicit_open_after_close_accepts_again():
    agent = _SteerableStub()
    agent._close_steer()
    assert agent.steer("refused") is False
    agent._open_steer()
    assert agent.steer("accepted") is True
