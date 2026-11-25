"""Shape gate for a block that waits on a human (t_2fecb5d0).

A ``needs_input`` block is a question for a person. On board default, 6 of 44
sampled human-wait blocks (30 days to 2026-11-25) did not say what was needed,
why, or how to answer, so the operator had to open the card to find out.
The worker-facing entrypoints (``kanban_block`` and ``hermes kanban block``) refuse
such a block unless the reason carries ``Need:``, ``Why:`` and ``How to reply:``.

Untouched: every other kind, and a ``needs_input`` block whose ``unblock_when:``
lines all name a non-human predicate (a PR merge, a card finishing, a time).
Internal callers of ``kanban_db.block_task`` (dispatcher parks) never pass here.

Kill switch: ``kanban.human_ask_shape: off`` in the board home or profile config.
Idea mined from dellanio/hermes-kanban-empowered (ke/worker_guard.py, ke/asks.py);
no code taken.
"""

from __future__ import annotations

import re
from typing import Optional

HUMAN_WAIT_KIND = "needs_input"

# unblock_when predicates that clear without a person answering.
NON_HUMAN_PREDICATES = frozenset({"pr_merged", "card_done", "after"})

_UNBLOCK_WHEN = re.compile(r"(?im)^[ \t]*unblock_when:[ \t]*([A-Za-z_]+)")
_FIELDS = (
    ("Need:", re.compile(r"(?i)(?:^|[\s.;|/])need:[ \t]*\S")),
    ("Why:", re.compile(r"(?i)(?:^|[\s.;|/])why:[ \t]*\S")),
    ("How to reply:", re.compile(r"(?i)(?:^|[\s.;|/])how to reply:[ \t]*\S")),
)

REFUSAL = (
    "refused: a needs_input block waits on a human, so its reason must say what you need, "
    "why, and how to answer. Missing: {missing}. Use this shape:\n"
    "  Need: <the one decision or action you need from the operator>\n"
    "  Why: <what it unblocks, or what breaks without it>\n"
    "  How to reply: <the exact reply, e.g. 'A' or 'B', a time, or a command to run>\n"
    "    A) <option>   B) <option>   (optional lettered options)\n"
    "  unblock_when: ruling:<card>-<topic>\n"
    "A wait on a PR, a card or a time is not a human wait: use kind=dependency, "
    "kind=deferred, or unblock_when: pr_merged:<o/r#n> / card_done:<id>."
)


def enabled() -> bool:
    """``kanban.human_ask_shape`` (board home first, then profile); default on."""
    try:
        from hermes_cli import kanban_db as kb

        value, _src = kb._kanban_review_setting("human_ask_shape", "on")
    except Exception:
        return True
    return str(value).strip().lower() not in {"off", "false", "0", "no"}


def is_human_wait(kind: Optional[str], reason: Optional[str]) -> bool:
    if kind != HUMAN_WAIT_KIND:
        return False
    preds = [p.lower() for p in _UNBLOCK_WHEN.findall(reason or "")]
    return not (preds and all(p in NON_HUMAN_PREDICATES for p in preds))


def shaped_for_machine(reason: str) -> str:
    """Prefix a fixed Need/Why/How to a block a program (not a model it can re-prompt)
    files on a worker's behalf, e.g. the native-worker handback, which gets ONE board call."""
    if refusal(HUMAN_WAIT_KIND, reason) is None:
        return reason
    return ("Need: read the block below and answer it. Why: the worker stopped and cannot ask "
            "again this run. How to reply: comment the answer on the card, then unblock it.\n"
            + reason)


def refusal(kind: Optional[str], reason: Optional[str]) -> Optional[str]:
    """The house refusal text for a malformed human-wait block, else ``None``."""
    if not is_human_wait(kind, reason) or not enabled():
        return None
    missing = [label for label, rx in _FIELDS if not rx.search(reason or "")]
    if not missing:
        return None
    return REFUSAL.format(missing=", ".join(missing))
