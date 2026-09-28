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
# "no longer blocked on". The negator must govern the phrase itself: only
# quantifier/adjective fillers may sit between them, so the "no" in
# "no workaround for STOP finding" (it negates "workaround") does not
# suppress the match (FleetReview #1447 @aa9e59a3).
_NEGATION_FILLERS = (
    r"new|more|further|other|additional|remaining|outstanding|open|real"
    r"|actual|single|remotely|longer|any|such|genuine"
)
_NEGATED_TAIL = re.compile(
    r"\b(?:no|not|never|without|longer)(?:\s+(?:" + _NEGATION_FILLERS + r")){0,2}\s+$",
    re.IGNORECASE,
)
PARTIAL_OUTCOMES = frozenset({"partial"})


# ``result`` also carries pasted tool/test output (e.g. a "could not find
# module" traceback fixed later). Lines shaped like pasted output are skipped;
# every other result line is the worker's own prose and is scanned
# (FleetReview #1447 @aa9e59a3: a verdict on line 2 closed the card done).
_FENCE = re.compile(r"^\s*(?:```|~~~)")
# "<program>[pid]: message" diagnostics (``ssh: Could not resolve hostname``,
# ``curl: (6) ...``, git's ``fatal:``/``error:``). A closed, lowercase,
# case-sensitive name list: a worker's own "status: blocked on ..." or
# "Verdict: could not ..." is not a program name and is still scanned
# (FleetReview #1464 @03d1cc77).
_TOOL_PREFIX = (
    r"\s*(?:[\w.~-]*/)*"
    r"(?:ssh|scp|sftp|rsync|git|gh|curl|wget|docker|kubectl|make|npm|npx|pnpm|yarn"
    r"|pip\d?|uv|python[\d.]*|node|bash|sh|zsh|sudo|cp|mv|rm|ln|mkdir|ls|cat|chmod"
    r"|chown|tar|unzip|launchctl|systemctl|journalctl|brew|apt(?:-get)?|sqlite3|psql"
    r"|ping|nc|dig|hermes|fatal|error|warning|hint|remote)"
    r"(?:\[\d+\])?:\s"
)
_LOG_LINE = re.compile(
    r"^(?:\s{2,}|\t"                               # indented block / traceback frame
    r"|\s*[>$]\s"                                   # quote, shell prompt
    r"|\s*Traceback\b"
    r"|" + _TOOL_PREFIX +                            # ssh: / curl: / fatal: ...
    r"|\s*(?:[\w.-]+:\s*)?[\w.]+(?:Error|Exception|Warning):\s"  # [tool: ]ImportError: ...
    r"|\s*E\s{2,}"                                    # pytest E-lines
    r"|\s*(?:ERROR|WARN(?:ING)?|INFO|DEBUG|CRITICAL|FATAL)\b"  # log level prefix
    r"|\s*\[?\d{1,4}[-:/]\d{1,2}[-:/]\d{1,4}"         # timestamp / date prefix
    r"|\s*\[\d{1,2}:\d{2})"                              # [HH:MM...] prefix
)


def _verdict_lines(result: Optional[str]) -> str:
    """``result`` minus fenced blocks and lines shaped like pasted output."""
    kept: list[str] = []
    in_fence = False
    for line in (result or "").splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or not line.strip() or _LOG_LINE.match(line):
            continue
        kept.append(line)
    return "\n".join(kept)


def handoff_texts(summary: Optional[str], result: Optional[str]) -> tuple:
    """The prose to scan: ``summary`` plus every verdict line of ``result``.

    Pasted output (fenced, indented, quoted, exception/log/timestamp-prefixed
    lines) is dropped; see :data:`_LOG_LINE`.
    """
    return (summary, _verdict_lines(result))


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
