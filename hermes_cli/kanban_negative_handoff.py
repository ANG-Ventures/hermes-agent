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

PHRASES = re.compile(
    r"NOT\s+DEPLOYED"
    r"|STOP\s+finding"
    r"|could\s+not"
    r"|blocked\s+on"
    r"|nothing\s+was\s+(?:armed|measured)",
    re.IGNORECASE,
)
PARTIAL_OUTCOMES = frozenset({"partial"})


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


def route_note(trigger: str) -> str:
    return f'auto-routed: handoff says "{trigger}" — not done; review by {REVIEWER}'
