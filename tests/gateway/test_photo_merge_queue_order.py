"""A queued photo must not absorb a later message ahead of messages queued in between (t_3ee12123).

``_queue_or_replace_pending_event`` merges a photo burst into the pending HEAD slot (album
semantics: a caption sent right after its photo joins the photo's turn). When other messages
already wait in the overflow queue behind that head, the merge moved the later message ahead of
them: head = photo, overflow = [document], then a text -> the text ran with the photo, before the
document. Owner's requirement (Discord 1575444644152614992): everything arrives in the order sent.
"""
from __future__ import annotations

import pytest

from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import build_session_key
from tests.gateway.test_steer_command import _make_runner, _make_source, _session_entry


def _ev(text, mtype, mid, media=None):
    return MessageEvent(
        text=text, source=_make_source(), message_id=mid, message_type=mtype,
        media_urls=list(media or []), media_types=["image/png"] * len(media or []),
    )


def _setup():
    runner, adapter = _make_runner(_session_entry())
    if not hasattr(runner, "_queued_events"):
        runner._queued_events = {}
    return runner, adapter, build_session_key(_make_source())


def _order(runner, adapter, sk):
    head = adapter._pending_messages.get(sk)
    return [head] + list(runner._queued_events.get(sk) or [])


def test_text_after_queued_photo_waits_behind_messages_queued_in_between():
    runner, adapter, sk = _setup()
    photo = _ev("", MessageType.PHOTO, "p1", ["/tmp/p1.png"])
    doc = _ev("", MessageType.DOCUMENT, "d1")
    text = _ev("T2 after the document", MessageType.TEXT, "t2")
    runner._queue_or_replace_pending_event(sk, photo)
    runner._queue_or_replace_pending_event(sk, doc)
    runner._queue_or_replace_pending_event(sk, text)

    queued = _order(runner, adapter, sk)
    assert [e.message_id for e in queued] == ["p1", "d1", "t2"], (
        "the text joined the photo's turn and overtook the document queued before it"
    )
    assert "T2 after the document" not in (queued[0].text or "")


def test_photo_after_queued_text_does_not_jump_messages_queued_in_between():
    runner, adapter, sk = _setup()
    first = _ev("T1", MessageType.TEXT, "t1")
    doc = _ev("", MessageType.DOCUMENT, "d1")
    photo = _ev("", MessageType.PHOTO, "p2", ["/tmp/p2.png"])
    runner._queue_or_replace_pending_event(sk, first)
    runner._queue_or_replace_pending_event(sk, doc)
    runner._queue_or_replace_pending_event(sk, photo)

    queued = _order(runner, adapter, sk)
    assert queued[0].message_id == "t1" and not (queued[0].media_urls or [])
    assert [e.message_id for e in queued] == ["t1", "d1", "p2"]


def test_caption_right_after_its_photo_still_merges():
    """Control: nothing queued between them, so the caption joins its photo (album semantics)."""
    runner, adapter, sk = _setup()
    photo = _ev("", MessageType.PHOTO, "p1", ["/tmp/p1.png"])
    caption = _ev("what is this?", MessageType.TEXT, "t1")
    runner._queue_or_replace_pending_event(sk, photo)
    runner._queue_or_replace_pending_event(sk, caption)

    queued = _order(runner, adapter, sk)
    assert len(queued) == 1
    assert queued[0].media_urls == ["/tmp/p1.png"]
    assert "what is this?" in queued[0].text


def test_second_photo_right_after_the_first_still_merges():
    runner, adapter, sk = _setup()
    runner._queue_or_replace_pending_event(sk, _ev("", MessageType.PHOTO, "p1", ["/tmp/p1.png"]))
    runner._queue_or_replace_pending_event(sk, _ev("", MessageType.PHOTO, "p2", ["/tmp/p2.png"]))

    queued = _order(runner, adapter, sk)
    assert len(queued) == 1
    assert queued[0].media_urls == ["/tmp/p1.png", "/tmp/p2.png"]


def test_at_the_queue_cap_a_photo_still_merges_rather_than_being_dropped():
    """Prism bad37f463754: with the queue full, a new slot is refused, so the photo merges into the
    head (as before this change) instead of being dropped."""
    runner, adapter, sk = _setup()
    runner._queue_or_replace_pending_event(sk, _ev("", MessageType.PHOTO, "p1", ["/tmp/p1.png"]))
    for i in range(runner._BUSY_QUEUE_MAX_PENDING - 1):
        runner._queue_or_replace_pending_event(sk, _ev("", MessageType.DOCUMENT, f"d{i}"))
    assert runner._queue_depth(sk, adapter=adapter) == runner._BUSY_QUEUE_MAX_PENDING
    runner._queue_or_replace_pending_event(sk, _ev("", MessageType.PHOTO, "p2", ["/tmp/p2.png"]))
    assert adapter._pending_messages[sk].media_urls == ["/tmp/p1.png", "/tmp/p2.png"]


def test_at_the_queue_cap_a_photo_without_media_does_not_replace_the_head():
    """Prism bad37f463754 (round 2): a PHOTO whose download failed has no media; the helper would
    REPLACE a text head instead of merging, so the cap fallback must not take it."""
    runner, adapter, sk = _setup()
    runner._queue_or_replace_pending_event(sk, _ev("t1", MessageType.TEXT, "t1"))
    for i in range(runner._BUSY_QUEUE_MAX_PENDING - 1):
        runner._queue_or_replace_pending_event(sk, _ev("", MessageType.DOCUMENT, f"d{i}"))
    assert runner._queue_depth(sk, adapter=adapter) == runner._BUSY_QUEUE_MAX_PENDING
    runner._queue_or_replace_pending_event(sk, _ev("please check this", MessageType.PHOTO, "p2"))
    head = adapter._pending_messages[sk]
    assert head.message_id == "t1" and head.text == "t1", "the accepted text head was replaced"


def test_photo_without_media_after_a_queued_text_takes_its_own_turn():
    """Same helper limit with nothing else queued: the media-less photo must not overwrite t1."""
    runner, adapter, sk = _setup()
    runner._queue_or_replace_pending_event(sk, _ev("t1", MessageType.TEXT, "t1"))
    runner._queue_or_replace_pending_event(sk, _ev("caption only", MessageType.PHOTO, "p2"))
    assert [e.message_id for e in _order(runner, adapter, sk)] == ["t1", "p2"]
