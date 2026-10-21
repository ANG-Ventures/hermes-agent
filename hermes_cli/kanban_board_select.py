"""Which board a kanban verb addresses (S7 D2): the verb names it, or the card does.

There is no current board and no board env var. ``resolve(args)`` is the one surface rule, used by
the CLI (``kanban_command``), hence ``/kanban`` slash, and by the ``kanban_*`` tools and the
dashboard through :func:`resolve_slug`. First match wins:

1. an explicit ``--board`` / ``board=`` (must exist; under a ``HERMES_KANBAN_DB`` pin it must
   agree with the pin, else ``KanbanPinDivergenceError``);
2. a DB pin is present -> the board it names (:func:`kanban_db.pinned_board`);
3. the verb's card id -> :func:`kanban_db.board_of_task` (never guesses);
4. ``create --parent/--continues/--workspace-of <card>`` -> that card's board;
5. otherwise :func:`bare_verb_policy`: ``refuse`` (default) -> :class:`BoardRequired`, whose text
   names the next command; ``default`` -> ``DEFAULT_BOARD``.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable, Optional

from hermes_cli import kanban_db as kb

BARE_VERB_REFUSE = "refuse"
BARE_VERB_DEFAULT = "default"

# Verbs that address no single board: they sweep every board, or touch none.
BOARD_AGNOSTIC_VERBS = frozenset({
    "clone", "base-check", "budget", "home-index", "session-closeout", "notify-status", "locate",
})


class BoardRequired(ValueError):
    """A verb with no derivable board under ``kanban.bare_verb: refuse`` (rc 2, HTTP 400)."""


class BoardNotFound(ValueError):
    """An explicit ``--board`` names no board on disk (rc 1, HTTP 404)."""


def bare_verb_policy() -> str:
    """``kanban.bare_verb`` from the ROOT ``config.yaml`` (``kanban_home()``), never a profile's.

    Absent or unreadable == ``refuse``; only the literal ``default`` relaxes it (S7 D7).
    """
    try:
        import hermes_yaml

        path = kb.kanban_home() / "config.yaml"
        if not path.is_file():
            return BARE_VERB_REFUSE
        cfg = hermes_yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        value = ((cfg.get("kanban") or {}).get("bare_verb") if isinstance(cfg, dict) else None)
    except Exception:
        return BARE_VERB_REFUSE
    return BARE_VERB_DEFAULT if str(value or "").strip().lower() == BARE_VERB_DEFAULT else BARE_VERB_REFUSE


def refusal_text(verb: str) -> str:
    tail = " or --parent <card>." if verb == "create" else "."
    return (f"kanban: {verb} needs a board: pass --board <slug> (boards: hermes kanban boards list) "
            f"or a card id this verb can derive it from{tail}")


def _check_explicit(slug: str) -> str:
    """Rule 1: the named board exists, and agrees with a DB pin when one is set.

    Under a pin that names a board (``pinned_board()`` is a real slug) any other ``--board`` is
    refused: before S7 the CLI silently followed the pin. A pin outside every board's layout has
    no name to disagree with, so only the existing hazard guard applies to it.
    """
    if slug != kb.DEFAULT_BOARD and not kb.board_exists(slug):
        raise BoardNotFound(f"kanban: board {slug!r} does not exist. "
                            f"Create it with `hermes kanban boards create {slug}`.")
    raw = kb._raw_db_pin()
    if raw:
        pin = Path(raw).expanduser()
        pinned = kb.pinned_board()
        if pinned and pinned != kb.PINNED_SLUG and pinned != slug:
            raise kb.KanbanPinDivergenceError(
                f"kanban: --board {slug} contradicts HERMES_KANBAN_DB={raw}, which is board "
                f"{pinned!r}'s DB; the pin outranks --board, so this verb would address {pinned!r}. "
                f"Refusing. Pass --board {pinned}, or unset HERMES_KANBAN_DB to address {slug!r}."
            )
        kb._refuse_if_pin_contradicts_board_arg(slug, pin)
    return slug


def resolve_slug(board: Optional[str], *, verb: str, card_ids: Iterable[str] = ()) -> str:
    """The board slug for one verb; raises :class:`BoardRequired` / ``ValueError`` /
    ``KanbanBoardDerivationError`` / ``KanbanPinDivergenceError`` with the next command."""
    if board is not None:
        normed = kb._normalize_board_slug(board)
        if not normed:
            raise ValueError("kanban: --board requires a slug")
        return _check_explicit(normed)
    pinned = kb.pinned_board()
    if pinned:
        return pinned
    for card in card_ids:
        card = str(card or "").strip()
        if card:
            return kb.board_of_task(card)
    if bare_verb_policy() == BARE_VERB_DEFAULT:
        return kb.DEFAULT_BOARD
    raise BoardRequired(refusal_text(verb))


def card_ids_of(args: argparse.Namespace) -> list[str]:
    """Card ids a parsed CLI verb names, in derivation order (rules 3 and 4)."""
    ids: list[str] = []
    for name in ("task_id", "parent_id", "child_id"):
        value = getattr(args, name, None)
        if isinstance(value, str):
            ids.append(value)
    for name in ("task_ids", "ids"):
        ids.extend(v for v in (getattr(args, name, None) or []) if isinstance(v, str))
    if getattr(args, "kanban_action", None) == "create":
        parents = getattr(args, "parent", None) or []
        ids.extend(p for p in (parents if isinstance(parents, list) else [parents]) if isinstance(p, str))
        for name in ("continues", "workspace_of"):
            value = getattr(args, name, None)
            if isinstance(value, str):
                ids.append(value)
    return ids


def resolve(args: argparse.Namespace) -> Optional[str]:
    """The board for a parsed ``hermes kanban`` invocation; ``None`` for board-agnostic verbs."""
    verb = getattr(args, "kanban_action", None) or ""
    if verb in BOARD_AGNOSTIC_VERBS and getattr(args, "board", None) is None:
        return None
    if verb == "notify-repair" and getattr(args, "all_boards", False):
        return None
    return resolve_slug(getattr(args, "board", None), verb=verb, card_ids=card_ids_of(args))
