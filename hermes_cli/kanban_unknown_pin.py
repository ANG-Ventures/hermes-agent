"""One card comment per UNKNOWN ``host:`` pin (t_cb2b888d).

A ``host:<word>`` line that resolves to no host (a typo, a name the registry
does not carry) used to be dropped silently and the card went portable:
2026-10-11 t_cccdee67 (``host:mac-studio-m3u``) spilled to ace-ai and 403'd.
The dispatcher now holds such a card local-only and posts ONE comment per
``(task_id, word)`` naming the word and the closest real pins. The latch is
the comment row itself (author ``dispatcher``, fixed prefix): it survives a
gateway restart, and a different word on the same card is a new comment.
"""

from __future__ import annotations

from typing import Mapping

AUTHOR = "dispatcher"
_PREFIX = "unknown host pin: host:"


def _body(word: str, suggestions) -> str:
    return (f"{_PREFIX}{word} is no pin the kanban pool can place (not any, the Studio or a pool host), so this card is "
            f"held LOCAL-only (never spilled to the pool). Closest pins: "
            f"{', '.join(suggestions) or '-'}. Fix the host: line to re-route it.")


def comment_unknown_pins(conn, unknown: Mapping[str, str]) -> list:
    """Comment each ``{task_id: word}`` not yet commented; returns the ids posted."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_worker_pool as _kwp

    fleet = _kb.kanban_home() / "fleet"
    known = aliases = None
    posted = []
    for tid, word in (unknown or {}).items():
        if not word:
            continue
        if conn.execute(
                "SELECT 1 FROM task_comments WHERE task_id = ? AND author = ? "
                "AND substr(body, 1, ?) = ? LIMIT 1",
                (tid, AUTHOR, len(_PREFIX) + len(word) + 1, f"{_PREFIX}{word} ")).fetchone():
            continue
        if known is None:
            known = _kwp.known_host_ids(fleet)
            aliases = _kwp.pin_aliases(fleet)
        _kb.add_comment(conn, tid, AUTHOR, _body(word, _kwp.pin_suggestions(word, known, aliases)))
        _kb._log.warning("kanban dispatch: card %s: unknown host pin %s, held local-only", tid, word)
        posted.append(tid)
    return posted
