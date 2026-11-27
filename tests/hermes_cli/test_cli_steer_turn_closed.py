"""CLI ``/steer`` when the agent's steering is closed queues the text as the next turn (t_2b16f0e6).

``finalize_turn`` closes steering at the turn's last drain, and the next turn opens it again at the
start of ``run_conversation``. The CLI still has ``_agent_running`` set on both sides of that gap:
after the last drain until ``chat()`` returns, and from turn two on between ``_agent_running=True``
and the open. A ``/steer`` there is refused by ``steer()``; the CLI used to print
"Steer rejected (empty payload)." and drop the text. It now queues it on ``_pending_input`` the way
the TUI busy path does. Probe from the independent read of #575, adapted.
"""
from __future__ import annotations

import queue

import pytest

import cli as cli_mod
from tests.agent.test_steer_turn_closed import _SteerableStub, finished_agent
from hermes_cli.cli_loops_mixin import CLILoopsMixin


class _FakeCLI:
    _cmd_steer = CLILoopsMixin._cmd_steer

    def __init__(self, agent):
        self.agent = agent
        self._agent_running = True  # still inside chat(): the agent is not marked idle yet
        self._pending_input = queue.Queue()


@pytest.fixture
def printed(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(cli_mod, "_cprint", lambda s="", *a, **k: lines.append(str(s)))
    return lines


def test_cli_steer_after_the_last_drain_is_queued_for_the_next_turn(printed):
    agent = finished_agent()  # the real finalize_turn has closed steering
    c = _FakeCLI(agent)
    c._cmd_steer("/steer use the other file")

    assert not c._pending_input.empty(), f"CLI /steer text dropped; CLI printed {printed!r}"
    assert c._pending_input.get_nowait() == "use the other file"
    assert agent._pending_steer is None
    assert not any("empty payload" in line for line in printed), printed
    assert any("Queued for the next turn" in line for line in printed), printed


def test_cli_steer_mid_turn_still_goes_into_the_running_turn(printed):
    agent = _SteerableStub()  # steering open: a turn is running
    c = _FakeCLI(agent)
    c._cmd_steer("/steer use the other file")

    assert c._pending_input.empty()
    assert agent._drain_pending_steer() == "use the other file"
    assert any("Steer queued" in line for line in printed), printed
