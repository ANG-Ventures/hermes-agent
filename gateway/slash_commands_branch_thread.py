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

# Child transcript row that tells the MODEL it is on a branch (t_424dcfb5). role=user, never system:
# strict OpenAI-compatible providers reject a non-leading system message (#48338). Typed with a
# display_kind so it is machinery, not a human turn (is_user_originated_turn → False), and the
# user;user alternation repair leaves it alone.
BRANCH_NOTICE_DISPLAY_KIND = "branch_notice"
# Rows written by the "[Forked session]" release (#1911) before the rename to "branch" (t_3c2223d9).
# Read alias only: nothing writes it any more, but children minted under it still replay and /merge.
LEGACY_FORK_NOTICE_DISPLAY_KIND = "fork_notice"
FORK_NOTICE_DISPLAY_KIND = LEGACY_FORK_NOTICE_DISPLAY_KIND
_NOTICE_DISPLAY_KINDS = frozenset({BRANCH_NOTICE_DISPLAY_KIND, LEGACY_FORK_NOTICE_DISPLAY_KIND})


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


BRANCH_NOTICE_PREFIX = "[Branched session]"
# Closing sentinel written WITH the notice. The alternation repair may fold the notice into an
# adjacent user row (a mid-turn parent's unanswered prompt before it, the child's first prompt or a
# second notice after it) with a blank-line join that any human text can also contain, so the only
# honest way to strip the notice back out is a delimiter the notice itself carries. A row without
# the sentinel is never touched (Apollo review r1; Prism e8e07ff8b118 / 199166e82bdc / b3522bab429c).
BRANCH_NOTICE_END = "[/Branched session]"
_BRANCH_NOTICE_TEMPLATE = (
    "{prefix} This session was branched from {parent} at row {row} ({where}). The parent session "
    "continues the work that was in progress above; do NOT resume it here. Take new direction "
    "from the user in this thread. {end}"
)
# The wording #1911 shipped. Still stripped by /merge so a child branched under it folds cleanly;
# never produced again.
LEGACY_FORK_NOTICE_PREFIX = "[Forked session]"
LEGACY_FORK_NOTICE_END = "[/Forked session]"
_LEGACY_FORK_NOTICE_TEMPLATE = (
    "{prefix} This session was forked from {parent} at row {row} ({where}). The parent session "
    "continues the work that was in progress above; do NOT resume it here. Take new direction "
    "from the user in this thread. {end}"
)
# One release of read aliases for the old names (t_3c2223d9).
FORK_NOTICE_PREFIX = LEGACY_FORK_NOTICE_PREFIX
FORK_NOTICE_END = LEGACY_FORK_NOTICE_END


def _notice_span_re(template: str, prefix: str, end: str) -> re.Pattern:
    """One COMPLETE generated notice (template shape, closed by the sentinel), wherever it sits in
    the row. A human fragment that merely starts with the prefix is not a span start, so it can
    never swallow text up to a real notice folded after it."""
    return re.compile(
        re.escape(template).replace(
            re.escape("{prefix}"), re.escape(prefix)).replace(
            re.escape("{parent}"), r"\S+").replace(
            re.escape("{row}"), r"\d+").replace(
            re.escape("{where}"), r"[^()\n]*").replace(
            re.escape("{end}"), re.escape(end))
    )


# Both generations, so /merge drops the notice whichever release branched the child.
_NOTICE_SPANS: tuple[tuple[str, re.Pattern], ...] = (
    (BRANCH_NOTICE_END, _notice_span_re(_BRANCH_NOTICE_TEMPLATE, BRANCH_NOTICE_PREFIX, BRANCH_NOTICE_END)),
    (LEGACY_FORK_NOTICE_END,
     _notice_span_re(_LEGACY_FORK_NOTICE_TEMPLATE, LEGACY_FORK_NOTICE_PREFIX, LEGACY_FORK_NOTICE_END)),
)


def branch_notice_content(*, parent_session_id: str, row_count: int, platform: Optional[Platform],
                          parent_ref: Optional[str]) -> str:
    """The branch-notice text the child replays after the copied history: one paragraph, opened by
    :data:`BRANCH_NOTICE_PREFIX` and closed by :data:`BRANCH_NOTICE_END` so :func:`merge_delta_rows`
    can lift it out of a repair-folded row without guessing where the human text starts."""
    where = f"{platform.value} {parent_ref}" if platform and parent_ref else (
        platform.value if platform else "gateway")
    return _BRANCH_NOTICE_TEMPLATE.format(prefix=BRANCH_NOTICE_PREFIX, parent=parent_session_id,
                                          row=row_count, where=where, end=BRANCH_NOTICE_END)


def branch_notice_row(content: str) -> dict:
    """Transcript row for :func:`branch_notice_content` (shape of ``_branch_row``)."""
    return {"role": "user", "content": content, "display_kind": BRANCH_NOTICE_DISPLAY_KIND}


def is_branch_notice_row(msg: dict) -> bool:
    """True for a notice row of either generation (``branch_notice`` or the legacy ``fork_notice``)."""
    return isinstance(msg, dict) and msg.get("display_kind") in _NOTICE_DISPLAY_KINDS


def strip_branch_notices(content: str) -> Optional[str]:
    """*content* with every sentinel-delimited notice lifted out, or None when nothing but notices
    (and the blank-line joins the alternation repair put between them) was there. Text outside the
    ``[Branched session] ... [/Branched session]`` spans (and the legacy ``[Forked session]`` ones)
    is kept verbatim apart from the join whitespace; a string with no closing sentinel is returned
    unchanged, whatever it starts with."""
    pieces = [content]
    for end, span_re in _NOTICE_SPANS:
        if end not in content:
            continue
        pieces = [piece for p in pieces for piece in span_re.split(p)]
    if len(pieces) == 1:
        return content
    rest = "\n\n".join(p for p in (piece.strip() for piece in pieces) if p)
    return rest or None


# Old names, one release (t_3c2223d9).
fork_notice_content = branch_notice_content
fork_notice_row = branch_notice_row
is_fork_notice_row = is_branch_notice_row
strip_fork_notices = strip_branch_notices


def merge_delta_rows(rows: list) -> list:
    """The post-branch rows /merge summarizes: the branch notice is bookkeeping, not exploration.

    A typed notice row (either generation) is dropped. A user row that carries a sentinel-delimited notice (the
    live-replay alternation repair folds the notice into an adjacent user row: a mid-turn parent's
    unanswered prompt, the child's first prompt, a second notice on a re-forked branch) keeps only
    the text outside the notice span(s), or is dropped when nothing else is left. A row with no
    closing sentinel is kept untouched, including a human prompt that begins with the prefix.
    """
    out = []
    for m in rows:
        if is_branch_notice_row(m):
            continue
        content = m.get("content") if isinstance(m, dict) else None
        if m.get("role") == "user" and isinstance(content, str):
            rest = strip_branch_notices(content)
            if rest is None:
                continue
            if rest != content:
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
