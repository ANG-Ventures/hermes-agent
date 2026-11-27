"""An operator note whose steer is refused is read again on the next poll (t_63bd0dcf).

Since PR 575, ``steer()`` returns False while a turn is closed. A kanban worker's turn 2+ polls
for comments from ``lease.start()`` (``agent/turn_facade.py``) before
``conversation_loop.run_conversation`` reopens steering, so that poll's steer is refused. The
watermark used to advance before the steer, so the note was never delivered in this process.
Now a refused (or raising) steer leaves the watermark where it was, and the next poll, after the
throttle and with the turn open, delivers the note once.

These drive the real ``finalize_turn`` (through ``finished_agent``), the real steer buffer, the
real kanban board and the real ``inject_new_comments_from_env``. The first test is red on 4c2f964d.
"""

from __future__ import annotations

import time

import tools.kanban_tools as kt
from tests.agent.test_steer_turn_closed import finished_agent
from tests.tools.test_kanban_comment_injection import (  # noqa: F401  (worker_home is a fixture)
    FakeAgent,
    _comment,
    _live_worker,
    _unthrottle,
    worker_home,
)


def test_note_refused_at_the_next_turn_start_is_delivered_once_the_turn_opens(worker_home, monkeypatch):
    tid, _ = _live_worker(monkeypatch, "turn start")  # seeds the watermark
    seeded = kt._comment_watermark[tid]
    agent = finished_agent()  # turn N went through the real finalizer: steering is closed
    _comment(tid, "apollo", "stop, use branch B")

    # Turn N+1's lease.start() poll, before run_conversation reopens steering.
    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is False
    assert kt._comment_watermark[tid] == seeded, "refused note consumed: the watermark moved past it"
    assert agent._pending_steer is None

    # Within the throttle the next poll reads nothing.
    assert kt.inject_new_comments_from_env(agent) is False

    # run_conversation opens steering; the first mid-turn poll after the throttle delivers it.
    agent._open_steer()
    kt._comment_poll_last_attempt = time.monotonic() - kt._COMMENT_POLL_MIN_INTERVAL_SECONDS - 1
    assert kt.inject_new_comments_from_env(agent) is True
    drained = agent._drain_pending_steer()
    assert drained and "branch B" in drained and drained.count("branch B") == 1
    assert kt._comment_watermark[tid] > seeded

    # A third poll delivers nothing.
    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is False
    assert agent._drain_pending_steer() is None


def test_a_raising_steer_also_leaves_the_note_for_the_next_poll(worker_home, monkeypatch):
    tid, _ = _live_worker(monkeypatch, "raising steer")
    seeded = kt._comment_watermark[tid]
    _comment(tid, "apollo", "use the v2 API")

    class Raises:
        def steer(self, text):
            raise RuntimeError("boom")

    _unthrottle()
    assert kt.inject_new_comments_from_env(Raises()) is False  # never raises
    assert kt._comment_watermark[tid] == seeded

    agent = FakeAgent()
    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is True
    assert len(agent.steers) == 1 and "v2 API" in agent.steers[0]


def test_an_accepted_steer_advances_the_watermark_with_no_duplicate(worker_home, monkeypatch):
    tid, agent = _live_worker(monkeypatch, "accepted")
    seeded = kt._comment_watermark[tid]
    _comment(tid, "apollo", "keep the rest unchanged")
    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is True
    assert kt._comment_watermark[tid] > seeded
    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is False
    assert len(agent.steers) == 1


def test_own_comments_only_still_advance_the_watermark(worker_home, monkeypatch):
    tid, agent = _live_worker(monkeypatch, "own only")
    seeded = kt._comment_watermark[tid]
    _comment(tid, "worker-bot", "i did a thing")
    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is False
    assert agent.steers == []
    assert kt._comment_watermark[tid] > seeded
