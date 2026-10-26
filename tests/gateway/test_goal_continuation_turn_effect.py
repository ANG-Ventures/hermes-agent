"""The gateway /goal continuation re-fires only after a turn that changed something.

Ace ruling 2026-10-22 (option b, t_c4ac59ae): on a busy board every kanban wake / page / NO_REPLY
answer was followed by a continuation turn that re-read unchanged state (2-6 min gaps on a goal
armed with a 30-min check-in). A turn the user did not author whose tool calls were all reads (or
none) keeps the judge verdicts but queues no continuation; the idle check-in stays the next nudge.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, build_session_key
from hermes_cli.goal_turn_effects import classify_turn_effect


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    goals._get_session_db()  # warm from a sync context (see test_goal_verdict_send.py)
    yield home
    goals._DB_CACHE.clear()


class _Adapter:
    def __init__(self) -> None:
        self._pending_messages: dict = {}
        self.sends: list = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sends.append(content)
        return SimpleNamespace(success=True, message_id="m")


def _runner(goals_cfg=None):
    from gateway.run import GatewayRunner

    src = SessionSource(platform=Platform.TELEGRAM, user_id="u1", chat_id="c1", user_name="t", chat_type="dm")
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")})
    runner._goals_config = lambda: dict(goals_cfg or {})
    runner.adapters, runner._running_agents, runner._running_agents_ts, runner._queued_events = {}, {}, {}, {}
    entry = SessionEntry(
        session_key=build_session_key(src), session_id=f"te-{uuid.uuid4().hex[:8]}",
        created_at=datetime.now(), updated_at=datetime.now(), platform=Platform.TELEGRAM, chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store._generate_session_key.return_value = build_session_key(src)
    adapter = _Adapter()
    runner.adapters[Platform.TELEGRAM] = adapter
    return runner, adapter, entry, src


def _call(name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}},
    ]}


_WAKE = "[kanban] card t_1234 changed: review requested"
_READ_TURN = [_call("kanban_show", task_id="t_1234"), _call("terminal", command="gh pr view 12 --json state | jq .state")]
_WRITE_TURN = _READ_TURN + [_call("kanban_comment", task_id="t_1234", body="ack")]


def _event(text, messages, *, internal):
    ev = MessageEvent(text=text, message_type=MessageType.TEXT, source=None, internal=internal)
    ev._goal_turn_api_calls = len(messages) + 1
    ev._goal_turn_effect = classify_turn_effect(messages)  # what run_turn stashes
    return ev


async def _run_turn(runner, adapter, entry, src, event, wait=2.0):
    # The enqueue happens inline after the judge hop; ``wait`` only absorbs scheduler jitter.
    with patch("hermes_cli.goals.judge_goal", return_value=("continue", "more to do", False, None, False)):
        await runner._post_turn_goal_continuation(session_entry=entry, source=src, final_response="ok", event=event)
        deadline = time.monotonic() + wait
        while not adapter._pending_messages and time.monotonic() < deadline:
            await asyncio.sleep(0.01)


def _set_goal(sid):
    from hermes_cli.goals import GoalManager

    GoalManager(sid).set("land the queue")
    return GoalManager(sid)


@pytest.mark.asyncio
async def test_read_only_wakes_queue_no_continuation_and_leave_state(hermes_home):
    runner, adapter, entry, src = _runner()
    before = await asyncio.to_thread(lambda: _set_goal(entry.session_id).state)
    for _ in range(10):
        await _run_turn(runner, adapter, entry, src, _event(_WAKE, _READ_TURN, internal=True), wait=0.05)
        await _run_turn(runner, adapter, entry, src, _event(_WAKE, [], internal=True), wait=0.05)  # NO_REPLY
    assert not adapter._pending_messages
    from hermes_cli.goals import load_goal

    after = await asyncio.to_thread(load_goal, entry.session_id)
    assert after.status == "active"
    assert (after.turns_used, after.last_turn_at, after.toolless_turns) == (
        before.turns_used, before.last_turn_at, before.toolless_turns,
    )


@pytest.mark.asyncio
async def test_write_turn_queues_exactly_one_continuation(hermes_home):
    runner, adapter, entry, src = _runner()
    await asyncio.to_thread(_set_goal, entry.session_id)
    await _run_turn(runner, adapter, entry, src, _event(_WAKE, _READ_TURN, internal=True), wait=0.05)
    await _run_turn(runner, adapter, entry, src, _event(_WAKE, _WRITE_TURN, internal=True))
    queued = [e for evs in adapter._pending_messages.values() for e in (evs if isinstance(evs, list) else [evs])]
    assert len(queued) == 1 and queued[0].text.startswith("[Continuing toward your standing goal]")


@pytest.mark.asyncio
async def test_user_message_continues_even_when_read_only(hermes_home):
    runner, adapter, entry, src = _runner()
    await asyncio.to_thread(_set_goal, entry.session_id)
    await _run_turn(runner, adapter, entry, src, _event("how is it going?", [], internal=False))
    assert adapter._pending_messages


@pytest.mark.asyncio
async def test_kill_switch_restores_continuation_after_read_only_turn(hermes_home):
    runner, adapter, entry, src = _runner({"nudge_on_read_only_turns": True})
    await asyncio.to_thread(_set_goal, entry.session_id)
    await _run_turn(runner, adapter, entry, src, _event(_WAKE, _READ_TURN, internal=True))
    assert adapter._pending_messages


def test_idle_checkin_still_fires_after_held_turns(hermes_home):
    mgr = _set_goal("idle-after-held")
    t0 = time.time()
    mgr.state.last_turn_at = t0
    mgr._save()
    with patch("hermes_cli.goals.judge_goal", return_value=("continue", "k", False, None, False)):
        for _ in range(10):
            d = mgr.evaluate_after_turn("ack", real_event=True, turn_effect="read")
            assert not d["should_continue"] and d["status"] == "active"
    prompt = mgr.idle_wake_prompt(t0 + 30 * 60 + 1, checkin_minutes=30)
    assert prompt and "Goal check-in 1/3" in prompt


def test_judge_can_still_end_the_goal_on_a_read_only_turn(hermes_home):
    mgr = _set_goal("done-on-read")
    with patch("hermes_cli.goals.judge_goal", return_value=("done", "merged", False, None, False)):
        d = mgr.evaluate_after_turn("PR merged", turn_effect="read")
    assert d["status"] == "done"


def _queued(adapter):
    return [e for evs in adapter._pending_messages.values() for e in (evs if isinstance(evs, list) else [evs])]


@pytest.mark.asyncio
@pytest.mark.parametrize("goal_cfg, per_goal_checkin", [
    ({"checkin_minutes": 0}, None),   # config: idle check-ins off
    ({}, 0),                          # per-goal /goal checkin 0
])
async def test_held_turn_is_never_the_last_word(hermes_home, goal_cfg, per_goal_checkin):
    """Regression for Prism 527a33ddb4e7: with no idle check-in to follow, a read-only turn (the loop's
    own continuation, or a wake) must still queue a continuation instead of stopping silently."""
    runner, adapter, entry, src = _runner(goal_cfg)

    def _arm():
        mgr = _set_goal(entry.session_id)
        if per_goal_checkin is not None:
            mgr.set_checkin_minutes(per_goal_checkin)
        return mgr.next_continuation_prompt()

    continuation = await asyncio.to_thread(_arm)
    await _run_turn(runner, adapter, entry, src, _event(continuation, _READ_TURN, internal=True))
    assert len(_queued(adapter)) == 1
    adapter._pending_messages.clear()
    await _run_turn(runner, adapter, entry, src, _event(_WAKE, _READ_TURN, internal=True))
    assert len(_queued(adapter)) == 1


def test_spent_checkin_budget_does_not_hold(hermes_home):
    """No real event re-arms the check-ins and the budget is used up: holding would be a silent stop."""
    mgr = _set_goal("spent-budget")
    mgr.state.idle_wakes = 3
    mgr._save()
    with patch("hermes_cli.goals.judge_goal", return_value=("continue", "k", False, None, False)):
        d = mgr.evaluate_after_turn("ack", turn_effect="read", checkin_minutes=30, max_checkins=3)
        assert d["should_continue"] and d["hold_overridden"]
        mgr.state.idle_wakes = 0  # control: a check-in can still follow -> held
        d = mgr.evaluate_after_turn("ack", turn_effect="read", checkin_minutes=30, max_checkins=3)
        assert not d["should_continue"] and d["held"] == "read"


@pytest.mark.asyncio
async def test_loop_own_read_only_continuation_keeps_going_with_checkins_on(hermes_home):
    """The hold targets wakes/pages, not the loop's heartbeat: a read-only continuation turn continues."""
    runner, adapter, entry, src = _runner({"checkin_minutes": 30})
    continuation = await asyncio.to_thread(lambda: _set_goal(entry.session_id).next_continuation_prompt())
    await _run_turn(runner, adapter, entry, src, _event(continuation, _READ_TURN, internal=True))
    assert len(_queued(adapter)) == 1


