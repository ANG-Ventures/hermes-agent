"""THE placement reason strings: one module, every reader imports it.

``kanban_worker_pool.portable()`` returns a rule from here, ``_route_row``
turns it into a wait reason, the dispatch printer and the ``placement_wait``
task_event carry the same string. Phase 2 of the traceability-vs-placement
spec (t_1cca39e1 / t_7133e03d) adds values here and deletes ``LINKED``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Mapping

_log = logging.getLogger(__name__)

NOT_PORTABLE = "not_portable"

# portable() rules (hard_ok failures), in the order portable() checks them.
WORKSPACE_KIND = "workspace_kind"
# Phase 2b-i (t_6d5bc652): the workspace binds that replace the link input.
# ``workspace_of:<id>`` = the card works IN <id>'s directory (explicit field);
# ``workspace_shared:<id>`` = another live card has the same canonical path,
# or a task-owned scratch/worktree path this card's path is nested under.
WORKSPACE_OF = "workspace_of"
WORKSPACE_SHARED = "workspace_shared"
# ``placement.rule`` of a row a bind pinned to the bound card's pool host.
BIND_HOST = "bind_host"
# Event on a card whose workspace_of target is terminal with no local path.
PLACEMENT_BIND_DROPPED_EVENT = "placement_bind_dropped"
# Kill-switch rule only (``lineage_pins: legacy | kinds``): a link holds the
# card local. Never produced under ``none``.
LINKED = "linked"
LOCAL_FILES = "local_files"
PROFILE = "profile"
NATIVE_COMMAND = "native_command"
HOST_STUDIO = "host_studio"
STUDIO_BOUND_SKILL = "studio_bound_skill"
HOST_REACH = "host_reach"  # host-bound card, no pool host with a proven ssh route (t_1e9ea7e9)
# A host: line that resolves to no host (t_cb2b888d): held local, never spilled.
PIN_UNKNOWN_HOST = "pin_unknown_host"


# Access-bound (spec v1.2 §2.6, Phase 2c, t_e99ef75c): the WORK is about host X
# or the board, the ACCESS exists only on the Studio. Local, named, never spills.
ACCESS_BOUND = "access_bound"
ACCESS_SSH_ROOT = "ssh-root"                      # ``ssh <alias> ... sudo ...``
ACCESS_CONTROL_PLANE_READ = "control-plane-read"  # board DBs, gateway.log, load gate, pool file


def access_bound(cap: str) -> str:
    """``access_bound:<cap>``."""
    return f"{ACCESS_BOUND}:{cap}"


def ssh_root(host: str) -> str:
    """``ssh-root:<host>``."""
    return f"{ACCESS_SSH_ROOT}:{host}"


def access_pin_contradiction(cap: str, pin: str) -> str:
    """The §5 contradiction comment for an access-bound card pinned to a pool host."""
    return f"access {cap} is Studio-only; remove host:{pin} or run it from the Studio"


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
# never deduplicated against or timed from the old one. ``assigned`` is one:
# unassigned/per_profile_cap are keyed on the assignee, so a (re)assignment is
# a reason boundary (Prism 57fba9daaa3c).
PLACEMENT_WAIT_EPISODE_END_KINDS = frozenset({
    "claimed", "spawned", "blocked", "unblocked", "requeued", "reopened",
    "promoted", "reclaimed", "completed", "review_requested", "scheduled",
    "deferred", "archived", "assigned",
})

# kanban.placement.lineage_pins (read per tick). ``kinds`` (default): only a
# link kind that can share a workspace holds a card local; ``legacy``: any
# link does, with the bare ``linked`` reason (the pre-t_ae87edff behaviour);
# ``none``: links never hold a card (spec Phase 2b-i), only the workspace
# binds do. An UNKNOWN value reads as ``legacy`` (the safe side of a kill
# switch: pin local), with one WARN per value per 5 min. Placement only:
# retention has its own switch (kanban.workspace.retention_key).
LINEAGE_PINS_KINDS = "kinds"
LINEAGE_PINS_LEGACY = "legacy"
LINEAGE_PINS_NONE = "none"
LINEAGE_PINS_MODES = (LINEAGE_PINS_KINDS, LINEAGE_PINS_LEGACY, LINEAGE_PINS_NONE)
# ``none`` becomes the default only after P0-7 (spec §7 Phase 0) posts a
# pool-profile blocks rate at or under 1%.
LINEAGE_PINS_DEFAULT = LINEAGE_PINS_KINDS
LINEAGE_PINS_UNKNOWN = LINEAGE_PINS_LEGACY
UNKNOWN_SWITCH_WARN_SECONDS = 300
_unknown_switch_warned: Dict[tuple, float] = {}

# Link kinds that never imply a shared workspace. ``derived-from`` is
# provenance only (kanban_db.LINK_KIND_DERIVED_FROM): it never gates, so the
# child never waits on, or builds on, the parent's output. ``blocks`` and a
# NULL kind (read as ``blocks``) stay pinning; an unknown kind fails closed.
PROVENANCE_LINK_KINDS = frozenset({"derived-from"})
# Link kinds that hold only the CHILD (t_250a7fc5): ``after-merge`` gates the
# child on the parent's PR merging; the parent owes the child nothing, so its
# own outgoing edge never constrains where it runs. t_041ad167 sat 20 h in
# ``not_portable:linked(after-merge->t_84e763a3)`` behind its own child.
CHILD_ONLY_LINK_KINDS = frozenset({"after-merge"})


# Phase 2a (t_7133e03d): hold codes written by the dispatcher itself.
LOCAL_FULL = "local_full"
# What bound a ``local_full`` hold (t_f73c65a8), so "local_full while a host has
# headroom" is checkable from the event alone: ``gate`` = the host load gate
# paused or allowed no new local worker this tick; ``quota`` = the gate had
# room but this board's share of it was spent (by other boards, by this
# board's own spawns this tick, or held back for a review row).
LOCAL_BINDING_GATE = "gate"
LOCAL_BINDING_QUOTA = "quota"
# The board-split pause the gateway writes when other boards spent the shared
# allowance; classified as ``quota``, every other pause as ``gate``.
QUOTA_SPENT_PREFIX = "load gate: this tick's allowance of "
POOL_UNAVAILABLE = "pool_unavailable"
POOL_FULL = "pool_full"
BOARD_UNHOSTABLE = "board_unhostable"

# The ONE vocabulary of ``SpilloverPlan.take()`` refusals for an UNPINNED row
# (Control-Plane Protection spec v0.3, Momus pass 3 R4). The placement record's
# ``take_refusal`` carries one of these codes (plus an optional ``(detail)``),
# and the I-1 closeout query (kanban_placement_gates) is generated from these
# sets: a code outside them is a violation, never silently allowed. Capacity =
# no free slot that serves the row; structural = no pool host may take this row
# at all (reach, board root). "Profile served on no free host" is reported as
# capacity (``pool_full`` / ``pool_unavailable``): take() has no code for it.
TAKE_CAPACITY = frozenset({POOL_FULL, POOL_UNAVAILABLE})
TAKE_STRUCTURAL = frozenset({HOST_REACH, BOARD_UNHOSTABLE})
TAKE_REFUSALS = TAKE_CAPACITY | TAKE_STRUCTURAL
# ``placement.reason`` of a portable row the Studio spawned after take()
# refused it this tick (spec v0.3 I-2: last resort means after the pool).
LAST_RESORT = "last-resort"
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
RULE_ORIGIN = "origin"  # continuation affinity: the card's state is on that host (t_8bfdcc07)
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


def keyed_detail(code: str, detail) -> "str | None":
    """The part of ``detail`` that IS the reason, for the dedup key (Prism
    01bdaad47969): ``not_portable:linked(blocks->t_x)`` is held by that edge,
    so a different edge is a new hold, never a 900 s duplicate. Grouping
    still reads the code alone (``not_portable:linked``)."""
    return detail or None if code == not_portable(LINKED) else None


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


def local_full_binding(spawn_paused, local_budget, ready_local, local_spawned) -> "dict | None":
    """``{binding, binding_detail}`` for this tick's ``local_full`` holds, or
    None when no local budget bound them (gate disabled: local is never full).
    Read at the end of the tick: ``local_spawned`` only grows, and a row is
    held local_full only once it reached ``ready_local``."""
    if spawn_paused:
        text = str(spawn_paused)
        cause = LOCAL_BINDING_QUOTA if text.startswith(QUOTA_SPENT_PREFIX) else LOCAL_BINDING_GATE
        return {"binding": cause, "binding_detail": text}
    if local_budget is None:
        return None
    if local_budget <= 0:
        return {"binding": LOCAL_BINDING_GATE, "binding_detail": "gate allowance 0 this tick"}
    held = local_budget - (ready_local if ready_local is not None else local_budget)
    detail = f"{local_spawned}/{local_budget} local spawns this tick"
    if held > 0:
        detail += f" ({held} held for review)"
    return {"binding": LOCAL_BINDING_QUOTA, "binding_detail": detail}


def take_refusal_code(refusal) -> "str | None":
    """The vocabulary code of a take() refusal string, or None for no refusal."""
    code = split_reason(refusal)[0] if refusal else ""
    return code or None


def not_portable(rule: str) -> str:
    """``not_portable:<rule>``."""
    return f"{NOT_PORTABLE}:{rule}"


def linked(kind: str, other_id: str) -> str:
    """The ``linked`` rule naming the edge that holds the card local."""
    return f"{LINKED}({kind}->{other_id})"


def workspace_bind_rule(rule: str, other_id: str) -> str:
    """``workspace_of:<id>`` / ``workspace_shared:<id>``."""
    return f"{rule}:{other_id}"


def note_unknown_switch(key: str, value: str, fallback: str) -> None:
    """One WARN per (switch, value) per 5 minutes: the switches are read per
    board per tick (and per GC decision), so an unthrottled WARN floods."""
    now = time.monotonic()
    last = _unknown_switch_warned.get((key, value))
    if last is None or now - last >= UNKNOWN_SWITCH_WARN_SECONDS:
        _unknown_switch_warned[(key, value)] = now
        _log.warning("kanban: %s: unknown value %r, reading it as %r", key, value, fallback)


def lineage_pins(kanban_cfg: Any) -> str:
    """``kanban.placement.lineage_pins``. Unset reads as the default; an
    unrecognised value reads as ``legacy`` (spec §6 parse correction: a typo
    on a kill switch lands on the safe side, every linked card local)."""
    pl = kanban_cfg.get("placement") if isinstance(kanban_cfg, Mapping) else None
    raw = pl.get("lineage_pins") if isinstance(pl, Mapping) else None
    if raw is None:
        return LINEAGE_PINS_DEFAULT
    mode = str(raw).strip().lower()
    if mode in LINEAGE_PINS_MODES:
        return mode
    note_unknown_switch("kanban.placement.lineage_pins", mode, LINEAGE_PINS_UNKNOWN)
    return LINEAGE_PINS_UNKNOWN
