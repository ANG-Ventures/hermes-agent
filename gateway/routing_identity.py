"""Pure session-key evidence checks shared by Kanban repair and live wakes."""

from __future__ import annotations

from typing import Any, Optional
from types import SimpleNamespace


def creator_stamp_is_session_key(stamp: Any) -> bool:
    """Whether a ``tasks.session_id`` creator stamp is a session KEY.

    🔴 SINGLE SOURCE OF TRUTH for the stamp-shape discrimination — the
    2026-08-12 phantom-session regression (fork #588) happened because #568
    compared this column against routing-index keys unconditionally.
    ``tasks.session_id`` is a mixed-format column:

    * gateway-created tasks stamp the creating turn's session KEY
      (``agent:main:discord:group:<chat>:<user>`` — always contains ``:``);
    * worker/CLI-created tasks stamp a RAW session id
      (``20260811_220323_2eafab`` — never contains ``:``).

    A raw id can NEVER equal a routing-index key, so key-equality against a
    raw stamp silently yields empty evidence and re-mints the phantom
    session. Every consumer that binds evidence to the creator stamp MUST
    branch on this helper — never inline ``":" in stamp`` (two inlined
    copies is how normalizer drift starts) and never assume one format.
    Contract-tested by ``tests/test_creator_stamp_shape_contract.py``.
    """
    return ":" in str(stamp or "")


def canonical_chat_type(platform: str, chat_type: str) -> str:
    """Compatibility for pre-resolver Discord guild-channel envelopes."""
    return "group" if platform == "discord" and chat_type == "channel" else chat_type


def routing_owner_identity(session_key: str, source: Any):
    """Type-free destination ownership, retaining profile/workspace/thread isolation."""
    parts = session_key.split(":")
    if len(parts) < 5 or parts[0] != "agent":
        return None
    if source is None:
        return ("key", *parts[:3], *parts[4:])
    if isinstance(source, dict):
        source = SimpleNamespace(**{
            name: source.get(name) for name in (
                "platform", "chat_id", "thread_id", "prospective_thread_id",
                "scope_id", "user_id_alt", "user_id",
            )
        })
    platform = str(getattr(source.platform, "value", source.platform))
    chat = str(source.chat_id or "")
    if not chat:
        return None
    thread = str(source.thread_id or source.prospective_thread_id or "")
    if platform == "discord" and thread:
        chat, thread = thread, ""
    scope = str(source.scope_id or "") if platform == "slack" else ""
    user = str(source.user_id_alt or source.user_id or "")
    return ("source", parts[1], platform, scope, chat, thread, user)


class SessionKeyConflict(ValueError):
    """A rejected alias; carries only routing metadata for the chat notice."""

    def __init__(self, existing_key: str, candidate_key: str, source=None):
        self.existing_key = existing_key
        self.candidate_key = candidate_key
        self.keys = tuple(sorted((existing_key, candidate_key)))
        parts, other = existing_key.split(":"), candidate_key.split(":")
        self.platform, self.chat_id = parts[2], parts[4]
        self.thread_id = None
        if source is not None:
            get = source.get if isinstance(source, dict) else lambda name: getattr(source, name, None)
            self.chat_id = str(get("chat_id") or self.chat_id)
            self.thread_id = get("thread_id")
        super().__init__(
            f"session-key collision refused: {self.platform} {self.chat_id} "
            f"({parts[3]} vs {other[3]})"
        )


def assert_unique_routing_entries(entries, existing=None, *, retired_keys=()):
    """Check both the proposed index and durable ownership BEFORE replacement.

    Call under the storage transaction/lock. A whole-index rewrite must not
    hide an alias by deleting the old row first. Only explicit migration may
    retire an old spelling; the resulting index must still be unique.
    """
    owners = {}
    for batch in (existing or {}, entries):
        for key, data in batch.items():
            if batch is not entries and key in retired_keys:
                continue
            if not isinstance(data, dict):
                data = {}
            for owner in (routing_owner_identity(key, None),
                          routing_owner_identity(key, data.get("origin"))):
                if owner is None:
                    continue
                previous = owners.get(owner)
                if previous and previous != key and previous.split(":")[3] != key.split(":")[3]:
                    raise SessionKeyConflict(previous, key, data.get("origin"))
                owners[owner] = key


def effective_routing_lane(
    *,
    platform: Any,
    chat_id: Any,
    chat_type: Any,
    thread_id: Optional[Any] = None,
    prospective_thread_id: Optional[Any] = None,
) -> tuple[str, str, str, str]:
    """Return the lane dimensions actually encoded by ``build_session_key``."""

    def clean(value: Any) -> str:
        return str(value or "").strip()

    platform_value = clean(getattr(platform, "value", platform)).lower()
    chat = clean(chat_id)
    kind = canonical_chat_type(platform_value, clean(chat_type).lower())
    thread = clean(thread_id)
    prospective = clean(prospective_thread_id)
    if kind != "dm" and prospective and not thread:
        return platform_value, chat, "thread", prospective
    return platform_value, chat, kind, thread


def routing_key_carries_identity(
    session_key: Any,
    *,
    platform: Any,
    chat_id: Any,
    chat_type: Any,
    thread_id: Optional[Any] = None,
    prospective_thread_id: Optional[Any] = None,
    user_id: Optional[Any] = None,
    user_id_alt: Optional[Any] = None,
    scope_id: Optional[Any] = None,
) -> bool:
    """Whether ``session_key`` has the canonical tail for this identity.

    This mirrors the identity-relevant branches of ``build_session_key`` without
    importing gateway runtime state. A complete tail comparison matters: checking
    only ``endswith(participant)`` mistakes a shared key for per-user evidence when
    its chat or thread id happens to equal the participant string.

    DMs with a chat id normally omit participant identity. Slack is the one useful
    exception: its workspace scope is part of the key, so the exact scoped DM tail
    proves the missing identity tuple needed to reconstruct that session.
    """

    def clean(value: Any) -> str:
        return str(value or "").strip()

    key = clean(session_key)
    kind = clean(chat_type).lower()
    thread = clean(thread_id)
    prospective = clean(prospective_thread_id)
    platform_value, chat, key_chat_type, effective_thread = effective_routing_lane(
        platform=platform,
        chat_id=chat_id,
        chat_type=kind,
        thread_id=thread,
        prospective_thread_id=prospective,
    )
    participant = clean(user_id_alt) or clean(user_id)
    scope = clean(scope_id) if platform_value == "slack" else ""
    if not key or not platform_value or not participant:
        return False

    if kind == "dm":
        parts = [platform_value, "dm"]
        if scope:
            parts.append(scope)
        if chat:
            # The participant does not influence a chat-addressed DM key. Slack
            # scope does, and is therefore the only identity evidence to adopt.
            if platform_value != "slack" or not scope:
                return False
            parts.append(chat)
            if thread:
                parts.append(thread)
        else:
            parts.append(participant)
            if thread:
                parts.append(thread)
        return key.endswith(":" + ":".join(parts))

    parts = [platform_value, key_chat_type]
    if scope:
        parts.append(scope)
    if chat:
        parts.append(chat)
    if effective_thread:
        parts.append(effective_thread)
    parts.append(participant)
    return key.endswith(":" + ":".join(parts))
