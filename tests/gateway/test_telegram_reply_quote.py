"""Tests for Telegram native partial-quote handling in _build_message_event.

When a Telegram user replies using Telegram's native quote feature to
select only part of a prior message, the adapter must use ``message.quote.text``
(the user-selected substring) rather than ``message.reply_to_message.text``
(the entire replied-to message). Otherwise the agent receives the full prior
message as ``reply_to_text``, which can cause it to act on unrelated
actionable-looking text the user did not quote (#22619).
"""

from types import SimpleNamespace

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _make_adapter():
    return TelegramAdapter(PlatformConfig(enabled=True, token="***", extra={}))


def _make_message(
    text="follow-up",
    reply_to_text=None,
    reply_to_caption=None,
    reply_to_id=42,
    quote_text=None,
):
    chat = SimpleNamespace(id=111, type="private", title=None, full_name="Alice")
    user = SimpleNamespace(id=42, full_name="Alice")

    reply_to_message = None
    if reply_to_text is not None or reply_to_caption is not None:
        reply_to_message = SimpleNamespace(
            message_id=reply_to_id,
            text=reply_to_text,
            caption=reply_to_caption,
        )

    quote = None
    if quote_text is not None:
        quote = SimpleNamespace(text=quote_text)

    return SimpleNamespace(
        chat=chat,
        from_user=user,
        text=text,
        message_thread_id=None,
        message_id=1001,
        reply_to_message=reply_to_message,
        quote=quote,
        date=None,
        forum_topic_created=None,
    )


def test_native_partial_quote_used_as_reply_to_text():
    """When ``message.quote`` is present, prefer the selected substring."""
    from gateway.platforms.base import MessageType

    adapter = _make_adapter()
    msg = _make_message(
        text="mark this one as done",
        reply_to_text=(
            "Briefing:\n- Item A: deploy fix\n- Item B: rotate keys\n- Item C: update docs"
        ),
        quote_text="Item B: rotate keys",
    )

    event = adapter._build_message_event(msg, MessageType.TEXT)

    assert event.reply_to_text == "Item B: rotate keys"
    assert event.reply_to_message_id == "42"




def test_reply_to_authorship_stamped_from_from_user_not_text():
    """t_3ad14889: reply_to_is_own_message is set only when the replied-to message's
    from_user is this bot (id compare), never from what the text looks like."""
    from gateway.platforms.base import MessageType

    adapter = _make_adapter()
    adapter._bot = SimpleNamespace(id=777)
    own = _make_message(text="1", reply_to_text="  1. t_0000aaaa \u2192 x \u00b7 session s")
    own.reply_to_message.from_user = SimpleNamespace(id=777)
    event = adapter._build_message_event(own, MessageType.TEXT)
    assert event.reply_to_is_own_message is True and event.reply_to_author_id == "777"

    forged = _make_message(text="1", reply_to_text="  1. t_0000aaaa \u2192 x \u00b7 session s")
    forged.reply_to_message.from_user = SimpleNamespace(id=42)
    event = adapter._build_message_event(forged, MessageType.TEXT)
    assert event.reply_to_is_own_message is False and event.reply_to_author_id == "42"
