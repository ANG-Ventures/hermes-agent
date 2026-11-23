"""Negative-result detection for review handoffs (t_db9ca661).

Under ``kanban.review_policy=milestone_only|none`` a slice card's
``request_review`` completes the card in place. A handoff whose own summary
says the close gate is NOT met / not proven / FAIL must not become ``done``:
t_63322ecc, t_18004327 and t_96adeb3f were false greens Apollo had to reopen.

The pattern mirrors hermes-home ``scripts/kanban-review-merge-pass.py``
``NEG_RESULT_RE`` (the merge-pass half of the same fix, hermes-home#3574).
FAIL/DEFERRED count only as a capitalised verdict/result/gate VALUE: the bare
word matched 385 of 1392 completions in 14 days ("3 new tests fail on base").
"""
from __future__ import annotations

import re
from typing import Any, Optional

_NEG_ACCEPT = (
    r"\bacceptance\s+(?:is\s+|was\s+)?(?:still\s+)?unmet\b|\bnot\s+(?:yet\s+)?met\b"
    r"|\b(?:do\s+not|don'?t)\s+(?:merge|land)\b|\"acceptance_met\"\s*:\s*false"
)
NEG_RESULT_RE = re.compile(
    r"(?i:" + _NEG_ACCEPT + r"|\bnot\s+(?:yet\s+)?proven\b)|\bUNTRUSTED\b"
    r"|(?i:\b(?:verdict|result|gate))\s*[:=]?\s*(?:FAIL(?:ED)?|DEFERRED)\b"
)


# t_1938555e: a match counts only where the worker states a verdict, never where it describes code, a test or
# quoted output ("Any other failure is reported as not proven" left t_c5cbe356's fixed PR a draft with no CI).
# 14 d of review_negative_result (292): "not met" 106/106 real; "not proven" 78 real / 57 false; the FAIL arm
# 14/15 false; "do not merge" 12/14 false. So: quoted text is blanked; "not proven" needs a verdict position;
# merge/land only as an instruction; FAIL only as a line's own label. Every other arm is unchanged.
# A ``` fence (pasted output, closed or running to the end) is quoted too (Prism 5154e0a63aa9).
_QUOTED_RE = re.compile(r'```[\s\S]*?(?:```|\Z)|"[^"\n]*"|`[^`\n]*`|\u201c[^\u201d\n]*\u201d')
# The worker's own verdict label before "not proven": "Result: not proven" (Prism dc37cf6bbd2e).
_VERDICT_LABEL_RE = re.compile(r"(?i)[\s>*#\-\u2022]*(?:result|status|verdict|outcome|conclusion)\s*[:=\-]\s*$")
_PLEASE_RE = re.compile(r"(?i)\bplease\s*$")
_NOT_PROVEN_RE = re.compile(r"(?i)\bnot\s+(?:yet\s+)?proven\b")
_DONT_LAND_RE = re.compile(r"(?i)\b(?:do\s+not|don'?t)\s+(?:merge|land)\b")
_FAIL_ARM_RE = re.compile(r"(?i:\b(?:verdict|result|gate))\s*[:=]?\s*(?:FAIL(?:ED)?|DEFERRED)\b")
# The card's own gate named in the sentence before "not proven": "The done criterion is NOT yet proven".
_GATE_NOUN_RE = re.compile(
    r"(?i)\b(?:accept\w*|done|done-when|card|target|criteri\w*|condition|requirement|goal|item|close\w*|AC\d+)\b"
)
_LINE_LEAD_RE = re.compile(r"[\s>*#\-\u2022]*(?:\(?\w{1,3}[.)]\s*)?$")  # bullets / "1." / "(c)" before the phrase


def _sentence_prefix(text: str, start: int) -> str:
    """The text from the start of ``start``'s sentence (line start or after . ! ? ;) up to ``start``."""
    line_start = text.rfind("\n", 0, start) + 1
    prefix = text[line_start:start]
    cut = max((m.end() for m in re.finditer(r"[.!?;](?:\s|$)", prefix)), default=0)
    return prefix[cut:]


