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

# The task_event that records why a ready card was held this tick.
PLACEMENT_WAIT_EVENT = "placement_wait"
# A repeated identical reason is re-written only after this many seconds.
PLACEMENT_WAIT_REFRESH_SECONDS = 900

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
