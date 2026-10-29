"""Which board a card or a DB pin belongs to (S7 D1 + D3), read-only and never guessing.

``pinned_board()`` names the board a ``HERMES_KANBAN_DB`` pin points at; ``board_of_task()``
finds the ONE board holding a card id. Both probe each board's LAYOUT path
(``_board_db_path_ignoring_pin``), never ``kanban_db_path``: under a pin that returns the pin for
every slug and would make every card AMBIGUOUS. ``hermes kanban locate`` is the CLI encoding of
``board_of_task`` (S7 Q2); ``scripts/kanban_board_resolve.py`` in hermes-home wraps that verb.
Spec: hermes-home plans/2026-10-21_sdlc-simplification/S7-boards-no-global-pointer-SPEC.md.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Optional


def _facade():
    # Late: the facade imports this module at its tail, so a module-level import would be
    # circular whichever of the two is imported first.
    from hermes_cli import kanban_db

    return kanban_db

_log = logging.getLogger(__name__)

# A ``HERMES_KANBAN_DB`` pin that agrees with no board's layout path still IS the DB every open
# uses; only its NAME is unknown. One encoding of that name, shared with hermes-home's
# scripts/kanban_board_resolve.py ``'override'`` (S7 D1, RC-7).
PINNED_SLUG = "override"

_PINNED_BOARD_MEMO: dict[tuple, str] = {}
_PINNED_BOARD_ERROR_LOGGED: set[str] = set()
# raw pin -> why naming it failed. Not memoised (the next call retries); the surface reads it to
# refuse the verb with the next command instead of acting under a pin it could not name (RC-11).
_PINNED_BOARD_ERRORS: dict[str, str] = {}


class KanbanBoardDerivationError(ValueError):
    """A card id could not be mapped to exactly one board (S7 D3). ``code`` is M8's code."""

    code = "BOARD_DERIVATION"


class KanbanCardNotFound(KanbanBoardDerivationError):
    code = "CARD_NOT_FOUND"


class KanbanCardAmbiguous(KanbanBoardDerivationError):
    code = "CARD_AMBIGUOUS"


class KanbanBoardUnreadable(KanbanBoardDerivationError):
    code = "BOARD_UNREADABLE"


def raw_db_pin() -> str:
    """The ``HERMES_KANBAN_DB`` pin as set; ``""`` when absent or sandboxed. Never raises."""
    _kb = _facade()
    return _kb._kanban_path_override("HERMES_KANBAN_DB").strip()


def board_db_layout() -> list[tuple[str, Path]]:
    """``(slug, layout DB path)`` for every board on disk, archived included, ``default`` first.

    The set :func:`kanban_db.list_boards` enumerates, resolved from ONE root read: per-board
    ``kanban_home()`` resolution costs ~4 ms a board at the live count (S7 RC-2). A dir named for
    a slug IS that board's dir; aliases redirect retired names, which have no dir.
    """
    _kb = _facade()
    home = _kb.kanban_home()
    out: list[tuple[str, Path]] = [(_kb.DEFAULT_BOARD, home / "kanban.db")]
    root = home / "kanban" / "boards"
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        try:
            slug = _kb._normalize_board_slug(child.name)
        except ValueError:
            continue
        if not slug or slug == _kb.DEFAULT_BOARD or not child.is_dir():
            continue
        if (child / "kanban.db").exists() or (child / "board.json").exists():
            out.append((slug, child / "kanban.db"))
    return out


def _board_archived(board_path: Path, slug: str) -> bool:
    _kb = _facade()
    if slug == _kb.DEFAULT_BOARD:
        return False
    try:
        meta = json.loads((board_path / "board.json").read_text(encoding="utf-8-sig"))
        return bool(meta.get("archived"))
    except (OSError, ValueError, AttributeError):
        return False


def pinned_board() -> Optional[str]:
    """Slug of the board ``HERMES_KANBAN_DB`` names; ``None`` only when there is no pin.

    Total (RC-11): an error while naming the pin returns :data:`PINNED_SLUG` and records the
    reason in ``_PINNED_BOARD_ERRORS``; :func:`pinned_board_error` hands it to the surface, which
    refuses the verb with the next command.
    """
    raw = raw_db_pin()
    return board_named_by_pin(raw) if raw else None


def pinned_board_error() -> Optional[str]:
    """Why the current pin could not be named, or ``None`` (no pin, or it was named)."""
    raw = raw_db_pin()
    return _PINNED_BOARD_ERRORS.get(raw) if raw else None


def board_named_by_pin(raw: str) -> str:
    """The board whose layout DB path is the file ``raw`` names, else :data:`PINNED_SLUG`.

    Spelling-blind inverse of ``_board_db_path_ignoring_pin`` (RC-7): compared through
    ``_pin_file_agrees``, the contradiction guard's own predicate, so a firmlink / symlinked /
    case-variant spelling of a board's DB names that board. Memoised on (pin, root, boards/
    mtime): a new board dir changes the mtime, so a process pays one scan per pin.
    """
    _kb = _facade()
    try:
        root = _kb.boards_root()
        try:
            stamp = root.stat().st_mtime_ns
        except FileNotFoundError:
            stamp = None
        key = (raw, str(_kb.kanban_home()), stamp)
        hit = _PINNED_BOARD_MEMO.get(key)
        if hit is not None:
            return hit
        pin = Path(raw).expanduser()
        found = PINNED_SLUG
        with _kb.enumerating_boards():
            for slug, path in board_db_layout():
                if _kb._pin_file_agrees(path, pin):
                    found = slug
                    break
        if len(_PINNED_BOARD_MEMO) > 256:
            _PINNED_BOARD_MEMO.clear()
        _PINNED_BOARD_MEMO[key] = found
        _PINNED_BOARD_ERRORS.pop(raw, None)
        return found
    except Exception as exc:  # RC-11: total; the pin still routes every open.
        _PINNED_BOARD_ERRORS[raw] = f"{type(exc).__name__}: {exc}"
        if raw not in _PINNED_BOARD_ERROR_LOGGED:
            _PINNED_BOARD_ERROR_LOGGED.add(raw)
            _log.warning("kanban: could not name the board HERMES_KANBAN_DB=%s pins (%s: %s); "
                         "using %r", raw, type(exc).__name__, exc, PINNED_SLUG)
        return PINNED_SLUG


