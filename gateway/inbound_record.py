"""The adapter's own account of who sent an inbound message (t_118d31b1).

A Telegram user turn's ``display_metadata`` carries ``inbound_record``, built by the adapter from the
update itself, never from message text: ``from_id`` (the update's sender), ``chat_id``, ``message_id``,
``date`` (Telegram's server time), ``text`` (the raw message text or caption), ``forwarded`` (the message
carries a forward origin) and ``edited``. A batch that absorbed a second message is ``merged``: it is no
longer one message. Readers (hermes-home ``scripts/shopper-codex/chat_gate.py``, Amazon chat-buy
approvals) treat a row without the record, or with ``forwarded``/``edited``/``merged`` set, as no proof.
Internal (self-injected) events never carry one.
"""

from __future__ import annotations

from typing import Any

KEY = "inbound_record"


def telegram_record(message: Any) -> dict:
    """JSON-safe record of one Telegram ``Message``. Any field it cannot read is ``None``."""
    def ident(value: Any) -> Any:
        return str(value) if isinstance(value, (int, str)) and not isinstance(value, bool) else None

    try:
        date = float(message.date.timestamp())
    except Exception:  # noqa: BLE001 -- no readable date: the reader refuses a record without one
        date = None
    text = getattr(message, "text", None) or getattr(message, "caption", None)
    user = getattr(message, "from_user", None)
    forwarded = any(getattr(message, a, None) for a in ("forward_origin", "forward_date", "forward_from",
                                                       "forward_from_chat", "is_automatic_forward"))
    return {"platform": "telegram", "from_id": ident(getattr(user, "id", None)),
            "chat_id": ident(getattr(getattr(message, "chat", None), "id", None)),
            "message_id": ident(getattr(message, "message_id", None)), "date": date,
            "text": text if isinstance(text, str) else None, "forwarded": bool(forwarded),
            "edited": getattr(message, "edit_date", None) is not None}


def mark_merged(target: Any) -> None:
    """``target`` absorbed another message (text or media batching): its record no longer names one message."""
    meta = getattr(target, "metadata", None)
    rec = meta.get(KEY) if isinstance(meta, dict) else None
    if isinstance(rec, dict):
        meta[KEY] = {**rec, "merged": True}


def display_metadata(event: Any) -> dict:
    """``{"inbound_record": {...}}`` for a human turn whose adapter recorded one, else ``{}``."""
    if getattr(event, "internal", False):
        return {}
    meta = getattr(event, "metadata", None)
    rec = meta.get(KEY) if isinstance(meta, dict) else None
    return {KEY: dict(rec)} if isinstance(rec, dict) else {}
