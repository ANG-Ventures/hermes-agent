"""``/branch`` destination on thread-capable platforms (#66023).

On Discord, Telegram, Slack and Matrix a plain ``/branch`` opens a NEW sibling thread for the
clone and leaves the current chat bound to the original session, so the user can keep both paths
alive. ``/branch --here`` keeps the legacy in-place switch; platforms without threads (and the CLI)
always branch in place. Pure helpers only — the handler lives in ``slash_commands_session.py``.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Optional

from gateway.config import Platform
from gateway.session import SessionSource

# Adapters that override ``BasePlatformAdapter.create_handoff_thread`` (the base returns None).
BRANCH_THREAD_PLATFORMS = frozenset({Platform.DISCORD, Platform.TELEGRAM, Platform.SLACK, Platform.MATRIX})
BRANCH_HERE_FLAG = "--here"
# Do NOT inherit the parent's /model + /reasoning overrides onto the child (default = inherit).
BRANCH_FRESH_FLAG = "--fresh"
_BRANCH_FLAGS = frozenset({BRANCH_HERE_FLAG, BRANCH_FRESH_FLAG})

# Child transcript row that tells the MODEL it is on a fork (t_424dcfb5). role=user, never system:
# strict OpenAI-compatible providers reject a non-leading system message (#48338). Typed with a
# display_kind so it is machinery, not a human turn (is_user_originated_turn → False), and the
# user;user alternation repair leaves it alone.
FORK_NOTICE_DISPLAY_KIND = "fork_notice"


def parse_branch_flags(raw: str) -> tuple[bool, bool, str]:
    """``(stay_here, fresh, title)`` from the text after ``/branch``.

    Only LEADING ``--here`` / ``--fresh`` tokens (any order) are flags; everything after the first
    non-flag token is the optional title.
    """
    tokens = (raw or "").strip().split()
    stay_here = fresh = False
    while tokens and tokens[0].lower() in _BRANCH_FLAGS:
        flag = tokens.pop(0).lower()
        stay_here = stay_here or flag == BRANCH_HERE_FLAG
        fresh = fresh or flag == BRANCH_FRESH_FLAG
    return stay_here, fresh, " ".join(tokens)


def parse_branch_args(raw: str) -> tuple[bool, str]:
    """``(stay_here, title)`` from the text after ``/branch``.

    Only leading flags are stripped; everything else is the optional title. The CLI strips the
    flags through the same parser so ``/branch --here`` never becomes a session titled ``--here``.
    """
    stay_here, _fresh, title = parse_branch_flags(raw)
    return stay_here, title


FORK_NOTICE_PREFIX = "[Forked session]"
_FORK_NOTICE_TEMPLATE = (
    "{prefix} This session was forked from {parent} at row {row} ({where}). The parent session "
    "continues the work that was in progress above; do NOT resume it here. Take new direction "
    "from the user in this thread."
)
# The COMPLETE notice, anchored at the start of a user row; a human prompt that merely starts
# with "[Forked session]" does not match and is never stripped (Prism P1 e8e07ff8b118).
_FORK_NOTICE_RE = re.compile(
    "^" + re.escape(_FORK_NOTICE_TEMPLATE).replace(
        re.escape("{prefix}"), re.escape(FORK_NOTICE_PREFIX)).replace(
        re.escape("{parent}"), r"\S+").replace(
        re.escape("{row}"), r"\d+").replace(
        re.escape("{where}"), r"[^()\n]*")
    + r"(?:\n\n|$)"
)


def fork_notice_content(*, parent_session_id: str, row_count: int, platform: Optional[Platform],
                        parent_ref: Optional[str]) -> str:
    """The fork-notice text the child replays after the copied history (single paragraph: the
    alternation repair joins it to the next human turn with a blank line, which is the seam
    :func:`merge_delta_rows` splits on)."""
    where = f"{platform.value} {parent_ref}" if platform and parent_ref else (
        platform.value if platform else "gateway")
    return _FORK_NOTICE_TEMPLATE.format(prefix=FORK_NOTICE_PREFIX, parent=parent_session_id,
                                        row=row_count, where=where)


def fork_notice_row(content: str) -> dict:
    """Transcript row for :func:`fork_notice_content` (shape of ``_branch_row``)."""
    return {"role": "user", "content": content, "display_kind": FORK_NOTICE_DISPLAY_KIND}


def is_fork_notice_row(msg: dict) -> bool:
    return isinstance(msg, dict) and msg.get("display_kind") == FORK_NOTICE_DISPLAY_KIND


def merge_delta_rows(rows: list) -> list:
    """The post-branch rows /merge summarizes: the fork notice is bookkeeping, not exploration.

    A standalone notice row is dropped. The live-replay read repairs ``user;user`` alternation by
    folding the notice into the next human turn (``notice + "\\n\\n" + prompt``, display_kind
    dropped), so a user row that starts with the COMPLETE generated notice keeps only the human
    part. Anything else, including a human prompt that merely begins with the prefix, is kept.
    """
    out = []
    for m in rows:
        if is_fork_notice_row(m):
            continue
        content = m.get("content") if isinstance(m, dict) else None
        if m.get("role") == "user" and isinstance(content, str) and content.startswith(FORK_NOTICE_PREFIX):
            match = _FORK_NOTICE_RE.match(content)
            if match is not None:
                rest = content[match.end():]
                if not rest.strip():
                    continue
                m = {**m, "content": rest}
        out.append(m)
    return out


def branch_thread_parent(source: SessionSource) -> Optional[str]:
    """Chat that can host a sibling thread for *source*, or None when nothing can (Discord DMs,
    a Discord thread whose parent channel is unknown)."""
    if source.platform not in BRANCH_THREAD_PLATFORMS:
        return None
    if source.platform == Platform.DISCORD:
        if source.chat_type == "dm":
            return None
        # Inbound Discord threads carry ``chat_id == thread_id``; the sibling goes under the real
        # text channel, which only ``parent_chat_id`` names.
        if source.thread_id or source.chat_type == "thread":
            return str(source.parent_chat_id) if source.parent_chat_id else None
    # Telegram forum topics, Slack threads and Matrix threads all key on the parent chat/room id.
    return str(source.chat_id) if source.chat_id else None


def branch_dest_source(source: SessionSource, *, parent_id: str, thread_id: str, title: str) -> SessionSource:
    """The ``SessionSource`` a follow-up typed in the new thread will arrive on — the shape must
    match each adapter's inbound source or the clone is bound to a key nobody ever reads (the
    CLI→platform handoff in ``run_startup.py::_handoff_resolve_destination`` mirrors the same
    rules). Per-message fields are dropped; identity/scope fields travel with the copy."""
    common = dict(thread_id=str(thread_id), message_id=None, prospective_thread_id=None,
                  auto_thread_created=False, auto_thread_initial_name=None)
    if source.platform == Platform.DISCORD:
        # Discord keys an in-thread message on the thread's OWN id as chat_id.
        return dataclasses.replace(source, chat_id=str(thread_id), chat_name=title or source.chat_name,
                                   chat_type="thread", parent_chat_id=str(parent_id), **common)
    # Telegram (``group:<chat>:<topic>``, private-chat topics stay ``dm``), Slack (parent channel's
    # dm/group + workspace scope) and Matrix (room type) key a thread reply on the PARENT chat's type.
    chat_type = "group" if source.chat_type == "thread" else source.chat_type
    return dataclasses.replace(source, chat_id=str(parent_id), chat_type=chat_type, **common)


def format_thread_ref(platform: Optional[Platform], thread_id: str) -> str:
    """Clickable pointer where the platform has one (Discord ``<#id>`` mentions); id otherwise."""
    if platform == Platform.DISCORD:
        return f"<#{thread_id}>"
    return f"`{thread_id}`"
