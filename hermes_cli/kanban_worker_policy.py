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

import os
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
