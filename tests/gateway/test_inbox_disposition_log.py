"""Step 0 logging (card t_1eb0a176): one ``inbox_disposition`` line per person message, and
``inbox_reorder_detected``. Logging only: the last tests prove the touched paths behave the same
with the lines switched off.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import inbox_log
from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from tests.gateway.test_steer_inbound_record import _agent as _real_agent, _steer_row
from tests.gateway.test_steer_order_behind_queue import (  # noqa: F401  (rig is a fixture)
    SK, _busy, _document, _kanban_wake, _photo, _text, rig,
)

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _fresh_tracker(monkeypatch, caplog):
    monkeypatch.setattr(inbox_log, "_TRACKER", inbox_log._ReorderTracker())
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
        "path": "steer", "held_release": "False", "reorder_check": "ordered"}
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
        "depth": "-", "path": "turn_start", "held_release": "False", "reorder_check": "ordered"}
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
        assert "inbox_log.log_disposition(" in src[at:at + 420], src[at:at + 120]


@pytest.mark.asyncio
async def test_interrupt_text_that_is_not_queued_logs_became_next_turn(rig, caplog):
    runner, adapter, agent, *_ = rig
    agent._supports_active_turn_redirect = False
    await runner._hm_busy_interrupt(_text("stop, do this instead", "9001"), _text("x", "0").source, agent, SK)
    assert agent.interrupted == ["stop, do this instead"] and SK not in adapter._pending_messages
    got = [(f["disposition"], f["msg_id"], f["path"]) for f in map(_fields, _lines(caplog))]
    assert got == [("became-next-turn", "9001", "interrupt_text")]
    # With something already queued the event is queued instead: one line, "queued".
    caplog.clear()
    runner._queue_or_replace_pending_event(SK, _document("9002"))
    await runner._hm_busy_interrupt(_text("and this", "9003"), _text("x", "0").source, agent, SK)
    got = [(f["disposition"], f["msg_id"]) for f in map(_fields, _lines(caplog))]
    assert got == [("queued", "9002"), ("queued", "9003")]


def test_ids_that_cannot_be_ordered_are_skipped(caplog):
    source = SessionSource(platform=Platform.MATRIX, chat_id="!room", chat_type="dm", user_id="ace")
    event = MessageEvent(text="hi", source=source, message_id="$abc:server", message_type=MessageType.TEXT)
    inbox_log.log_disposition("steered", "k", event)
    inbox_log._TRACKER.wait_idle()
    assert _fields(_lines(caplog)[0])["reorder_check"] == "skipped"
    assert _lines(caplog, "inbox_reorder_detected") == []


def test_without_a_session_store_the_check_starts_no_thread(caplog):
    inbox_log.log_disposition("steered", SK, _text("a", "3000"))
    inbox_log.log_disposition("steered", SK, _text("b", "2500"))
    assert inbox_log._TRACKER._pool is None
    (warn,) = _lines(caplog, "inbox_reorder_detected")
    assert _fields(warn)["msg_id"] == "2500" and _fields(warn)["high_water_source"] == "memory"


def test_an_odd_event_never_raises(caplog):
    inbox_log.log_disposition("queued", "k", object())
    assert _fields(_lines(caplog)[0])["msg_id"] == "-"


def _store(tmp_path, rows):
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
                 "platform_message_id TEXT)")
    conn.executemany("INSERT INTO messages (session_id, role, content, platform_message_id) VALUES (?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return SimpleNamespace(session_store=SimpleNamespace(_db=SimpleNamespace(db_path=path)))


def test_reorder_detected_from_the_session_store_after_a_restart(tmp_path, caplog):
    runner = _store(tmp_path, [("s1", "user", "plain", "2000"), ("s1", "assistant", "x", None),
                               ("s2", "user", "other session", "9000")])
    released = _text("sent two days ago", "1500")
    released._queue_released = True
    # A new process has no memory of the session: the mark comes from the store.
    inbox_log.log_disposition("became-next-turn", SK, released, path="followup", runner=runner, session_id="s1")
    inbox_log._TRACKER.wait_idle()
    (warn,) = _lines(caplog, "inbox_reorder_detected")
    assert _fields(warn) == {"session": SK, "platform": "discord", "msg_id": "1500", "high_water": "2000",
                             "held_release": "True", "high_water_source": "store"}
    assert [r.levelno for r in caplog.records if "inbox_reorder_detected" in r.getMessage()] == [logging.WARNING]
    # Later, in the same process: 3000 then 2500 is a reorder that is not a held release.
    caplog.clear()
    inbox_log.log_disposition("steered", SK, _text("a", "3000"), runner=runner, session_id="s1")
    inbox_log.log_disposition("steered", SK, _text("b", "2500"), runner=runner, session_id="s1")
    inbox_log._TRACKER.wait_idle()
    (warn,) = _lines(caplog, "inbox_reorder_detected")
    assert _fields(warn)["high_water"] == "3000" and _fields(warn)["held_release"] == "False"
    assert _fields(warn)["high_water_source"] == "memory"


def test_the_store_seed_reads_a_steered_rows_id_from_its_origin_header(tmp_path):
    import contextlib

    from gateway.run_busy import GatewayBusySessionMixin
    source = SessionSource(platform=Platform.DISCORD, chat_id="chat-1", chat_type="dm", user_id="ace")
    event = MessageEvent(text="x", source=source, message_id="2600", message_type=MessageType.TEXT)
    fake = SimpleNamespace(_profile_scope_for_source=lambda _s: contextlib.nullcontext())
    steered_text = GatewayBusySessionMixin._steer_text_with_origin(fake, "look at this", event)
    runner = _store(tmp_path, [("s1", "user", "old", "2000"), ("s1", "user", steered_text, None)])
    assert inbox_log.seed_high_water(str(runner.session_store._db.db_path), "s1") == 2600
    assert inbox_log.seed_high_water(str(tmp_path / "missing.db"), "s1") is None
    assert inbox_log.seed_high_water(None, "s1") is None


@pytest.mark.asyncio
async def test_a_steered_row_carries_its_platform_message_id(caplog):
    """Item 4: the id reaches the row the session store persists (its existing column)."""
    from tests.gateway.test_steer_inbound_record import _runner
    runner, agent = _runner(), _real_agent()
    event = _text("mid-turn note", "7001")
    outcome = await runner._resolve_busy_steer_or_redirect(event, "key", "steer", agent)
    assert outcome.steered
    assert _steer_row(agent)["platform_message_id"] == "7001"
    # Two messages drained into one row: no single id describes it.
    for mid in ("7002", "7003"):
        await runner._resolve_busy_steer_or_redirect(_text(f"note {mid}", mid), "key", "steer", agent)
    assert "platform_message_id" not in _steer_row(agent)


def test_a_steer_row_outside_the_gateway_is_unchanged():
    from agent.prompt_builder import steer_user_row
    agent = _real_agent()
    assert set(steer_user_row("cli steer", agent=agent)) == {"role", "content", "display_kind"}
    assert set(steer_user_row("no agent", agent=None)) == {"role", "content", "display_kind"}


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
