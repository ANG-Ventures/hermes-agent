"""Post-turn hooks (the /goal judge) run AFTER the reply is sent, not between ready and send (t_3acac80f).

Real ``GatewayRunner._handle_message`` + real ``BasePlatformAdapter`` via the flush-window rig.
"""

import asyncio
import logging
import time

import pytest

from gateway.turn_lease import TurnLeaseTimeoutError
from tests.gateway.test_turn_lease_flush_window import KEY, _msg, _Rig, _wait_for


def _timed_sends(rig):
    stamps = []
    real_send = rig.adapter.send

    async def send(chat_id, content, reply_to=None, metadata=None):
        if "answer " in str(content):  # the turn's reply, not the home-channel notice
            stamps.append(time.monotonic())
        return await real_send(chat_id, content, reply_to=reply_to, metadata=metadata)

    rig.adapter.send = send
    return stamps


@pytest.mark.asyncio
async def test_reply_is_sent_before_the_goal_judge_starts(monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path)
    sends = _timed_sends(rig)
    judge = {}

    async def slow_judge(**_kw):
        judge["start"] = time.monotonic()
        await asyncio.sleep(3.0)  # a judge round trip under load
        judge["end"] = time.monotonic()

    rig.runner._run_post_turn_hooks = slow_judge
    await rig.adapter.handle_message(_msg("first"))
    await _wait_for(lambda: "end" in judge and sends, "judge and reply", timeout=10)
    assert sends[0] < judge["start"], "reply waited on the /goal judge"


@pytest.mark.asyncio
async def test_continuation_queued_after_delivery_runs_as_next_turn(monkeypatch, tmp_path):
    """The hook now runs after the turn task released the session, so its FIFO enqueue must wake it."""
    rig = _Rig(monkeypatch, tmp_path)
    runner = rig.runner
    fired = []

    async def judge_then_enqueue(*, source, event=None, **_kw):
        if not fired:
            fired.append(1)
            runner._enqueue_fifo(KEY, runner._synthetic_prompt_event(source, "continue", internal=True), rig.adapter)

    runner._run_post_turn_hooks = judge_then_enqueue
    await rig.adapter.handle_message(_msg("first"))
    await _wait_for(lambda: len(rig.calls) >= 2, "continuation turn", timeout=10)
    assert [c.split("\n\n")[0] for c in rig.calls] == ["first", "continue"]


@pytest.mark.asyncio
async def test_shutdown_drain_lets_in_flight_hook_queue_its_continuation_once(monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path)
    runner = rig.runner
    started = asyncio.Event()

    async def judge_then_enqueue(*, source, **_kw):
        started.set()
        await asyncio.sleep(0.3)
        runner._enqueue_fifo(KEY, runner._synthetic_prompt_event(source, "continue", internal=True), rig.adapter)

    runner._run_post_turn_hooks = judge_then_enqueue
    await rig.adapter.handle_message(_msg("first"))
    await asyncio.wait_for(started.wait(), 5)
    runner._draining = True  # stop() sets this before the drain
    await asyncio.wait_for(runner._drain_post_turn_tasks(10.0), 5)
    queued = [rig.adapter._pending_messages.get(KEY)] + list(runner._overflow_queue(KEY) or [])
    assert [e.text for e in queued if e is not None] == ["continue"]
    assert not runner._post_turn_tasks


@pytest.mark.asyncio
async def test_shutdown_drain_is_bounded_and_logs(monkeypatch, tmp_path, caplog):
    rig = _Rig(monkeypatch, tmp_path)
    caplog.set_level(logging.WARNING)
    started = asyncio.Event()

    async def stuck(**_kw):
        started.set()
        await asyncio.sleep(60)

    rig.runner._run_post_turn_hooks = stuck
    await rig.adapter.handle_message(_msg("first"))
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.wait_for(rig.runner._drain_post_turn_tasks(0.2), 5)
    assert "post-turn hook task(s) still running" in caplog.text
    for t in list(rig.runner._post_turn_tasks):
        t.cancel()


@pytest.mark.asyncio
async def test_lease_rejection_returns_before_any_hook(monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path)
    calls = []

    async def hooks(**_kw):
        calls.append(1)

    async def reject(*_a, **_kw):
        raise TurnLeaseTimeoutError("sid", owner_key=KEY, generation=1, wait_seconds=0.1)

    rig.runner._run_post_turn_hooks = hooks
    rig.runner._handle_message_with_agent = reject
    rig.runner._hm_lease_rejection_reply = lambda *_a: "resend"
    assert await rig.runner._handle_message(_msg("first")) == "resend"
    await asyncio.sleep(0.05)
    assert calls == []
    assert not getattr(rig.runner, "_post_turn_tasks", None)
