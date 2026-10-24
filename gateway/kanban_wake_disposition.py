"""Judgment gate for boomerang-wake kanban wakes (Boomerang v2 P3a, spec §5.4, D-13).

Pure logic, no I/O. ``wake_disposition(kind, payload, task, *, retries)`` answers what a
card event's wake should become once ``resolve_boomerang_wake`` says the card is at the
``boomerang wake`` level:

- ``"surface"`` — today's path (passive line + wake turn). Every judgment event, every
  event on a spend/egress card, and every kind this table does not name (INV-5).
- ``"none"``    — no child and no wake change: the merge pass owns a green or pending
  own-repo review handback (pass-1 B3).
- ``"auto"``    — a mechanical event a boomerang child may handle. Only this exact string
  dispatches; a caller must never dispatch on ``!= "surface"`` (pass-3 residue).

Precedence is TIERED (pass-2 C1), not first-match over a flat list: ``_TABLE`` is read in
order and the first matching row wins, and rows are ordered by tier, so a card tag beats
every kind row and the green-PR exclusion beats the own-repo ``auto`` row.

The function takes the RAW event kind (``task_events.kind``). Both wake paths have it:
the notifier iterates events, and every owner-wake item carries ``item["kind"]`` next to
its ``trigger``. The payload is the event payload, plus two keys the caller resolves for a
``review_requested`` handback (this module never reads GitHub):

- ``own_repo``: True when the handback's PR is on a repo whose merges the fleet owns.
  Missing/false → the upstream row (surface).
- ``pr_state``: ``"green"`` | ``"pending"`` | ``"red"`` | ``"dirty"``. Missing or anything
  else on an own repo → surface (fail-safe: never guess a PR is green).
- ``prism_all_answered``: True only when the handback text answers every Prism key open at
  the handback head (``gateway/kanban_prism_answers.py``) and the row's config knob is on.
  Anything else (False / None / missing) leaves today's rows deciding (t_6e9f48cc).

Card tags are read from ``task.tags`` (an iterable) when the caller has one, else from the
card title/body: a ``tags: a, b`` line or a ``[tag]`` marker (the ``[milestone]`` /
``wake: off`` marker shape). A broader match only surfaces more, which is the safe side.
"""
from __future__ import annotations

import re
from typing import Any, Callable, NamedTuple, Optional

AUTO, SURFACE, NONE = "auto", "surface", "none"
VERDICTS = frozenset({AUTO, SURFACE, NONE})
MODE_FORK, MODE_SUMMARY = "fork", "summary"

# Tags whose authority is Ace's (spend / egress / irreversible effects, USER.md).
SURFACE_TAGS = frozenset({"irreversible", "spend", "outbound"})
CRASH_KINDS = frozenset({"crashed", "timed_out", "stalled", "gave_up"})
# D-13: one automatic retry; ``retries`` counts the auto-retries already spent on the card.
MAX_AUTO_RETRIES = 1

_TAGS_LINE_RE = re.compile(r"^\s*tags\s*:\s*(?P<tags>.+?)\s*$", re.I | re.M)
_BRACKET_TAG_RE = re.compile(r"\[\s*(?P<tag>[a-z][a-z0-9_-]*)\s*\]", re.I)


class Disposition(NamedTuple):
    verdict: str            # AUTO | SURFACE | NONE
    mode: Optional[str]     # MODE_FORK | MODE_SUMMARY on an AUTO row, else None
    tier: int
    row: str                # the table row that matched (log/attribution)


class _Ctx(NamedTuple):
    kind: str
    payload: dict
    tags: frozenset
    retries: int


class _Row(NamedTuple):
    tier: int
    name: str
    match: Callable[[_Ctx], bool]
    verdict: str
    mode: Optional[str] = None


def _field(task: Any, name: str) -> Any:
    if task is None:
        return None
    if isinstance(task, dict):
        return task.get(name)
    return getattr(task, name, None)


def card_tags(task: Any) -> frozenset:
    """Lower-cased tags of a card: ``task.tags`` if present, else title/body markers."""
    explicit = _field(task, "tags")
    if explicit is not None and not isinstance(explicit, (str, bytes)):
        return frozenset(str(t).strip().lower() for t in explicit if str(t).strip())
    out: set = set()
    for text in (_field(task, "title"), _field(task, "body"), explicit):
        if not isinstance(text, str) or not text:
            continue
        for m in _TAGS_LINE_RE.finditer(text):
            out.update(t.strip().lower() for t in re.split(r"[,\s]+", m.group("tags")) if t.strip())
        out.update(m.group("tag").lower() for m in _BRACKET_TAG_RE.finditer(text))
    return frozenset(out)


