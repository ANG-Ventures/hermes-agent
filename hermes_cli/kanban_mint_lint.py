"""Mint-time lint for the kanban create surfaces (CLI + ``kanban_create`` tool).

Two rules that lived only as prose in the kanban-orchestrator skill and were
broken again on 2026-10-12 (t_6d1df155):

* TIME GATE. t_e1319480 said "BLOCKED UNTIL ... 24 h of shadow have elapsed"
  but was minted with a ``blocks`` edge; the edge released it one minute into
  the window. A card that reads time-gated must be minted ``--until <when>``
  (parked ``scheduled``, woken by ``wake_due_scheduled``) or say
  ``--i-mean-blocks``.
* OPEN DUPLICATE. t_22499911 / t_07450218 re-filed Momus passes that were
  still open. The 24 h near-duplicate guard (``find_near_duplicates``) only
  refuses an IDENTICAL title; this adds a no-window check against OPEN cards:
  token-set Jaccard >= :data:`OPEN_DUP_TITLE_THRESHOLD` over the first
  :data:`OPEN_DUP_TITLE_CHARS` chars of the title, or the same
  ``Momus pass N`` + spec name.

Series siblings are not duplicates: two titles that each carry a number the
other lacks ("Phase 2" vs "Phase 3", "SHARD 2/4" vs "3/4") skip the ratio
rule. Measured on the live board 2026-10-12: of 42 open pairs >= 0.6, every
one was either idempotency-keyed automation (skipped, as in the 24 h guard)
or such a series sibling.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Iterable, Optional

TIME_GATE_RE = re.compile(
    r"\b(after|once|until|when)\b.{0,40}"
    r"\b(\d+\s*(h|hr|hours?|d|days?|min)\b|window (closes|ends)|shadow|elapsed"
    r"|tomorrow|\d{4}-\d\d-\d\d)",
    re.IGNORECASE,
)
TIME_GATE_REFUSAL = (
    "this card reads time-gated; mint it --kind deferred --until <when> "
    "(the blocks edge releases it the instant the parent closes)"
)

OPEN_DUP_TITLE_THRESHOLD = 0.6
OPEN_DUP_TITLE_CHARS = 80
OPEN_DUP_MIN_TOKENS = 3
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MOMUS_RE = re.compile(
    r"^Momus pass (\d+)\b.*?:\s*(.+?)(?:\bspec\b|—|\(|$)", re.IGNORECASE,
)


class TimeGateError(ValueError):
    """A ``blocks``-edge card whose text reads time-gated, minted without ``until``."""

    def __init__(self, message: str, match: str):
        super().__init__(message)
        self.match = match


def _strip_origin(body: Optional[str]) -> str:
    return "\n".join(
        line for line in (body or "").splitlines()
        if not line.strip().lower().startswith("origin:")
    )


def time_gate_match(title: Optional[str], body: Optional[str]) -> Optional[str]:
    """The text that makes this card read time-gated, or None."""
    for text in (title or "", _strip_origin(body)):
        m = TIME_GATE_RE.search(text)
        if m:
            return m.group(0)
    return None


def _head_tokens(title: Optional[str]) -> frozenset:
    return frozenset(_TOKEN_RE.findall((title or "")[:OPEN_DUP_TITLE_CHARS].lower()))


_DATE_RE = re.compile(r"\d{4}-\d\d-\d\d")


def _series_numbers(title: Optional[str]) -> frozenset:
    """Pure-digit tokens of the title head, dates removed (a re-file under a
    new date is still a duplicate; a hex id is not a series number)."""
    head = _DATE_RE.sub(" ", (title or "")[:OPEN_DUP_TITLE_CHARS])
    return frozenset(t for t in _TOKEN_RE.findall(head.lower()) if t.isdigit())


def momus_pair(title: Optional[str]) -> Optional[tuple[str, tuple[str, ...]]]:
    """``(pass number, normalized spec-name tokens)`` for a Momus pass title."""
    m = _MOMUS_RE.match((title or "").strip())
    if not m:
        return None
    name = tuple(_TOKEN_RE.findall(m.group(2).lower()))
    return (m.group(1), name) if name else None


def find_open_duplicates(
    conn: sqlite3.Connection,
    *,
    title: str,
    tenant: Optional[str] = None,
    exclude_ids: Iterable[str] = (),
) -> list[dict]:
    """Open (not done/archived) cards this title duplicates, best first.

    ``[{"id", "title", "status", "assignee", "score", "rule"}]``; ``rule`` is
    ``title_ratio`` or ``momus_pass``. ``exclude_ids`` are the new card's own
    parents: a child that resembles the card it hangs off is linked, not lost.
    """
    new_toks = _head_tokens(title)
    new_nums = _series_numbers(title)
    new_momus = momus_pair(title)
    if len(new_toks) < OPEN_DUP_MIN_TOKENS and new_momus is None:
        return []
    skip = set(exclude_ids)
    rows = conn.execute(
        "SELECT id, title, status, assignee FROM tasks "
        "WHERE status NOT IN ('done', 'archived') AND tenant IS ?",
        (tenant,),
    ).fetchall()
    hits: list[dict] = []
    for row in rows:
        if row["id"] in skip:
            continue
        rule = None
        old_toks = _head_tokens(row["title"])
        old_nums = _series_numbers(row["title"])
        union = new_toks | old_toks
        score = len(new_toks & old_toks) / len(union) if union else 0.0
        if new_momus is not None and momus_pair(row["title"]) == new_momus:
            rule = "momus_pass"
        elif (
            len(new_toks) >= OPEN_DUP_MIN_TOKENS
            and score >= OPEN_DUP_TITLE_THRESHOLD
            # Each side names a number the other lacks: a series sibling.
            and not ((new_nums - old_nums) and (old_nums - new_nums))
        ):
            rule = "title_ratio"
        if rule is None:
            continue
        hits.append({
            "id": row["id"], "title": row["title"], "status": row["status"],
            "assignee": row["assignee"], "score": round(score, 3), "rule": rule,
        })
    hits.sort(key=lambda h: (h["rule"] != "momus_pass", -h["score"]))
    return hits


def open_duplicate_message(hits: list[dict]) -> str:
    best = hits[0]
    rule = (
        "same Momus pass + spec" if best["rule"] == "momus_pass"
        else f"title similarity {best['score']:.2f} on the first {OPEN_DUP_TITLE_CHARS} chars"
    )
    return (
        f"duplicate of open card {best['id']} (status {best['status']}, assignee "
        f"{best['assignee'] or '-'}; {rule}): {best['title'][:100]!r} -- comment on it "
        "instead, or re-run with --not-a-duplicate \"<why>\" / not_a_duplicate to file anyway"
    )
