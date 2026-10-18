"""A message delivered by mid-turn /steer must not be replayed by restart backfill.

Incident 2026-10-18 05:49 (#hypercube): Ace sent one message while Apollo was mid-turn. The busy
handler steered it into the running turn (a ``display_kind='steer'`` user row) and Apollo answered
it. The steer row is persisted WITHOUT ``platform_message_id``: the inbound id lives only in the
origin header ``_steer_text_with_origin`` prepends. At 05:54 the gateway restarted; the Discord
backfill asked the transcript authority whether message 1561360611428802601 was present, got
"absent", re-injected it, and Apollo answered it a second time ("That's the same message twice").

These tests drive the REAL producer (``GatewayRunner._steer_text_with_origin``), the REAL steer row
shape (``steer_user_row``), a REAL SessionDB, and the REAL backfill sweep.
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gateway"))

from test_restart_backfill import (  # noqa: E402
    _FakeAuthor, _FakeChannel, _build_adapter_with_store, _make_msg, _seed_session_for_channel, _tmp_home,  # noqa: F401
)


def _steer_payload(msg_id: int, chat_id: int, text: str) -> str:
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    runner = GatewayRunner(config=GatewayConfig())
    source = SessionSource(platform=Platform.DISCORD, chat_id=str(chat_id), chat_type="group",
                           user_id="11111", message_id=str(msg_id))
    event = MessageEvent(text=text, source=source, message_id=str(msg_id))
    return runner._steer_text_with_origin(text, event)


def _wire_client(adapter, channel):
    adapter._restart_recovery.mark_channel_active(str(channel.id))
    adapter._client = SimpleNamespace(
        user=_FakeAuthor(99999, "bot", bot=True),
        get_channel=lambda _id: channel,
        fetch_channel=AsyncMock(return_value=channel),
    )


@pytest.mark.asyncio
async def test_mid_turn_steered_message_is_not_replayed_by_backfill(_tmp_home):
    from agent.prompt_builder import steer_user_row

    adapter, store, db = _build_adapter_with_store(_tmp_home)
    author = _FakeAuthor(11111)
    channel = _FakeChannel(987600, [])
    msg = _make_msg(1561360611428802601, author, channel, content="the hotspot keeps dropping")
    channel._messages = [msg]
    session_id = _seed_session_for_channel(adapter, store, db, channel, author)

    row = steer_user_row(_steer_payload(msg.id, channel.id, "the hotspot keeps dropping"))
    db.append_message(session_id=session_id, role="user", content=row["content"],
                      display_kind=row["display_kind"])
    assert db.has_platform_message_id(session_id, str(msg.id)) is False  # the gap: no column id

    assert store.has_platform_message_id_answerable(session_id, str(msg.id)) == (True, True)
    _wire_client(adapter, channel)
    await adapter._backfill_missed_messages()
    assert adapter._dispatched == [], "a steer-delivered message must not be re-injected after restart"


@pytest.mark.asyncio
async def test_leftover_steer_turn_is_not_replayed_by_backfill(_tmp_home):
    """A leftover steer runs as the next user turn with the origin header as its persisted text."""
    adapter, store, db = _build_adapter_with_store(_tmp_home)
    author = _FakeAuthor(11111)
    channel = _FakeChannel(987601, [])
    msg = _make_msg(70601, author, channel, content="late message")
    channel._messages = [msg]
    session_id = _seed_session_for_channel(adapter, store, db, channel, author)

    db.append_message(session_id=session_id, role="user",
                      content=_steer_payload(msg.id, channel.id, "late message"))
    _wire_client(adapter, channel)
    await adapter._backfill_missed_messages()
    assert adapter._dispatched == []


@pytest.mark.asyncio
async def test_unrelated_steer_and_quoted_id_do_not_mask_a_missed_message(_tmp_home):
    """Controls: a steer for a DIFFERENT message, an id that is a prefix of it, and an id quoted
    in ordinary user text (no origin header) all leave the missed message recoverable."""
    from agent.prompt_builder import steer_user_row

    adapter, store, db = _build_adapter_with_store(_tmp_home)
    author = _FakeAuthor(11111)
    channel = _FakeChannel(987602, [])
    missed = _make_msg(70602, author, channel, content="missed while down")
    channel._messages = [missed]
    session_id = _seed_session_for_channel(adapter, store, db, channel, author)

    other = steer_user_row(_steer_payload(7060299, channel.id, "a different steer"))
    db.append_message(session_id=session_id, role="user", content=other["content"],
                      display_kind=other["display_kind"])
    db.append_message(session_id=session_id, role="user",
                      content='look at {"message_id": "70602"} in this log')
    db.append_message(session_id=session_id, role="assistant",
                      content=_steer_payload(missed.id, channel.id, "echoed by the model"))

    assert store.has_platform_message_id_answerable(session_id, "70602") == (True, False)
    _wire_client(adapter, channel)
    await adapter._backfill_missed_messages()
    assert [m.id for m in adapter._dispatched] == [70602]


def test_redacted_steer_origin_counts_as_present(tmp_path, monkeypatch):
    """privacy.redact_pii hashes message_id in the header on non-Discord platforms."""
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from test_has_platform_message_id_answerable import _make_store

    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text("privacy:\n  redact_pii: true\n", encoding="utf-8")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="555", chat_type="group", user_id="7")
    payload = GatewayRunner(config=GatewayConfig())._steer_text_with_origin(
        "hi", MessageEvent(text="hi", source=source, message_id="8812"))
    assert '"8812"' not in payload  # the raw id is really absent from the row

    store = _make_store(tmp_path)
    store._db.create_session(session_id="s1", source="telegram")
    store._db.append_message(session_id="s1", role="user", content=payload)
    assert store.has_platform_message_id_answerable("s1", "8812") == (True, True)
    assert store.has_platform_message_id_answerable("s1", "8813") == (True, False)


def test_origin_header_constant_matches_the_producer():
    from hermes_state_messages import STEER_ORIGIN_HEADER

    assert _steer_payload(1, 2, "x").splitlines()[0] == STEER_ORIGIN_HEADER
