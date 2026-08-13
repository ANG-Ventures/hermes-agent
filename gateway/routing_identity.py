"""Pure session-key evidence checks shared by Kanban repair and live wakes."""

from __future__ import annotations

from typing import Any, Optional


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
    kind = clean(chat_type).lower()
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
