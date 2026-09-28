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
# handoffs, not "blocked on" / "could not". A negated phrase ("no STOP
# finding", "no longer blocked on") is positive too, and "could not
# reproduce" is the usual wording of a clean flake verdict
# (FleetReview #1447, t_daa1f3bf).
_NEGATED = r"(?<!\bno\s)(?<!\bnot\s)(?<!\bnever\s)(?<!\blonger\s)(?<!\bwithout\s)"
PHRASES = re.compile(
    _NEGATED
    + r"\b(?:NOT\s+DEPLOYED"
    r"|STOP\s+finding"
    r"|could\s+not(?!\s+reproduce)"
    r"|blocked\s+on"
    r"|nothing\s+was\s+(?:armed|measured))\b",
    re.IGNORECASE,
)
PARTIAL_OUTCOMES = frozenset({"partial"})


def handoff_texts(summary: Optional[str], result: Optional[str]) -> tuple:
    """The prose to scan: ``summary`` when present, else the legacy ``result``.

    ``result`` often carries pasted tool/test output (e.g. a "could not find
    module" line from a failure fixed later), so it is only the handoff when
    no summary was written.
    """
    return (summary,) if (summary or "").strip() else (result,)


def match(texts: Iterable[Optional[str]], metadata: Optional[dict] = None) -> Optional[str]:
    """Return the quoted trigger (phrase or ``outcome=partial``) or ``None``."""
    for text in texts:
        if not text:
            continue
        hit = PHRASES.search(str(text))
        if hit:
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