def _verdict_position(text: str, m: re.Match) -> bool:
    phrase = m.group(0)
    prefix = _sentence_prefix(text, m.start())
    if _FAIL_ARM_RE.fullmatch(phrase):
        # "Verdict: FAIL" as the line's own label; "prints RESULT FAIL" / "on verdict=FAIL" describe output.
        line_start = text.rfind("\n", 0, m.start()) + 1
        return bool(_LINE_LEAD_RE.match(text[line_start:m.start()]))
    if _DONT_LAND_RE.fullmatch(phrase):
        # An instruction ("Do not land.", "draft, do not merge"), never a description ("I do not merge").
        prefix = _PLEASE_RE.sub("", prefix)  # "Please do not merge" is the same instruction (Prism 01d8577b1e47)
        return bool(re.search(r"(?:^|[,:(\-])\s*$", prefix)) or bool(_LINE_LEAD_RE.match(prefix))
    if _NOT_PROVEN_RE.fullmatch(phrase):
        if _LINE_LEAD_RE.match(prefix):
            return True  # "Not proven: ...", "NOT YET PROVEN: ...", "- Not proven live"
        if _VERDICT_LABEL_RE.match(prefix):
            return True  # "Result: not proven", "Status: NOT proven"
        if re.match(r"\s*(?:yet|here|live)?\s*:", text[m.end():]):
            return True  # a heading: "What is not proven:"
        return bool(_GATE_NOUN_RE.search(prefix))
    return True


def _first_verdict(text: str) -> Optional[str]:
    # Newlines survive the blanking so line and sentence starts after a fence stay where they were.
    blanked = _QUOTED_RE.sub(lambda q: re.sub(r"[^\n]", " ", q.group(0)), text)
    for m in NEG_RESULT_RE.finditer(blanked):
        if _verdict_position(blanked, m):
            return m.group(0)
    return None


def _acceptance_met_false(value: Any) -> bool:
    if isinstance(value, dict):
        return value.get("acceptance_met") is False or any(_acceptance_met_false(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_acceptance_met_false(v) for v in value)
    return False


def _meta_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _meta_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _meta_strings(v)


def negative_result_match(summary: Optional[str], metadata: Any = None) -> Optional[str]:
    """The matched text when the handoff reports a negative result, else None.

    Scans ``summary`` and each string value of ``metadata``, counting a phrase only in a verdict position (see
    :func:`_verdict_position`); ``"acceptance_met": false`` in the metadata always counts.
    """
    hit = _first_verdict(summary or "")
    if hit is not None:
        return hit
    if _acceptance_met_false(metadata):
        return '"acceptance_met": false'
    if metadata and not isinstance(metadata, (dict, list, tuple, str)):
        return _first_verdict(str(metadata))
    for s in _meta_strings(metadata):
        hit = _first_verdict(s)
        if hit is not None:
            return hit
    return None


DRAFT_NOTE_PING_MAX = 300
_DRAFT_HEAD = "negative result: PR left as-is (a worker draft stays draft); "


def draft_cmds(metadata: Any = None) -> list:
    """``ace_gate.py ready <repo> <n>`` for each of the card's OWN fleet handoff PRs (t_5c6c67c5).

    A NOT-met handback leaves its PR exactly as it is: a worker draft stays draft, which keeps
    self-declared-unfinished work out of every lander until the human lane decides. Ownership is the
    freshness gate's own filter (:func:`kanban_pr_freshness.handoff_refs`): a PR the summary merely
    cites (another card's dependency) never becomes a command (Prism d0bec1125a49). No network read.
    """
    from hermes_cli.kanban_open_pr import split_fleet
    from hermes_cli.kanban_pr_freshness import handoff_refs

    meta = metadata if isinstance(metadata, dict) else None
    return [f"ace_gate.py ready {r.repo} {r.number}" for r in split_fleet(handoff_refs(meta))[0]]


def draft_note_fits(cmds: list, limit: int = DRAFT_NOTE_PING_MAX) -> bool:
    return len(_DRAFT_HEAD + "on accept flip it: " + "; ".join(cmds)) <= limit


def draft_note_line(cmds: list, limit: int = DRAFT_NOTE_PING_MAX) -> Optional[str]:
    """The review-ping line: every command whole, or a count pointing at the card comment. Never a
    sliced command, which could name a different valid PR (Prism 11a86869f533)."""
    if not cmds:
        return None
    if draft_note_fits(cmds, limit):
        return _DRAFT_HEAD + "on accept flip it: " + "; ".join(cmds)
    return _DRAFT_HEAD + f"{len(cmds)} flip commands, see the card"


def draft_note_comment(cmds: list) -> str:
    """Card comment carrying the full command list when the ping line only counts them."""
    return _DRAFT_HEAD + "on accept flip each one:\n" + "\n".join(cmds)