_BOARD_FOR_CONN_MEMO: dict[tuple, Optional[str]] = {}


def _conn_main_file(conn: sqlite3.Connection) -> str:
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main":
            return row[2] or ""
    return ""


def board_for_conn(conn: sqlite3.Connection) -> Optional[str]:
    """The board whose database file ``conn`` is open on, or ``None`` when no board owns it.

    THE one answer to "which board does a journal record for this connection belong to"
    (Prism 826db72bcf2d): a layout board's DB names that board; the ``HERMES_KANBAN_DB`` pin's
    file names :func:`board_named_by_pin` (``PINNED_SLUG`` off-layout). Anything else (a
    snapshot, a forensics copy, ``:memory:``) is ``None``: journaling it under the ambient board
    would replay its writes into the live one (Prism 77b7e56c0d2c). Compared by filesystem
    identity, memoised on (file, its identity, home, boards/ mtime, pin, its identity). Never raises.
    """
    _kb = _facade()
    try:
        actual = _conn_main_file(conn)
        if not actual:
            return None
        root = _kb.boards_root()
        try:
            stamp = root.stat().st_mtime_ns
        except FileNotFoundError:
            stamp = None
        raw = raw_db_pin()
        here = Path(actual)
        ident = _kb._path_identity(here)
        if ident is None:
            return None
        # The FILE's identity is in the key, not just its name: a path re-pointed at another
        # file (os.replace of a hard-link alias with a snapshot) must miss (Prism 2bfd9f8c68d9).
        pin_ident = _kb._path_identity(Path(raw).expanduser()) if raw else None
        key = (actual, ident, str(_kb.kanban_home()), stamp, raw, pin_ident)
        if key in _BOARD_FOR_CONN_MEMO:
            return _BOARD_FOR_CONN_MEMO[key]
        found: Optional[str] = None
        with _kb.enumerating_boards():
            for slug, path in board_db_layout():
                if _kb._same_path(here, path):
                    found = slug
                    break
        if found is None and raw and _kb._same_path(here, Path(raw).expanduser()):
            found = board_named_by_pin(raw)
        if len(_BOARD_FOR_CONN_MEMO) > 256:
            _BOARD_FOR_CONN_MEMO.clear()
        _BOARD_FOR_CONN_MEMO[key] = found
        return found
    except Exception:
        return None


def _unreadable(slug: str, exc: BaseException) -> KanbanBoardUnreadable:
    return KanbanBoardUnreadable(
        f"kanban: board {slug} is unreadable ({type(exc).__name__}: {exc}); fix it or "
        f"pass --board <slug> to bypass derivation."
    )


def board_of_task(task_id: str) -> str:
    """The ONE board holding ``task_id``, probed read-only across every board on disk.

    Never guesses (S7 D3, GT6): zero hits -> :class:`KanbanCardNotFound`; two or more ->
    :class:`KanbanCardAmbiguous` (archived hits marked); any unreadable board ->
    :class:`KanbanBoardUnreadable` (fail closed: the card might be on it). A board dir with no
    ``kanban.db`` holds no cards. Each probe is a ``mode=ro`` open (no init, no
    migration, no mkdir) on the board's layout path.
    """
    _kb = _facade()
    tid = str(task_id or "").strip()
    if not tid:
        raise KanbanCardNotFound("kanban: a card id is required to derive its board.")
    hits: list[tuple[str, bool]] = []
    with _kb.enumerating_boards():
        boards = board_db_layout()
        for slug, path in boards:
            if not path.exists():
                continue
            # One indexed SELECT per board on a ``mode=ro`` open (never creates, never migrates).
            # A file without a ``tasks`` table (0-byte phantom, foreign sqlite) fails it with
            # ``no such table``: the same not-a-board verdict as ``connect_readonly``'s
            # ``assert_is_board_db``, without its second query per board (RC-2 cost).
            try:
                conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            except Exception as exc:
                raise _unreadable(slug, exc) from exc
            try:
                row = conn.execute("SELECT 1 FROM tasks WHERE id = ? LIMIT 1", (tid,)).fetchone()
            except sqlite3.Error as exc:
                raise _unreadable(slug, exc) from exc
            finally:
                conn.close()
            if row is not None:
                hits.append((slug, _board_archived(path.parent, slug)))
    if not hits:
        raise KanbanCardNotFound(f"kanban: {tid} is on no board (checked {len(boards)} boards).")
    if len(hits) > 1:
        named = ", ".join(f"{s} (archived)" if arch else s for s, arch in hits)
        raise KanbanCardAmbiguous(f"kanban: {tid} exists on {named}; pass --board <slug>.")
    return hits[0][0]


def locate(task_id: str) -> tuple[str, str]:
    """``(slug, db_path)`` for ``hermes kanban locate``. Under a pin: the pin's board and the pin
    itself (a pinned process addresses that DB whatever the card says; D2 step 2)."""
    _kb = _facade()
    raw = raw_db_pin()
    if raw:
        return board_named_by_pin(raw), raw
    slug = board_of_task(task_id)
    with _kb.enumerating_boards():
        return slug, str(_kb._board_db_path_ignoring_pin(slug))
