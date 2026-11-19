"""The Telegram adapter records who sent each inbound message (t_118d31b1).

hermes-home's Amazon chat-buy gate approves a Telegram "place it" only from the gateway's OWN record of the
inbound update in Ace's DM: the sender id from the update, and no forward origin (a forward reports the
ORIGINAL author, Prism 3bc50b229c84). These tests pin that the record is built from the message object,
travels on the event, is marked when batching merges two messages, and reaches the persisted user row.
"""

import ast
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import inbound_record
from gateway.config import PlatformConfig
from tests.gateway.conftest import _ensure_telegram_mock  # noqa: E402

_ensure_telegram_mock()
sys.modules.pop("plugins.platforms.telegram.adapter", None)
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
WHEN = datetime(2026, 11, 18, 23, 0, 5, tzinfo=timezone.utc)


def _msg(text="place it", *, message_id=4242, user_id=571820863, **extra):
    chat = SimpleNamespace(id=571820863, type="private", title=None, full_name="Ace")
    return SimpleNamespace(chat=chat, from_user=SimpleNamespace(id=user_id, full_name="Ace"), text=text,
                           message_thread_id=None, is_topic_message=False, message_id=message_id,
                           reply_to_message=None, date=WHEN, forum_topic_created=None, **extra)


def _event(**kw):
    from gateway.platforms.event import MessageType
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***", extra={}))
    return adapter, adapter._build_message_event(_msg(**kw), MessageType.TEXT)


def test_a_typed_dm_message_carries_its_own_sender_and_no_forward():
    _, ev = _event()
    assert ev.metadata[inbound_record.KEY] == {
        "platform": "telegram", "from_id": "571820863", "chat_id": "571820863", "message_id": "4242",
        "date": WHEN.timestamp(), "text": "place it", "forwarded": False, "edited": False}
    assert inbound_record.display_metadata(ev) == {inbound_record.KEY: ev.metadata[inbound_record.KEY]}


@pytest.mark.parametrize("field,value", [("forward_origin", SimpleNamespace(type="user")),
                                         ("is_automatic_forward", True)])
def test_a_forwarded_message_is_recorded_as_forwarded(field, value):
    _, ev = _event(**{field: value})
    assert ev.metadata[inbound_record.KEY]["forwarded"] is True


def test_an_edited_message_is_recorded_as_edited():
    _, ev = _event(edit_date=WHEN)
    assert ev.metadata[inbound_record.KEY]["edited"] is True


def test_internal_events_never_carry_a_record():
    _, ev = _event()
    ev.internal = True
    assert inbound_record.display_metadata(ev) == {}


@pytest.mark.asyncio
async def test_text_batching_marks_a_merged_record():
    adapter, first = _event(text="place")
    second = adapter._build_message_event(_msg("it", message_id=4243), first.message_type)
    adapter._enqueue_text_event(first)
    adapter._enqueue_text_event(second)
    try:
        (pending,) = adapter._pending_text_batches.values()
        assert pending.metadata[inbound_record.KEY]["merged"] is True
        assert pending.metadata[inbound_record.KEY]["message_id"] == "4242"
    finally:
        for task in adapter._pending_text_batch_tasks.values():
            task.cancel()
        await asyncio.sleep(0)


def test_media_batching_marks_a_merged_record():
    adapter, first = _event(text="yes")
    second = adapter._build_message_event(_msg("photo 2", message_id=4243), first.message_type)
    pending = {}
    adapter._merge_into_pending(pending, "k", first)
    assert "merged" not in pending["k"].metadata[inbound_record.KEY]
    adapter._merge_into_pending(pending, "k", second)
    assert pending["k"].metadata[inbound_record.KEY]["merged"] is True


def _rec(ev):
    return ev.metadata[inbound_record.KEY]


@pytest.mark.parametrize("kind", ["text", "photo"])
def test_busy_pending_merge_marks_a_merged_record(kind):
    """Prism 7fb14e749eb2: the busy fast-path ``merge_pending_message_event`` absorbs text and photos."""
    from gateway.platforms.base import merge_pending_message_event
    from gateway.platforms.event import MessageType
    adapter, first = _event(text="place it")
    second = adapter._build_message_event(_msg("fwd", message_id=4243), MessageType.TEXT)
    if kind == "photo":
        for ev in (first, second):
            ev.message_type, ev.media_urls, ev.media_types = MessageType.PHOTO, ["/tmp/x.jpg"], ["image/jpeg"]
    pending = {}
    merge_pending_message_event(pending, "k", first, merge_text=True)
    assert "merged" not in _rec(pending["k"])
    merge_pending_message_event(pending, "k", second, merge_text=True)
    assert pending["k"] is first and _rec(first)["merged"] is True


@pytest.mark.asyncio
async def test_busy_text_debounce_marks_a_merged_record():
    """Prism 7fb14e749eb2 repro: "place it" (4242) then a forward (4243) inside the debounce window."""
    adapter, first = _event(text="place it")
    second = adapter._build_message_event(_msg("fwd", message_id=4243, forward_origin=SimpleNamespace(type="user")),
                                          first.message_type)
    try:
        await adapter._queue_text_debounce("k", first)
        await adapter._queue_text_debounce("k", second)
        state = adapter._text_debounce_store()["k"]
        assert state.event.message_id == "4243"
        assert inbound_record.display_metadata(state.event)[inbound_record.KEY]["merged"] is True
    finally:
        adapter._discard_text_debounce("k")
        await asyncio.sleep(0)


def test_a_record_naming_another_message_id_reads_as_merged():
    """Backstop at the single reader: an event whose message_id moved off the recorded one is not that message."""
    _, ev = _event()
    ev.message_id = "4243"
    assert inbound_record.display_metadata(ev)[inbound_record.KEY]["merged"] is True
    assert "merged" not in _rec(ev)


def test_queue_merge_of_user_items_marks_the_head_and_leaves_the_items():
    from gateway.queue_mgmt import merge_user_events
    adapter, first = _event(text="place it")
    second = adapter._build_message_event(_msg("fwd", message_id=4243), first.message_type)
    head = merge_user_events([first, second])
    assert _rec(head)["merged"] is True
    assert "merged" not in _rec(first) and "merged" not in _rec(second)
    assert "merged" not in _rec(merge_user_events([first]))


def test_every_persisted_user_row_spreads_the_record():
    """Both ``_run_agent`` calls in run_turn.py and the transient-failure transcript entry put
    ``inbound_record.display_metadata(...)`` into the user row's display_metadata."""
    tree = ast.parse((REPO / "gateway" / "run_turn.py").read_text(encoding="utf-8"))

    def spreads_record(d):
        return isinstance(d, ast.Dict) and any(
            k is None and isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
            and v.func.attr == "display_metadata" and getattr(v.func.value, "id", "") == "inbound_record"
            for k, v in zip(d.keys, d.values))

    sites = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_run_agent":
            meta = next((k.value for k in node.keywords if k.arg == "persist_user_display_metadata"), None)
            if meta is not None:
                sites.append(spreads_record(meta.values[0] if isinstance(meta, ast.BoolOp) else meta))
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Subscript) and getattr(t.slice, "value", None) == "display_metadata"
                for t in node.targets):
            sites.append(spreads_record(node.value))
    assert len(sites) >= 3 and all(sites), sites
