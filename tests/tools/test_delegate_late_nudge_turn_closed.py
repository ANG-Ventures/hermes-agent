"""The delegate late-result nudge still reaches the parent's next turn once steering is closed.

A child that hit ``child_timeout`` and finishes later steers a "[delegate_task late result]" note
into its parent. That is normally after the parent's turn has returned, and since t_2b16f0e6 the
turn's last drain closes steering, so a plain ``steer()`` is refused. On main the note waited in the
parent's buffer and the parent's next turn drained it; this caller alone keeps that delivery
(``late_result_nudge``). Any other ``steer()`` into a closed agent is still refused.
"""
from __future__ import annotations

import threading

from agent.interrupt_control import InterruptControlMixin
from tests.tools.test_delegate_late_completion import (  # noqa: F401
    _Agent,
    _clean_registry,
    _late_results,
    _wait_until,
    fleet_home,
)


class _ClosableParent(_Agent):
    """The late-completion stub parent with the real steer buffer and close/open methods."""

    steer = InterruptControlMixin.steer
    _drain_pending_steer = InterruptControlMixin._drain_pending_steer
    _drain_pending_steer_and_close = InterruptControlMixin._drain_pending_steer_and_close
    _open_steer = InterruptControlMixin._open_steer

    def __init__(self):
        super().__init__(None, depth=0)
        self._pending_steer_lock = threading.Lock()


def test_late_result_nudge_after_the_parent_turn_returned_reaches_its_next_turn(
    fleet_home, monkeypatch
):
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.3)
    release = threading.Event()

    def _slow(self):
        release.wait(10)
        return {"final_response": "the late answer", "completed": True, "api_calls": 3}

    parent = _ClosableParent()
    child = _Agent("sa-0-nudge", parent=parent, api_calls=2, behavior=_slow)

    entry = delegate_tool._run_single_child(0, "late goal", child, parent)
    assert entry["status"] == delegate_tool.TIMED_OUT_RUNNING
    parent._drain_pending_steer_and_close()  # the parent's turn returns: finalize_turn's last drain
    assert parent.steer("a plain steer into the finished parent") is False
    release.set()

    assert _wait_until(lambda: _late_results(parent)), "late result not recorded"
    assert _wait_until(lambda: parent._pending_steer, timeout=5.0), (
        "late-result nudge refused by the closed parent; its next turn never sees it"
    )
    assert parent.steer("another plain steer") is False, "the bypass leaked past the nudge"

    parent._open_steer()  # the next turn starts (run_conversation)
    nxt = parent._drain_pending_steer()  # its first drain (prepare_iteration's pre-API drain)
    assert nxt and nxt.startswith("[delegate_task late result] subagent sa-0-nudge"), nxt
    assert "the late answer" in nxt
