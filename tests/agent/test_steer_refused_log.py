"""``steer()`` says why it refused (card t_1eb0a176, Step 0): one INFO line, never the text. The
agent's own line covers every surface (gateway, CLI, TUI, API, kanban). Behaviour is unchanged."""
from __future__ import annotations

import logging
import threading

import pytest

from agent.interrupt_control import InterruptControlMixin, late_result_nudge


class _Agent:
    steer = InterruptControlMixin.steer

    def __init__(self):
        self._pending_steer = None
        self._pending_steer_lock = threading.Lock()
        self.session_id = "20261127_s1"


@pytest.fixture(autouse=True)
def _info(caplog):
    caplog.set_level(logging.INFO, logger="run_agent")


def _refusals(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("steer_refused ")]


def test_empty_text_is_refused_with_a_reason(caplog):
    agent = _Agent()
    assert agent.steer("   ") is False and agent.steer("") is False
    assert _refusals(caplog) == ["steer_refused reason=empty session=20261127_s1"] * 2
    assert agent._pending_steer is None


def test_a_closed_turn_refuses_with_a_reason_and_no_text(caplog):
    agent = _Agent()
    agent._steer_closed = True
    assert agent.steer("  the launch code  ") is False
    assert _refusals(caplog) == ["steer_refused reason=turn_closed session=20261127_s1 len=15"]
    assert agent._pending_steer is None


def test_accepted_steers_and_the_late_nudge_write_no_refusal(caplog):
    agent = _Agent()
    assert agent.steer("one") is True and agent.steer("two") is True
    agent._steer_closed = True
    with late_result_nudge():
        assert agent.steer("late result") is True
    assert agent._pending_steer == "one\ntwo\nlate result"
    assert _refusals(caplog) == []
