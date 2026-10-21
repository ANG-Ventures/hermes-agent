"""Which board a kanban verb addresses (S7 D2): the verb names it, or the pin or the card does.

``resolve(args)`` is the surface rule. First match wins:

1. an explicit ``--board`` (must exist; under a ``HERMES_KANBAN_DB`` pin that names a board it
   must be that board, else ``KanbanPinDivergenceError``);
2. a DB pin is present -> the board it names (``pinned_board``); a pin that could not be named
   refuses the verb with the next command (RC-11);
3. the verb's card id -> ``board_of_task`` (never guesses);
4. ``create --parent/--continues/--workspace-of <card>`` -> that card's board;
5. otherwise :func:`bare_verb_policy`: ``refuse`` -> :class:`BoardRequired`, whose text names the
   next command; ``default`` -> ``DEFAULT_BOARD``.

Gated (S7 C1a): ``kanban.bare_verb`` in the ROOT config.yaml. Absent == ``legacy``: the pre-S7
resolution (``--board`` > ``HERMES_KANBAN_BOARD`` > ``kanban/current`` > ``default``) is untouched
and this module only serves ``hermes kanban locate``. ``refuse`` / ``default`` turn S7 on. C1b
flips the absent value to ``refuse`` and deletes ``legacy`` with the pointer.
"""
from __future__ import annotations

import argparse
from typing import Iterable, Optional

BARE_VERB_LEGACY = "legacy"
BARE_VERB_REFUSE = "refuse"
BARE_VERB_DEFAULT = "default"
_POLICIES = frozenset({BARE_VERB_REFUSE, BARE_VERB_DEFAULT})

# Verbs that address no single board: they sweep every board, or touch none.
BOARD_AGNOSTIC_VERBS = frozenset({
    "clone", "base-check", "budget", "home-index", "session-closeout", "notify-status", "locate",
})

_POLICY_CACHE: dict[str, tuple[Optional[tuple[int, int]], str]] = {}


class BoardRequired(ValueError):
    """A verb with no derivable board under ``kanban.bare_verb: refuse`` (rc 2)."""


class BoardNotFound(ValueError):
    """An explicit ``--board`` names no board on disk (rc 1)."""


def bare_verb_policy() -> str:
    """``kanban.bare_verb`` from the ROOT ``config.yaml`` (``kanban_home()``), never a profile's.

    ``refuse`` / ``default`` (S7 D7) or ``legacy`` (absent, unreadable, anything else). Cached on
    the file's (mtime, size): ``get_current_board`` asks on every call.
    """
    from hermes_cli import kanban_db as kb

    path = kb.kanban_home() / "config.yaml"
    try:
        st = path.stat()
        stamp: Optional[tuple[int, int]] = (st.st_mtime_ns, st.st_size)
    except OSError:
        return BARE_VERB_LEGACY
    cached = _POLICY_CACHE.get(str(path))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        import hermes_yaml as yaml

        cfg = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
        kanban = cfg.get("kanban") if isinstance(cfg, dict) else None
        value = str((kanban or {}).get("bare_verb") or "").strip().lower() if isinstance(kanban, dict) else ""
    except Exception:
        value = ""
    policy = value if value in _POLICIES else BARE_VERB_LEGACY
    _POLICY_CACHE[str(path)] = (stamp, policy)
    return policy


def s7_enabled() -> bool:
    """True when the S7 resolver replaces the pointer (``kanban.bare_verb`` set)."""
    return bare_verb_policy() != BARE_VERB_LEGACY


def refusal_text(verb: str) -> str:
    tail = " or --parent <card>." if verb == "create" else "."
    return (f"kanban: {verb} needs a board: pass --board <slug> (boards: hermes kanban boards list) "
            f"or a card id this verb can derive it from{tail}")


def unnamed_pin_text(raw: str, why: str) -> str:
    """RC-11: the one next command when a pin's board could not be named."""
    return (f"kanban: could not name the board HERMES_KANBAN_DB={raw} pins ({why}); "
            f"pass --board <the slug that DB belongs to> (boards: hermes kanban boards list).")


def _check_explicit(slug: str) -> str:
    """Rule 1: the named board exists, and agrees with a DB pin when one names a board."""
    from hermes_cli import kanban_db as kb
    from pathlib import Path

    if slug != kb.DEFAULT_BOARD and not kb.board_exists(slug):
        raise BoardNotFound(f"kanban: board {slug!r} does not exist. "
                            f"Create it with `hermes kanban boards create {slug}`.")
    raw = kb.raw_db_pin()
    if raw:
        pinned = kb.pinned_board()
        if pinned and pinned != kb.PINNED_SLUG and pinned != slug:
            raise kb.KanbanPinDivergenceError(
                f"kanban: --board {slug} contradicts HERMES_KANBAN_DB={raw}, which is board "
                f"{pinned!r}'s DB; the pin outranks --board, so this verb would address {pinned!r}. "
                f"Refusing. Pass --board {pinned}, or unset HERMES_KANBAN_DB to address {slug!r}."
            )
        kb._refuse_if_pin_contradicts_board_arg(slug, Path(raw).expanduser())
    return slug


def resolve_slug(board: Optional[str], *, verb: str, card_ids: Iterable[str] = ()) -> str:
    """The board slug for one verb. Raises :class:`BoardRequired`, :class:`BoardNotFound`,
    ``ValueError``, ``KanbanBoardDerivationError`` or ``KanbanPinDivergenceError``, each naming
    the next command."""
    from hermes_cli import kanban_db as kb

    if board is not None:
        normed = kb._normalize_board_slug(board)
        if not normed:
            raise ValueError("kanban: --board requires a slug")
        return _check_explicit(normed)
    pinned = kb.pinned_board()
    if pinned:
        failed = kb.pinned_board_error()
        if failed:  # RC-11: never act under a pin whose board could not be named
            raise BoardRequired(unnamed_pin_text(kb.raw_db_pin(), failed))
        return pinned
    policy = bare_verb_policy()
    for card in card_ids:
        card = str(card or "").strip()
        if not card:
            continue
        try:
            return kb.board_of_task(card)
        except kb.KanbanCardNotFound:
            # Under ``default`` a card on no board falls to step 5, as before S7, and the verb
            # reports the missing card itself. AMBIGUOUS and UNREADABLE always refuse.
            if policy != BARE_VERB_DEFAULT:
                raise
            break
    if policy == BARE_VERB_DEFAULT:
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
