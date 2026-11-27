"""The adapter's own account of who sent an inbound message (t_118d31b1).

A Telegram user turn's ``display_metadata`` carries ``inbound_record``, built by the adapter from the
update itself, never from message text: ``from_id`` (the update's sender), ``chat_id``, ``message_id``,
``date`` (Telegram's server time), ``kind`` (what the message is: ``voice``, ``audio``, ``photo``, ... or
``text``), ``text`` (the raw message text or caption), ``forwarded`` (the message carries a forward origin) and
``edited``. A voice/audio message whose one clip the gateway transcribed itself also carries ``transcript`` and
``transcript_source`` (the STT provider), set by ``attach_transcript`` from the STT result, never from the
composed turn text (t_239103a9). A batch that absorbed a second message is ``merged``: it is no
longer one message. Readers (hermes-home ``scripts/shopper-codex/chat_gate.py``, Amazon chat-buy
approvals) treat a row without the record, or with ``forwarded``/``edited``/``merged`` set, as no proof.
Internal (self-injected) events never carry one. A message steered into a running turn keeps its record on
the steer row the agent writes (``steer_display_metadata``, t_b8fbfc54); a row built from several steers is
``merged``.
"""

from __future__ import annotations

from typing import Any

KEY = "inbound_record"
# Telegram ``Message`` media attributes, checked in order; the first one present names the record's ``kind``.
_KINDS = ("voice", "audio", "video_note", "video", "animation", "photo", "document", "sticker", "location",
          "contact", "poll")
TRANSCRIBED_KINDS = ("voice", "audio")


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
    kind = next((k for k in _KINDS if getattr(message, k, None)), "text" if isinstance(text, str) else "other")
    forwarded = any(getattr(message, a, None) for a in ("forward_origin", "forward_date", "forward_from",
                                                       "forward_from_chat", "is_automatic_forward"))
    return {"platform": "telegram", "from_id": ident(getattr(user, "id", None)),
            "chat_id": ident(getattr(getattr(message, "chat", None), "id", None)),
            "message_id": ident(getattr(message, "message_id", None)), "date": date,
            "kind": kind, "text": text if isinstance(text, str) else None, "forwarded": bool(forwarded),
            "edited": getattr(message, "edit_date", None) is not None}


def mark_merged(target: Any) -> None:
    """``target`` absorbed another message (text or media batching): its record no longer names one message.
    Rebinds a fresh ``metadata`` dict, so a shallow copy's source event keeps its own record."""
    meta = getattr(target, "metadata", None)
    rec = meta.get(KEY) if isinstance(meta, dict) else None
    if isinstance(rec, dict):
        target.metadata = {**meta, KEY: {**rec, "merged": True}}


def attach_transcript(event: Any, audio_paths: Any, clips: Any) -> None:
    """Record the gateway's own STT result on a voice/audio record (t_239103a9). ``clips`` is one
    ``{"path", "transcript", "provider"}`` per clip the STT step finished. Only a message with exactly one clip and
    one non-empty transcript of it gets ``transcript``; anything else (several clips, a failed or empty STT, a
    record that is not voice/audio) writes ``transcript: None``, so a re-run cannot leave a stale one behind.
    Rebinds a fresh ``metadata`` dict, as ``mark_merged`` does."""
    meta = getattr(event, "metadata", None)
    rec = meta.get(KEY) if isinstance(meta, dict) else None
    if not isinstance(rec, dict) or rec.get("kind") not in TRANSCRIBED_KINDS:
        return
    paths = list(dict.fromkeys(audio_paths or []))
    done = [c for c in clips or [] if isinstance(c, dict) and c.get("path") in paths]
    text, source = None, None
    if len(paths) == 1 and len(done) == 1:
        tx = done[0].get("transcript")
        if isinstance(tx, str) and tx.strip():
            text, source = tx, (str(done[0].get("provider")) if done[0].get("provider") else None)
    event.metadata = {**meta, KEY: {**rec, "transcript": text, "transcript_source": source}}


def steer_display_metadata(drained_text: Any, entries: Any) -> dict:
    """``display_metadata`` for the ONE user row a drained steer buffer becomes (t_b8fbfc54). ``entries`` are the
    gateway's provenance entries for that buffer, ``(text, internal, record)``. The row carries the record only
    when it is exactly one entry, that entry is the whole drained text and it has a record; a row built from
    several messages, or from text no entry accounts for (a steer that bypassed the gateway), carries the first
    record marked ``merged``, which approves nothing. No record on any entry: ``{}``."""
    entries = [e for e in entries or () if isinstance(e, tuple) and len(e) >= 2]
    recs = [e[2] for e in entries if len(e) > 2 and isinstance(e[2], dict)]
    if not recs:
        return {}
    rec = dict(recs[0])
    whole = len(entries) == 1 and str(entries[0][0]).strip() == str(drained_text or "").strip()
    if not whole:
        rec["merged"] = True
    return {KEY: rec}


def display_metadata(event: Any) -> dict:
    """``{"inbound_record": {...}}`` for a human turn whose adapter recorded one, else ``{}``. A record
    whose ``message_id`` is not the event's (a batch that moved its id to a later message) reads merged."""
    if getattr(event, "internal", False):
        return {}
    meta = getattr(event, "metadata", None)
    rec = meta.get(KEY) if isinstance(meta, dict) else None
    if not isinstance(rec, dict):
        return {}
    rec = dict(rec)
    event_id = getattr(event, "message_id", None)
    if rec.get("message_id") is not None and event_id is not None and str(event_id) != rec["message_id"]:
        rec["merged"] = True
    return {KEY: rec}