@pytest.mark.asyncio
async def test_compaction_during_turn_is_unknown_and_not_held(hermes_home):
    """Regression for Prism 3c0c66874847: after mid-turn compaction the last user row (a folded todo
    snapshot) is not the turn start; a write before it must not be lost to a read-only slice."""
    from hermes_cli.goal_turn_effects import turn_effect_for_result

    messages = [
        {"role": "user", "content": _WAKE},
        _call("kanban_comment", task_id="t_1234", body="ack"),
        {"role": "user", "content": "[todo snapshot]"},
        _call("kanban_show", task_id="t_1234"),
    ]
    result = {"messages": messages, "history_offset": 0, "compacted_in_place": True, "compacted_during_turn": True}
    assert turn_effect_for_result(result, messages) is None
    assert turn_effect_for_result({**result, "compacted_during_turn": False}, messages) == "read"

    runner, adapter, entry, src = _runner({"checkin_minutes": 30})
    await asyncio.to_thread(_set_goal, entry.session_id)
    ev = _event(_WAKE, messages, internal=True)
    ev._goal_turn_effect = turn_effect_for_result(result, messages)
    await _run_turn(runner, adapter, entry, src, ev)
    assert len(_queued(adapter)) == 1


@pytest.mark.parametrize("messages, effect", [
    ([], "none"),
    ([_call("read_file", path="x"), _call("terminal", command="git status && git log -3 | cat")], "read"),
    ([_call("terminal", command="gh api repos/o/r/pulls/1 --jq .state")], "read"),
    ([_call("terminal", command="gh api -X PUT repos/o/r/pulls/1/update-branch")], "write"),
    ([_call("terminal", command="git commit -m x && git push")], "write"),
    ([_call("terminal", command="cat a > b")], "write"),
    ([_call("cronjob", action="list")], "read"),
    ([_call("cronjob", action="create")], "write"),
    ([_call("write_file", path="x", content="y")], "write"),
    ([_call("some_plugin_tool")], "write"),  # unknown: never hold a turn that might have acted
])
def test_turn_effect_classifier(messages, effect):
    assert classify_turn_effect(messages) == effect