def _is_review(c: _Ctx) -> bool:
    return c.kind == "review_requested"


def _own_review(c: _Ctx) -> bool:
    return _is_review(c) and c.payload.get("own_repo") is True


def _pr_state(c: _Ctx) -> str:
    return str(c.payload.get("pr_state") or "").strip().lower()


# THE table. Ordered by tier; the first matching row wins. Every ``auto`` row names a mode.
_TABLE: tuple = (
    # Tier 1 — card tags beat every kind row.
    _Row(1, "card_tag_surface", lambda c: bool(c.tags & SURFACE_TAGS), SURFACE),
    # Tier 2 — judgment kinds.
    _Row(2, "blocked_needs_input",
         lambda c: c.kind == "blocked" and str(c.payload.get("kind") or "") == "needs_input", SURFACE),
    _Row(2, "blocked", lambda c: c.kind == "blocked", SURFACE),
    _Row(2, "block_loop_detected", lambda c: c.kind == "block_loop_detected", SURFACE),
    # Tier 3 — a mechanical own-repo handback: every open Prism key is answered in the
    # handback text, so posting those lines, the coverage and the enqueue is not a verdict
    # (t_6e9f48cc). Above the green and red rows: the queue owns CI (a red row's existing
    # fm_args such as --allow-red-ci ride along unchanged; a dirty PR refuses at step 3 and
    # the child surfaces). An unanswered key falls through to today's rows.
    _Row(3, "own_repo_review_all_keys_answered",
         lambda c: _own_review(c) and c.payload.get("prism_all_answered") is True, AUTO, MODE_FORK),
    # Tier 3 — the merge pass owns a green/pending own-repo handback.
    _Row(3, "own_repo_review_green_or_pending",
         lambda c: _own_review(c) and _pr_state(c) in ("green", "pending"), NONE),
    # Tier 4 — specific mechanical rows.
    _Row(4, "own_repo_review_red",
         lambda c: _own_review(c) and _pr_state(c) in ("red", "dirty"), AUTO, MODE_FORK),
    _Row(4, "completed", lambda c: c.kind == "completed", AUTO, MODE_FORK),
    _Row(4, "changes_requested", lambda c: c.kind == "changes_requested", AUTO, MODE_FORK),
    _Row(4, "crash_first_occurrence",
         lambda c: c.kind in CRASH_KINDS and c.retries < MAX_AUTO_RETRIES, AUTO, MODE_FORK),
    # t_1e0ffee4: the review deadman's tier-1 page, emitted as a ``review_deadman`` card event
    # (hermes-home scripts/kanban-review-deadman.py) instead of a direct wake. The child does the
    # attended read: a bounded why-waits comment, or surface.
    _Row(4, "review_deadman", lambda c: c.kind == "review_deadman", AUTO, MODE_FORK),
    # Tier 5 — default: upstream handbacks, repeat crashes, reclaimed, skipped_nonspawnable,
    # status/heartbeat plumbing and any kind nobody listed.
    _Row(5, "default", lambda c: True, SURFACE),
)


def classify(kind: str, payload: Optional[dict], task: Any, *, retries: int = 0) -> Disposition:
    """Full disposition (verdict + auto mode + matching row) for one card event."""
    ctx = _Ctx(
        kind=str(kind or "").strip(),
        payload=payload if isinstance(payload, dict) else {},
        tags=card_tags(task),
        retries=max(0, int(retries or 0)),
    )
    for row in _TABLE:
        if row.match(ctx):
            return Disposition(row.verdict, row.mode if row.verdict == AUTO else None, row.tier, row.name)
    raise AssertionError("unreachable: the default row matches everything")


def wake_disposition(kind: str, payload: Optional[dict], task: Any, *, retries: int = 0) -> str:
    """``"auto"`` | ``"surface"`` | ``"none"`` for one card event (spec §5.4)."""
    return classify(kind, payload, task, retries=retries).verdict


def resolve_boomerang_wake(card: Any) -> bool:
    """D-A: the card's own level is the only setting (no session toggle, no board default).

    ``card`` is the card's ``boomerang_wake`` value or the card itself (a Task / dict carrying
    ``boomerang_wake``). Only an explicit on (True / 1 / "true") opts in.
    """
    value = card if card is None or isinstance(card, (bool, int, str)) else _field(card, "boomerang_wake")
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    return bool(value) if isinstance(value, (bool, int)) else False
