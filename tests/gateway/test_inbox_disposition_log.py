"""Step 0 logging (card t_1eb0a176): one ``inbox_disposition`` line per person message. Logging only:
the last tests prove the touched paths behave the same with the lines switched off, or raising.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from gateway import inbox_log
from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from tests.gateway.test_steer_order_behind_queue import (  # noqa: F401  (rig is a fixture)
    SK, _busy, _document, _kanban_wake, _photo, _text, rig,
)

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _info(caplog):
    caplog.set_level(logging.INFO, logger="gateway.inbox_log")


def _lines(caplog, needle="inbox_disposition"):
    return [r.getMessage() for r in caplog.records if r.name == "gateway.inbox_log" and needle in r.getMessage()]


def _fields(line):
    return dict(part.split("=", 1) for part in line.split()[1:])


@pytest.mark.asyncio
async def test_steered_line(rig, caplog):
    runner, _adapter, agent, *_ = rig
    await _busy(runner, _text("turn left", "1001"), "steer", agent)
    assert len(agent.steered) == 1
    (line,) = _lines(caplog)
    assert _fields(line) == {
        "disposition": "steered", "session": SK, "platform": "discord", "msg_id": "1001", "depth": "-",
        "path": "steer"}
    assert "turn left" not in line   # ids only, never message text


@pytest.mark.asyncio
async def test_queued_then_held_lines(rig, caplog):
    runner, adapter, agent, *_ = rig
    await _busy(runner, _document("2001"), "steer", agent)     # a file is not steerable: queued
    await _busy(runner, _text("and this", "2002"), "steer", agent)   # held behind it, then queued
    got = [(f["disposition"], f["msg_id"], f["depth"], f["path"]) for f in map(_fields, _lines(caplog))]
    assert got == [("queued", "2001", "1", "fifo"), ("held", "2002", "-", "steer_behind_queued"),
                   ("queued", "2002", "2", "fifo")]
    assert agent.steered == [] and adapter._pending_messages[SK].message_id == "2001"


def test_merged_into_head_line(rig, caplog):
    runner, adapter, _agent, _stt, _vision, shots = rig
    runner._queue_or_replace_pending_event(SK, _photo(shots[0], "3001"))
    runner._queue_or_replace_pending_event(SK, _photo(shots[1], "3002"))
    got = [(f["disposition"], f["msg_id"], f["depth"], f["path"]) for f in map(_fields, _lines(caplog))]
    assert got == [("queued", "3001", "1", "fifo"), ("merged-into-head", "3002", "1", "photo_burst")]
    assert len(adapter._pending_messages[SK].media_urls) == 2


def test_refused_lines_at_cap_and_without_an_adapter(rig, caplog):
    runner, adapter, *_ = rig
    cap = runner._BUSY_QUEUE_MAX_PENDING
    for i in range(cap):
        runner._queue_or_replace_pending_event(SK, _text(f"m{i}", str(4000 + i)))
    caplog.clear()
    runner._queue_or_replace_pending_event(SK, _text("one too many", "4999"))
    (line,) = _lines(caplog)
    assert _fields(line)["disposition"] == "refused" and _fields(line)["path"] == "cap"
    assert _fields(line)["msg_id"] == "4999" and _fields(line)["depth"] == str(cap)
    assert runner._queue_depth(SK, adapter=adapter) == cap
    caplog.clear()
    runner.adapters = {}
    runner._queue_or_replace_pending_event(SK, _text("nobody home", "5000"))
    assert [(_fields(x)["disposition"], _fields(x)["path"]) for x in _lines(caplog)] == [("refused", "no_adapter")]


def test_internal_wakes_write_no_line(rig, caplog):
    runner, *_ = rig
    runner._queue_or_replace_pending_event(SK, _kanban_wake())
    assert _lines(caplog) == []


def test_became_next_turn_line_and_its_two_call_sites(caplog):
    inbox_log.log_disposition("became-next-turn", SK, _text("hello", "6001"), path="turn_start")
    (line,) = _lines(caplog)
    assert _fields(line) == {
        "disposition": "became-next-turn", "session": SK, "platform": "discord", "msg_id": "6001",
        "depth": "-", "path": "turn_start"}
    # Both turn starts (a fresh inbound turn, a queued follow-up) log it right before the turn runs.
    src = (REPO / "gateway" / "run_turn.py").read_text(encoding="utf-8-sig")
    for path in ("turn_start", "followup"):
        at = src.index(f'path="{path}"')
        assert "await self._run_agent(" in src[at:at + 260], path


def test_every_adapter_slot_write_has_its_line():
    """The adapter's own queue writes (default path, photo burst, text debounce) are checked in the
    source, not driven: each direct write of the pending slot is followed by its disposition line."""
    src = (REPO / "gateway" / "platforms" / "base.py").read_text(encoding="utf-8-sig")
    sites = [i for i in range(len(src)) if src.startswith("merge_pending_message_event(self._pending_messages", i)]
    assert len(sites) == 4
    for at in sites:
        line = src[at:at + 420]
        assert "inbox_log.log_disposition(" in line or "inbox_log.log_slot_write(" in line, src[at:at + 120]


@pytest.mark.asyncio
async def test_interrupt_text_that_is_not_queued_logs_interrupt_requested(rig, caplog):
    """Reader F8: the interrupt path only hands the text to the running turn; the turn runner may later
    re-queue it as a new event (depth cap) whose own line says ``became-next-turn``. So this line says
    ``interrupt-requested``, and one message never gets two ``became-next-turn`` lines."""
    runner, adapter, agent, *_ = rig
    agent._supports_active_turn_redirect = False
    await runner._hm_busy_interrupt(_text("stop, do this instead", "9001"), _text("x", "0").source, agent, SK)
    assert agent.interrupted == ["stop, do this instead"] and SK not in adapter._pending_messages
    got = [(f["disposition"], f["msg_id"], f["path"]) for f in map(_fields, _lines(caplog))]
    assert got == [("interrupt-requested", "9001", "interrupt_text")]
    assert not [x for x in _lines(caplog) if "became-next-turn" in x]
    # With something already queued the event is queued instead: one line, "queued".
    caplog.clear()
    runner._queue_or_replace_pending_event(SK, _document("9002"))
    await runner._hm_busy_interrupt(_text("and this", "9003"), _text("x", "0").source, agent, SK)
    got = [(f["disposition"], f["msg_id"]) for f in map(_fields, _lines(caplog))]
    assert got == [("queued", "9002"), ("queued", "9003")]


def test_an_odd_event_never_raises(caplog):
    inbox_log.log_disposition("queued", "k", object())
    assert _fields(_lines(caplog)[0])["msg_id"] == "-"


async def _scenario(rig):
    runner, adapter, agent, _stt, _vision, shots = rig
    await _busy(runner, _text("first", "8001"), "steer", agent)
    await _busy(runner, _document("8002"), "steer", agent)
    await _busy(runner, _text("behind the file", "8003"), "steer", agent)
    runner._queue_or_replace_pending_event(SK, _kanban_wake())
    for i in range(runner._BUSY_QUEUE_MAX_PENDING):
        runner._queue_or_replace_pending_event(SK, _text(f"m{i}", str(8100 + i)))
    overflow = runner._session_state(SK).conversation.queued_events
    return (list(agent.steered), adapter._pending_messages[SK].message_id,
            [(e.message_id, e.text, bool(e.internal)) for e in overflow])


@pytest.mark.asyncio
async def test_behaviour_is_identical_with_the_lines_switched_off(rig, monkeypatch, tmp_path):
    """The same busy sequence, with and without the Step 0 lines: same steers, same queue, same order."""
    with_lines = await _scenario(rig)
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from tests.gateway.test_steer_order_behind_queue import _Adapter, _Agent
    monkeypatch.setattr(inbox_log, "log_disposition", lambda *a, **k: None)
    runner = GatewayRunner(config=GatewayConfig())
    adapter, agent = _Adapter(), _Agent()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._session_state(SK).turn.agent = agent
    without_lines = await _scenario((runner, adapter, agent, None, None, rig[5]))
    assert with_lines == without_lines
    assert with_lines[0] and with_lines[1] == "8002" and len(with_lines[2]) == runner._BUSY_QUEUE_MAX_PENDING - 1



@pytest.mark.asyncio
async def test_a_log_helper_that_raises_changes_no_outcome(rig, monkeypatch, caplog):
    """The helper's logger raises on every call: every message ends where it would anyway."""
    with_lines = await _scenario(rig)
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from tests.gateway.test_steer_order_behind_queue import _Adapter, _Agent

    class _Broken:
        def info(self, *_a, **_k):
            raise RuntimeError("log handler down")

    monkeypatch.setattr(inbox_log, "logger", _Broken())
    runner = GatewayRunner(config=GatewayConfig())
    adapter, agent = _Adapter(), _Agent()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._session_state(SK).turn.agent = agent
    assert await _scenario((runner, adapter, agent, None, None, rig[5])) == with_lines
    # A depth read that raises inside the line is dropped with the line.
    inbox_log.log_disposition("queued", SK, _text("x", "1"), depth=lambda: 1 // 0)


# --- Prism round fixes ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_refused_priority_interrupt_is_not_logged_as_the_next_turn(rig, caplog):
    """Prism fc5652a0476b: queue at cap, redirect off, a new interrupt: refused, never became-next-turn."""
    runner, adapter, agent, *_ = rig
    agent._supports_active_turn_redirect = False
    for i in range(runner._BUSY_QUEUE_MAX_PENDING):
        runner._queue_or_replace_pending_event(SK, _text(f"m{i}", str(4000 + i)))
    caplog.clear()
    await runner._hm_busy_interrupt(_text("now this", "5000"), _text("x", "0").source, agent, SK)
    got = [(f["disposition"], f["msg_id"], f["path"]) for f in map(_fields, _lines(caplog))]
    assert got == [("refused", "5000", "cap")]


@pytest.mark.asyncio
async def test_a_successful_explicit_steer_command_is_logged(rig, caplog):
    """Prism b03344443cd2: ``/steer <text>`` into a live turn writes ``steered path=steer_command``."""
    runner, _adapter, agent, *_ = rig
    event = _text("/steer change course", "120")
    await runner._busy_steer_command(event, SK, event.source)
    assert len(agent.steered) == 1
    got = [(f["disposition"], f["msg_id"], f["path"]) for f in map(_fields, _lines(caplog))]
    assert got == [("steered", "120", "steer_command")]
    assert "change course" not in " ".join(_lines(caplog))


@pytest.mark.asyncio
async def test_a_drain_refusal_at_the_adapter_guard_is_logged(rig, monkeypatch, caplog):
    """Prism e49d4ed0642d: the busy guard's own drain branch rejects the message and logs it."""
    runner, *_ = rig
    sent = []

    async def _reply(event, adapter, content, **_k):
        sent.append(content)

    monkeypatch.setattr(runner, "_queue_during_drain_enabled", lambda *_a: False)
    monkeypatch.setattr(runner, "_send_busy_reply", _reply)
    await runner._send_busy_drain_notice(_text("late", "130"), SK, "steer")
    assert len(sent) == 1
    got = [(f["disposition"], f["msg_id"], f["path"]) for f in map(_fields, _lines(caplog))]
    assert got == [("refused", "130", "drain")]


def test_a_slot_write_that_replaces_the_head_says_replaced_head(rig, caplog):
    """Reader F4 / Prism e4a6812b9358: a photo whose download failed (no media) followed by text is
    REPLACED in the pending slot, not merged. The line says ``replaced-head`` and names the lost head."""
    runner, adapter, _agent, _stt, _vision, shots = rig
    src = _text("x", "0").source
    runner._hm_merge_pending_for_source(src, SK, MessageEvent(
        text="", message_type=MessageType.PHOTO, source=src, message_id="7001"))
    runner._hm_merge_pending_for_source(src, SK, _text("words", "7002"))
    assert adapter._pending_messages[SK].message_id == "7002"          # 7001 is gone
    runner._hm_merge_pending_for_source(src, SK, _photo(shots[0], "7003"))
    assert adapter._pending_messages[SK].message_id == "7002"          # a photo with media merges in
    got = [(f["disposition"], f["msg_id"], f["path"], f.get("into")) for f in map(_fields, _lines(caplog))]
    assert got == [("queued", "7001", "pending_slot", None), ("replaced-head", "7002", "pending_slot", "7001"),
                   ("merged-into-head", "7003", "pending_slot", None)]


@pytest.mark.asyncio
async def test_a_leftover_steer_requeued_as_the_next_turn_is_logged(rig, caplog):
    """Reader F6: steered text the turn never read is put back as the next turn's input: one
    ``requeued-leftover-steer`` line, ``msg_id=-``; an internal leftover writes none."""
    runner, adapter, *_ = rig
    src = _text("x", "0").source
    # A follow-up owns the next turn: the leftover goes on the overflow queue behind it.
    runner._queue_or_replace_pending_event(SK, _document("7101"))
    caplog.clear()
    runner._queue_leftover_steer("and also check the logs", src, SK, display_metadata={})
    runner._queue_leftover_steer("wake text", src, SK, display_metadata={}, internal=True)
    assert len(runner._session_state(SK).conversation.queued_events) == 2
    (line,) = _lines(caplog)
    assert _fields(line) == {"disposition": "requeued-leftover-steer", "session": SK, "platform": "discord",
                             "msg_id": "-", "depth": "2", "path": "leftover_steer"}
    assert "check the logs" not in line
    # Nothing pending: the drain makes the leftover itself the next turn, with the same line.
    adapter._pending_messages.clear()
    runner._session_state(SK).conversation.queued_events.clear()
    caplog.clear()
    adapter.get_pending_message = lambda key: adapter._pending_messages.pop(key, None)
    _event, pending = await runner._run_agent_drain_pending({"pending_steer": "turn right"}, adapter, src, SK)
    assert pending == "turn right"
    assert [(_fields(x)["disposition"], _fields(x)["msg_id"], _fields(x)["path"]) for x in _lines(caplog)] == [
        ("requeued-leftover-steer", "-", "leftover_steer")]
