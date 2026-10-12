"""THE placement reason strings: one module, every reader imports it.

``kanban_worker_pool.portable()`` returns a rule from here, ``_route_row``
turns it into a wait reason, the dispatch printer and the ``placement_wait``
task_event carry the same string. Phase 2 of the traceability-vs-placement
spec (t_1cca39e1 / t_7133e03d) adds values here and deletes ``LINKED``.
"""

from __future__ import annotations

from typing import Any, Mapping

NOT_PORTABLE = "not_portable"

# portable() rules (hard_ok failures), in the order portable() checks them.
WORKSPACE_KIND = "workspace_kind"
LINKED = "linked"
LOCAL_FILES = "local_files"
PROFILE = "profile"
NATIVE_COMMAND = "native_command"
HOST_STUDIO = "host_studio"
STUDIO_BOUND_SKILL = "studio_bound_skill"
HOST_REACH = "host_reach"  # host-bound card, no pool host with a proven ssh route (t_1e9ea7e9)
# A host: line that resolves to no host (t_cb2b888d): held local, never spilled.
PIN_UNKNOWN_HOST = "pin_unknown_host"


def pin_unknown_host(word: str, suggestions=()) -> str:
    """``pin_unknown_host:<word>`` plus `` (did you mean: a, b)``."""
    tail = f" (did you mean: {', '.join(suggestions)})" if suggestions else ""
    return f"{PIN_UNKNOWN_HOST}:{word}{tail}"

# The task_event that records why a ready card was held this tick.
PLACEMENT_WAIT_EVENT = "placement_wait"
# The task_event on a run that was spawned on a pool host (host, reason, band).
WORKER_PLACED_EVENT = "worker_placed"
# The task_event on every spawn, both routes (spec §4.1; written from Phase 2a).
PLACEMENT_EVENT = "placement"
# A repeated identical reason is re-written only after this many seconds.
PLACEMENT_WAIT_REFRESH_SECONDS = 900
# A task_event of one of these kinds after the latest placement_wait ends that
# wait episode: the next hold is a new episode (new event, ``since`` = now),
# never deduplicated against or timed from the old one.
PLACEMENT_WAIT_EPISODE_END_KINDS = frozenset({
    "claimed", "spawned", "blocked", "unblocked", "requeued", "reopened",
    "promoted", "reclaimed", "completed", "review_requested", "scheduled",
    "deferred", "archived",
})

# kanban.placement.lineage_pins (read per tick). ``kinds`` (default): only a
# link kind that can share a workspace holds a card local; ``legacy``: any
# link does, with the bare ``linked`` reason (the pre-t_ae87edff behaviour).
LINEAGE_PINS_KINDS = "kinds"
LINEAGE_PINS_LEGACY = "legacy"
LINEAGE_PINS_DEFAULT = LINEAGE_PINS_KINDS

# Link kinds that never imply a shared workspace. ``derived-from`` is
# provenance only (kanban_db.LINK_KIND_DERIVED_FROM): it never gates, so the
# child never waits on, or builds on, the parent's output. ``blocks`` and a
# NULL kind (read as ``blocks``) stay pinning; an unknown kind fails closed.
PROVENANCE_LINK_KINDS = frozenset({"derived-from"})


# Phase 2a (t_7133e03d): hold codes written by the dispatcher itself.
LOCAL_FULL = "local_full"
POOL_UNAVAILABLE = "pool_unavailable"
UNASSIGNED = "unassigned"
PER_PROFILE_CAP = "per_profile_cap"
# One placement_wait row replaces an A->B->A reason flap inside one refresh
# window (Momus pass 2 R-B); its ``detail`` names the codes seen.
FLAPPING = "flapping"
# Total-budget gates (spec §3.1 step 1): ``total:<gate>`` holds every ready row.
TOTAL = "total"
TOTAL_BUDGET_PAUSED = "budget_paused"
TOTAL_MAX_SPAWN = "max_spawn"
TOTAL_MAX_IN_PROGRESS = "max_in_progress"
TOTAL_MEMORY_CRITICAL = "memory_critical"
TOTAL_MAX_NEW = "max_new"
TOTAL_READY_BUDGET = "ready_budget"

# The record of every spawn, both routes (spec §4.1); its ``route`` values
# live in kanban_db_dispatch (``_ROUTE_*``): routes are not hold reasons.
# ``placement.rule`` values the dispatcher names itself (portable() rules
# above are the rest): ``default`` = portable by policy, placed locally
# because the Studio had allowance; ``unplanned`` = no pool plan this tick.
RULE_PIN = "pin"
RULE_POLICY = "policy"
RULE_DEFAULT = "default"
RULE_UNPLANNED = "unplanned"

# kanban.placement.route_first (read per tick, default true): false restores
# the pre-Phase-2a early returns and the old one-shot gate line.
ROUTE_FIRST_DEFAULT = True


def total(gate: str) -> str:
    """``total:<gate>``."""
    return f"{TOTAL}:{gate}"


def split_reason(reason) -> "tuple[str, str | None]":
    """``(code, detail)`` of a wait reason string (spec §4.2, I-S6).

    The volatile part of a reason lives in ONE trailing parenthesised group
    (``pool_unavailable (ci-box: stale 12s)``, ``pin_host_unknown (...)``,
    ``not_portable:linked(blocks->t_x)``, ``per_profile_cap (alpha 4/4)``);
    the code is everything before it. Dedup compares the code only."""
    text = str(reason or "").strip()
    if text.endswith(")") and "(" in text:
        i = text.index("(")
        return text[:i].rstrip(), text[i + 1:-1] or None
    return text, None


def named_hosts(code: str, detail) -> "str | None":
    """The pool host(s) a reason names, for the dedup key (Momus R-B): a
    host change is a new hold even when the code is the same. Only the
    plan-wide ``pool_unavailable (<host>: <why>; ...)`` names hosts in its
    detail; pin reasons carry their host in the payload already."""
    if code != POOL_UNAVAILABLE or not detail:
        return None
    names = sorted({part.split(":", 1)[0].strip() for part in str(detail).split(";")
                    if ":" in part and part.split(":", 1)[0].strip()})
    return ",".join(names) or None


def route_first(kanban_cfg: Any) -> bool:
    """``kanban.placement.route_first``; anything but an explicit false reads
    as the default (true)."""
    pl = kanban_cfg.get("placement") if isinstance(kanban_cfg, Mapping) else None
    val = pl.get("route_first") if isinstance(pl, Mapping) else None
    if val is None:
        return ROUTE_FIRST_DEFAULT
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() not in ("false", "off", "no", "0")


def not_portable(rule: str) -> str:
    """``not_portable:<rule>``."""
    return f"{NOT_PORTABLE}:{rule}"


def linked(kind: str, other_id: str) -> str:
    """The ``linked`` rule naming the edge that holds the card local."""
    return f"{LINKED}({kind}->{other_id})"


def lineage_pins(kanban_cfg: Any) -> str:
    """``kanban.placement.lineage_pins``; anything unrecognised reads as the
    default, so a typo never silently re-pins every linked card."""
    pl = kanban_cfg.get("placement") if isinstance(kanban_cfg, Mapping) else None
    mode = pl.get("lineage_pins") if isinstance(pl, Mapping) else None
    mode = str(mode).strip().lower() if mode is not None else ""
    return mode if mode in (LINEAGE_PINS_KINDS, LINEAGE_PINS_LEGACY) else LINEAGE_PINS_DEFAULT
