"""Negative-handoff gate: a completion that SAYS it did not land is not ``done``.

Incidents 2026-09-28: t_d0aee724 completed with "NOT DEPLOYED, so nothing was
armed or measured" and t_b6eb2944 with "STOP finding: ... measures nothing".
Both went through the worker's own ``kanban_complete`` (``completed`` event,
no PR, so the open-PR route never fired) and closed ``done``; Apollo found the
first one 2.5 h later. :func:`hermes_cli.kanban_db.complete_task` asks
:func:`match` and, when the board knob ``kanban.negative_handoff_review`` is
on, routes the card to ``review`` (``human:apollo``) with the phrase quoted.

The phrase set is deliberately small; widening it is a tested change.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

REVIEWER = "human:apollo"
SETTING = "negative_handoff_review"

# Word-bounded on both sides: "unblocked once" / "could notify" are positive
# handoffs, not "blocked on" / "could not". "could not reproduce" is the usual
# wording of a clean flake verdict (FleetReview #1447, t_daa1f3bf).
PHRASES = re.compile(
    r"\b(?:NOT\s+DEPLOYED"
    r"|STOP\s+finding"
    r"|could\s+not(?!\s+reproduce)"
    r"|blocked\s+on"
    r"|nothing\s+was\s+(?:armed|measured))\b",
    re.IGNORECASE,
)
# A negated phrase is positive: "no STOP finding", "no new STOP finding",
# "no longer blocked on". The negator may sit up to two words before it.
_NEGATED_TAIL = re.compile(
    r"\b(?:no|not|never|without|longer)(?:\s+[\w-]+){0,2}\s+$", re.IGNORECASE,
)
PARTIAL_OUTCOMES = frozenset({"partial"})


def handoff_texts(summary: Optional[str], result: Optional[str]) -> tuple:
    """The prose to scan: ``summary`` plus the headline (first line) of ``result``.

    ``result`` is a short result line, but workers also paste tool/test output
    into it (e.g. a "could not find module" traceback fixed later). Its first
    line is the verdict; later lines are treated as quoted logs.
    """
    headline = next((ln for ln in (result or "").splitlines() if ln.strip()), "")
    return (summary, headline)


def match(texts: Iterable[Optional[str]], metadata: Optional[dict] = None) -> Optional[str]:
    """Return the quoted trigger (phrase or ``outcome=partial``) or ``None``."""
    for text in texts:
        if not text:
            continue
        text = str(text)
        for hit in PHRASES.finditer(text):
            if not _NEGATED_TAIL.search(text[max(0, hit.start() - 48):hit.start()]):
                return hit.group(0)
    if isinstance(metadata, dict):
        outcome = metadata.get("outcome")
        if isinstance(outcome, str) and outcome.strip().casefold() in PARTIAL_OUTCOMES:
            return f"outcome={outcome.strip()}"
    return None


def routed_summary(note: str, summary: Optional[str], result: Optional[str]) -> str:
    """The review handoff keeps the note, the summary AND the result (deduped)."""
    parts: list[str] = [note]
    for text in (summary, result):
        if text and text.strip() and text not in parts:
            parts.append(text)
    return "\n".join(parts)


def route_note(trigger: str) -> str:
    return f'auto-routed: handoff says "{trigger}" — not done; review by {REVIEWER}'
