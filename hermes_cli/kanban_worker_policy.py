"""Fan-out brake: cards created BY a dispatched worker park for a human.

Incident 2026-09-22: ~200 human-carded alert-remediation items became ~730
worked cards / ~$13K in two days. The mechanism was not the split-on-new-defect
rule (that stays) — it was that a dispatched worker's ``kanban_create`` landed a
card in ``ready``, the dispatcher picked it up on the next tick, and that worker
created more. Unbounded auto-dispatch of worker-created cards, with no human in
the loop.

The brake: a card created by a *dispatched worker* lands in
``kanban.worker_created_status`` (default ``triage``) instead of ``ready``.
``triage`` is the one status no automation ever clears, so a human promotes the
card. Setting the knob to ``ready`` restores the legacy behaviour exactly.

**The worker marker is ``HERMES_KANBAN_TASK``, not the profile name.** The
dispatcher injects that env var (the worker's OWN card id) into every worker it
spawns, and only into workers. Keying on the profile name would be wrong in both
directions: an orchestrator profile can also be dispatched as a worker, and a
worker profile can legitimately be driven interactively by a human.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

# Statuses whose creation intent is "queue this for work". Only these are
# overridden — an explicit ``blocked`` (the human-ops/R3 gate) or an already
# parked card is the caller saying something specific, and is left alone.
_DEFAULT_CREATE_STATUSES = {"running", "ready", "todo"}

DEFAULT_WORKER_CREATED_STATUS = "triage"

WORKER_ENV_MARKER = "HERMES_KANBAN_TASK"


def _load_config() -> dict:
    """Indirection so tests can pin config without touching the real home."""
    from hermes_cli.config import load_config

    return load_config() or {}


def is_dispatched_worker() -> bool:
    """True when this process is a dispatcher-spawned Kanban worker.

    Keyed on ``HERMES_KANBAN_TASK`` — see the module docstring for why the
    profile name is the wrong signal.
    """
    return bool((os.environ.get(WORKER_ENV_MARKER) or "").strip())


def configured_worker_created_status() -> str:
    """Resolve ``kanban.worker_created_status``, falling back to ``triage``.

    Fails toward the BRAKE: an unreadable config or an invalid status yields
    the default park rather than silently restoring unbounded auto-dispatch.
    """
    try:
        cfg = _load_config()
        value = (cfg.get("kanban", {}) or {}).get("worker_created_status")
    except Exception:
        return DEFAULT_WORKER_CREATED_STATUS
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_WORKER_CREATED_STATUS
    candidate = value.strip()
    from hermes_cli.kanban_db import VALID_STATUSES

    if candidate not in VALID_STATUSES:
        return DEFAULT_WORKER_CREATED_STATUS
    return candidate


def resolve_park_status(
    *, initial_status: Optional[str], triage: bool = False,
) -> Optional[str]:
    """Return the status to FORCE this creation into, or None to leave as-is.

    ``None`` means "no policy override" — the caller's normal parent-gated
    status resolution applies unchanged. That is the answer for every
    non-worker caller, for an explicit ``initial_status=blocked``, for
    ``triage=True`` (already parked), and when the knob is set to a status the
    normal path would reach anyway.
    """
    if not is_dispatched_worker():
        return None
    if triage:
        return None
    requested = (initial_status or "running").strip() or "running"
    if requested not in _DEFAULT_CREATE_STATUSES:
        return None
    target = configured_worker_created_status()
    if target in _DEFAULT_CREATE_STATUSES:
        # "ready"/"todo"/"running" is the legacy behaviour: nothing to force.
        return None
    return target


def park_event_payload(status: str) -> dict:
    """Audit payload for the ``parked_by_policy`` task_event."""
    return {
        "status": status,
        "knob": "kanban.worker_created_status",
        "reason": (
            f"parked_by_policy: worker-created cards start in {status} "
            "(kanban.worker_created_status)"
        ),
        "worker_task_id": (os.environ.get(WORKER_ENV_MARKER) or "").strip() or None,
    }


# ``default`` is the operator's own profile (Apollo). A dispatched worker that
# passes ``assignee="default"`` is almost always a model filling the required
# field with a placeholder, not a real routing decision: six such cards landed
# in 24h on 2026-09-24 (t_4424c05d). Operator/CLI creates are untouched.
RESERVED_WORKER_ASSIGNEES = frozenset({"default"})


def configured_default_assignee() -> str:
    """Resolve ``kanban.default_assignee``; empty string when unset/unreadable."""
    try:
        cfg = _load_config()
        value = (cfg.get("kanban", {}) or {}).get("default_assignee")
    except Exception:
        return ""
    if not isinstance(value, str):
        return ""
    return value.strip()


def resolve_worker_assignee(
    assignee: str, *, parent_lane: Optional[str] = None,
) -> tuple[str, Optional[dict], Optional[str]]:
    """Rewrite a worker's placeholder assignee to ``kanban.default_assignee``.

    Returns ``(assignee, remap_payload, error)``:

    - not a dispatched worker, or a normal assignee: ``(assignee, None, None)``
    - worker passed ``default`` and the knob names another profile:
      ``(configured, payload, None)`` where ``payload`` is the audit record
    - worker passed ``default`` and the knob is unset (or is ``default``):
      ``(assignee, None, error)``, and the caller refuses the create.
    """
    requested = str(assignee).strip()
    if not is_dispatched_worker() or not is_placeholder_assignee(requested):
        return str(assignee), None, None
    lane = (parent_lane or "").strip()
    if lane and not is_placeholder_assignee(lane):
        # Minted under an already-ruled parent: the child rides the parent's
        # assignee lane, never a human/operator placeholder (r16 K).
        return lane, {
            "from": requested,
            "to": lane,
            "knob": "ruled_parent_lane",
            "reason": (
                f"worker-created card asked for placeholder assignee="
                f"{requested!r}; remapped to the ruled parent's lane {lane!r}"
            ),
            "worker_task_id": (os.environ.get(WORKER_ENV_MARKER) or "").strip() or None,
        }, None
    configured = configured_default_assignee()
    if not configured or is_placeholder_assignee(configured):
        return str(assignee), None, (
            f"assignee={requested!r} is a placeholder/operator lane and "
            "cannot be used by a dispatched worker, and kanban.default_assignee "
            "is not set to another profile. Name the specialist profile that "
            "should run this card."
        )
    return configured, {
        "from": requested,
        "to": configured,
        "knob": "kanban.default_assignee",
        "reason": (
            f"worker-created card asked for assignee={requested!r}; remapped to "
            f"kanban.default_assignee={configured!r}"
        ),
        "worker_task_id": (os.environ.get(WORKER_ENV_MARKER) or "").strip() or None,
    }, None


# ---------------------------------------------------------------------------
# r16 K: children minted under an already-ruled parent skip the triage park.
#
# Incident 2026-09-29: every card a worker split off a card Apollo had already
# ruled on (Prism W4a-d, t_99d05f73, t_a88d7ab0 with assignee 'apollo') landed
# in ``triage`` with no run and nothing to rule, and Apollo hand-resolved each
# one to ``todo`` every sweep. The rule: a worker-created child whose parent's
# ruling is recorded lands in ``todo`` on the parent's assignee lane. Only a
# body carrying an explicit ``NEEDS RULING:`` line still parks in triage.
# ---------------------------------------------------------------------------

# Profiles that are a human/operator lane or a model's filler, never a worker
# that the dispatcher can usefully spawn for a worker-created card.
PLACEHOLDER_ASSIGNEES = RESERVED_WORKER_ASSIGNEES | frozenset({
    "apollo", "aegis", "human", "operator", "reviewer", "worker",
    "assignee", "tbd", "none", "null", "unassigned",
})

# Comment authors whose comment on a parent IS a recorded ruling (Apollo runs
# as the ``default`` profile; Ace's own comments are authored ``user``/``ace``).
RULING_AUTHORS = frozenset({"default", "apollo", "ace", "user"})

# A parent that is itself queued or being worked was put there by a ruling.
RULED_PARENT_STATUSES = frozenset({"ready", "running"})

RULED_MINT_EVENT = "minted_under_ruled_parent"

_NEEDS_RULING_RE = re.compile(r"^[ \t>*_-]*NEEDS RULING:", re.MULTILINE)


def is_placeholder_assignee(name: Optional[str]) -> bool:
    """True for a human/operator/placeholder lane (``apollo``, ``human:x``...)."""
    value = (name or "").strip().lower()
    if not value:
        return True
    return value in PLACEHOLDER_ASSIGNEES or value.startswith("human:")


def body_needs_ruling(body: Optional[str]) -> bool:
    """True when the card body carries an explicit ``NEEDS RULING:`` line."""
    return bool(body) and bool(_NEEDS_RULING_RE.search(body))


def find_ruled_parent(conn, parents) -> Optional[dict]:
    """Return the first parent whose ruling is recorded, else None.

    A parent is ruled when (in order):

    1. it carries a comment by an operator/Ace author (:data:`RULING_AUTHORS`);
    2. it was itself minted under a ruled parent by THIS SAME worker run
       (sibling chains such as W4a -> W4b inherit the one ruling);
    3. it is ``ready``/``running`` -- unless it was minted by this rule, so a
       bypass-born card's own children park again. That bounds automatic
       dispatch to one generation per human ruling (the 2026-09-22 fan-out
       brake stays intact for recursive worker trees).
    """
    worker = (os.environ.get(WORKER_ENV_MARKER) or "").strip()
    authors = sorted(RULING_AUTHORS)
    for pid in parents or ():
        row = conn.execute(
            "SELECT id, status, assignee FROM tasks WHERE id = ?", (pid,),
        ).fetchone()
        if row is None:
            continue
        ruling = conn.execute(
            "SELECT id, author FROM task_comments WHERE task_id = ? AND "
            "lower(author) IN (" + ",".join("?" * len(authors)) + ") "
            "ORDER BY id LIMIT 1",
            (pid, *authors),
        ).fetchone()
        base = {"parent": row["id"], "parent_status": row["status"],
                "parent_assignee": row["assignee"]}
        if ruling is not None:
            return {**base, "why": "ruling_comment",
                    "comment_id": ruling["id"], "ruled_by": ruling["author"]}
        bypass = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
            "ORDER BY id LIMIT 1",
            (pid, RULED_MINT_EVENT),
        ).fetchone()
        if bypass is not None:
            try:
                minted_by = (json.loads(bypass["payload"] or "{}") or {}).get(
                    "worker_task_id"
                )
            except (TypeError, ValueError):
                minted_by = None
            if worker and minted_by == worker:
                return {**base, "why": "sibling_of_ruled_mint"}
            continue
        if row["status"] in RULED_PARENT_STATUSES:
            return {**base, "why": "parent_status"}
    return None


def resolve_ruled_mint(conn, *, parents, body: Optional[str]) -> Optional[dict]:
    """Decide whether a worker-created, policy-parked card should be ``todo``.

    Returns the ruled-parent record (see :func:`find_ruled_parent`) when the
    card skips triage, or None to keep the park. Never overrides an explicit
    ``NEEDS RULING:`` line, and never applies to a parentless card.
    """
    if not is_dispatched_worker() or not parents:
        return None
    if body_needs_ruling(body):
        return None
    return find_ruled_parent(conn, parents)


def ruled_mint_event_payload(ruled: dict) -> dict:
    """Audit payload for the ``minted_under_ruled_parent`` task_event."""
    return {
        **ruled,
        "status": "todo",
        "rule": "r16 K: child of a ruled parent -> todo on the parent lane; "
                "only an explicit 'NEEDS RULING:' line parks in triage",
        "worker_task_id": (os.environ.get(WORKER_ENV_MARKER) or "").strip() or None,
    }
