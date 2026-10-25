"""Refuse a kanban comment whose ``close-on`` token is not a valid release line (t_ebc8eb87).

Incident 2026-10-17 (t_d712b9ff): an ACK comment said "Bare `close-on: merged` posted
above"; the review-merge-pass read that prose as a new, unprovable gate and re-dispatched a
merged card's worker. hermes-home#4812 tried to refuse such bodies in a pre_tool_call hook
that parses the SHELL COMMAND, and a command-text parser cannot see the stored body
(``$GATE``, ``printf``, heredocs on other fds, line continuations). This check runs where the
body is exact: ``kanban_db.add_comment``, which the CLI ``comment`` verb, the
``kanban_comment`` tool, the dashboard and every other writer share.

Rule: every line carrying the ``close-on`` token must be a ``close_on.LINE_RE`` line whose
gate ``close_on.parse_release`` accepts. The grammar is the fleet's, loaded by file path from
``<fleet root>/scripts/lib/close_on.py``; nothing here re-implements it. When that library
(or ``our_authors.py``, the automation-marker set) cannot be loaded, the comment is ALLOWED
and the failure logged once: the closer stays fail-closed, this is only the early warning.

Exempt: automation authors (:data:`MACHINE_AUTHORS`) and bodies that lead with an automation
marker (``our_authors.is_machine``; the merge pass posts as ``default`` behind
``[review-merge-pass]``), so the closer's own comments are never refused.
Operator off-switch: ``<fleet root>/state/close-on-comment-policy-off`` (the hook's file).
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
from pathlib import Path
from types import ModuleType
from typing import Optional

logger = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"close-on", re.I)
# Kernel writers plus the fleet automation that posts under its own author name.
MACHINE_AUTHORS = frozenset({
    "dispatcher", "kanban-dispatcher", "kanban", "kanban-pr-gate", "kanban-overlap-gate",
    "kanban-orphan-menu", "review-merge-pass", "kanban-review-merge-pass", "fleetreview-route",
    "prism-holds", "review-failsafe", "close-on-convert", "boomerang-wake",
})
REFUSAL_HINT = "post the bare line on its own; say release line in prose"

_LIB_CACHE: dict[str, tuple[int, ModuleType]] = {}
_WARNED: set[str] = set()


class CloseOnLineRefused(ValueError):
    """The comment body carries a ``close-on`` token outside a valid release line."""


def _fleet_root() -> Path:
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root()


def _warn_once(key: str, msg: str) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        logger.warning("close-on comment guard disabled (allowing): %s", msg)


def _load(name: str) -> Optional[ModuleType]:
    """``<fleet root>/scripts/lib/<name>.py`` by path, cached on mtime; None when unreadable."""
    path = _fleet_root() / "scripts" / "lib" / f"{name}.py"
    try:
        mtime = path.stat().st_mtime_ns
        hit = _LIB_CACHE.get(str(path))
        if hit and hit[0] == mtime:
            return hit[1]
        spec = importlib.util.spec_from_file_location(f"_kanban_guard_{name}", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"no loader for {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:  # noqa: BLE001 -- any load failure fails open, by contract
        _warn_once(str(path), f"{path}: {exc}")
        return None
    _LIB_CACHE[str(path)] = (mtime, mod)
    return mod


def violations(body: str, lib: ModuleType) -> list[tuple[str, str]]:
    """``[(line, why)]`` for every token-carrying line that is not a valid release line."""
    out = []
    # ``\n`` only, the fleet grammar's one line boundary (``LINE_RE`` is ``re.M`` with a
    # ``[^\n]*`` gate). ``splitlines()`` also splits on \v, \f, \x1c-\x1e, \x85, U+2028/9,
    # which would pass ``close-on: merged\vprose`` as a valid line plus prose.
    for line in str(body or "").split("\n"):
        if not TOKEN_RE.search(line):
            continue
        m = lib.LINE_RE.match(line)
        if m and lib.parse_release(m.group("gate")) is not None:
            continue
        out.append((line.strip()[:160], "malformed close-on line" if m else "close-on token in prose"))
    return out


def check_comment(author: str, body: str) -> None:
    """Raise :class:`CloseOnLineRefused` when ``body`` would be read as a bad close gate."""
    if not TOKEN_RE.search(body or ""):
        return
    a = str(author or "").strip().lower()
    if a.startswith("unverified:"):
        a = a.split(":", 1)[1].strip()
    if a in MACHINE_AUTHORS:
        return
    root = _fleet_root()
    if os.path.exists(root / "state" / "close-on-comment-policy-off"):
        return
    authors, lib = _load("our_authors"), _load("close_on")
    if authors is None or lib is None:
        return
    if authors.is_machine(body):
        return
    bad = violations(body, lib)
    if bad:
        lines = "; ".join(f"{why}: {line!r}" for line, why in bad[:4])
        raise CloseOnLineRefused(
            f"comment refused, the review-merge-pass would read it as a new close gate ({lines}). "
            f"{REFUSAL_HINT}. Valid: `close-on: merged`, `close-on: <cron-id> <text>`, "
            "`close-on: <ISO-8601 with offset> <text>`, or a close-on predicate / `manual:` gate."
        )


# Fallback when the fleet grammar cannot be loaded: the same shape as close_on.LINE_RE. The
# conditional append fails CLOSED, so it needs a line matcher even when the guard above fails open.
_LINE_RE_FALLBACK = re.compile(
    r"^[ \t]*(?:[-*][ \t]+)?[*_`]*close-on[*_`]*[ \t]*:[*_`]*[ \t]*(?P<gate>[^\n]*)$", re.I | re.M)


class ReleaseSuperseded(ValueError):
    """``--if-latest-release <id>``: a newer release-bearing comment exists (or ``<id>`` is unknown)."""


def newer_release_comment(conn, task_id: str, comment_id: int) -> Optional[str]:
    """Why ``comment_id`` is NOT the card's latest release-bearing comment, or None when it is.

    Release-bearing = a ``close-on:`` line (``close_on.LINE_RE``) in a comment that is not
    automation (:data:`MACHINE_AUTHORS`, ``our_authors.is_machine``). Order is the closer's:
    ``(created_at, id)``. Fails closed: an unknown id, one on another card, or an anchor that is
    not itself release-bearing (Prism 1a7315c3ea79) is a refusal.
    Called inside the comment insert's write transaction (Prism 3406ba8bbfd9 / f3498875f176)."""
    row = conn.execute("SELECT task_id, created_at, author, body FROM task_comments WHERE id = ?",
                       (int(comment_id),)).fetchone()
    if row is None or row[0] != task_id:
        return f"comment {comment_id} is not a comment on {task_id}"
    lib, authors = _load("close_on"), _load("our_authors")
    line_re = getattr(lib, "LINE_RE", None) or _LINE_RE_FALLBACK

    def release_bearing(author, body) -> bool:
        a = str(author or "").strip().lower()
        if a.startswith("unverified:"):
            a = a.split(":", 1)[1].strip()
        if a in MACHINE_AUTHORS or (authors is not None and authors.is_machine(body)):
            return False
        return bool(line_re.search(str(body or "")))

    if not release_bearing(row[2], row[3]):
        return f"comment {comment_id} ({row[2]}) is not a release-bearing comment"
    at = int(row[1] or 0)
    for cid, author, body in conn.execute(
            "SELECT id, author, body FROM task_comments WHERE task_id = ? AND id != ? "
            "AND (created_at > ? OR (created_at = ? AND id > ?)) ORDER BY created_at, id",
            (task_id, int(comment_id), at, at, int(comment_id))):
        if release_bearing(author, body):
            return f"comment {cid} ({author}) is a newer release-bearing comment than {comment_id}"
    return None


def refusal(author: str, body: str) -> Optional[str]:
    """:func:`check_comment` for writers that return ``(ok, reason)``: the message, or None."""
    try:
        check_comment(author, body)
    except CloseOnLineRefused as exc:
        return str(exc)
    return None
