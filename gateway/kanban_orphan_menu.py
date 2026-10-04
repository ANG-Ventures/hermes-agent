"""Orphan-card menu reply (t_6281f908, Ace 2026-10-03 14:42, D-O6/D-O7).

``kanban-cross-session-watch`` posts one 🏷 #alerts line for an orphan card
it could not home by itself, with the candidate homes as a numbered menu::

    🏷 Kanban t_1234abcd has no home session/channel (no session) — … reply with a number …
      1. t_1234abcd → <#1502…> · session 20261003_1415_ab (parent t_9f…)
      2. t_1234abcd [subs-ace] → <#1508…> · session 20261003_0900_cd (parent t_77…)

A bare-number reply to that message re-homes the card to the option's
session. Stateless: every option line carries the card, board and session, so
no menu table exists. Inferred and menu rehomes are FINAL: a card that already
has a chat home is refused (D-O5, there is no unhome verb).
"""
from __future__ import annotations

import re
from typing import NamedTuple, Optional

BARE_NUMBER_RE = re.compile(r"^\s*(\d{1,2})\s*$")
# "  2. t_1234abcd [board] → <#chan> · session <sid> (source)"
OPTION_RE = re.compile(
    r"^\s*(?P<n>\d{1,2})\.\s+(?P<card>t_[0-9a-f]{8})(?:\s+\[(?P<board>[a-z0-9][a-z0-9_-]*)\])?"
    r"\s+\u2192\s+.*?\u00b7\s+session\s+(?P<sid>\S+)",
    re.M,
)


class MenuChoice(NamedTuple):
    number: int
    card: str
    board: str
    session: str


def parse_choice(reply_text: Optional[str], replied_to: Optional[str]) -> Optional[MenuChoice]:
    """The option a bare-number reply picks, or None when this is not a menu reply.

    None (pass through untouched) unless the reply is a bare 1-2 digit number
    AND the replied-to message carries orphan-menu option lines. A number with no matching
    option is a menu reply with no choice: returned as a MenuChoice with
    ``card=""`` so the caller can answer instead of handing "7" to the agent."""
    m = BARE_NUMBER_RE.match(reply_text or "")
    if not m or not replied_to:
        return None
    # A long cron delivery can be chunked so the 🏷 head and an option land in
    # different messages: the option shape alone (card → chat · session sid) is
    # specific enough to identify the menu.
    options = {int(o["n"]): o for o in OPTION_RE.finditer(replied_to)}
    if not options:
        return None
    n = int(m.group(1))
    o = options.get(n)
    if o is None:
        return MenuChoice(n, "", "", "")
    return MenuChoice(n, o["card"], o["board"] or "default", o["sid"])


def apply_choice(choice: MenuChoice, *, actor: str = "") -> str:
    """Blocking: re-home the card. Returns the one-line reply for the chat."""
    from gateway.kanban_home_route import SessionLookupError, home_from_row, read_session_row
    from hermes_cli import kanban as kc
    from hermes_cli import kanban_db as kb

    if not choice.card:
        return f"\u26a0 No option {choice.number} in that menu."
    try:
        row = read_session_row(choice.session)
    except SessionLookupError as exc:
        return f"\u26a0 {choice.card} not re-homed: {exc}"
    target = home_from_row(choice.session, row).target
    if target is None:
        return f"\u26a0 {choice.card} not re-homed: session {choice.session} has no chat."
    with kb.connect_closing(board=choice.board) as conn:
        task = kb.get_task(conn, choice.card)
        if task is None:
            return f"\u26a0 No card {choice.card} on board {choice.board}."
        current = None
        if task.session_id and not kb.is_operator_home(task.session_id):
            try:
                current = home_from_row(task.session_id, read_session_row(task.session_id)).target
            except SessionLookupError as exc:
                return f"\u26a0 {choice.card} not re-homed: {exc}"
        if current is not None or kb.is_operator_home(task.session_id):
            return (f"{choice.card} already has a home ({task.session_id}); "
                    "rehomes are final, nothing changed.")
        if not kc.rehome_apply(conn, choice.card, choice.session, row, target):
            return f"\u26a0 {choice.card} not re-homed (unknown id)."
        kb.add_comment(
            conn, choice.card, author="kanban-orphan-menu",
            body=(f"orphan menu: re-homed to session {choice.session} "
                  f"(option {choice.number}, chosen by {actor or 'a reply'}; t_6281f908)"),
        )
    where = f"<#{target.chat_id}>" if target.platform == "discord" else f"{target.platform}:{target.chat_id}"
    return f"\u23f3 {choice.card} re-homed to {where} (session {choice.session}, option {choice.number})."
