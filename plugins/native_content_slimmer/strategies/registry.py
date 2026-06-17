from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .base import Compressor


@dataclass(frozen=True)
class StrategySelection:
    tool_name: str
    content_class: str
    compressor: Compressor
    eval_run_id: str
    threshold: str
    strategy_name: str
    params: Mapping[str, object] = field(default_factory=dict)


_REGISTRY: dict[tuple[str, str], StrategySelection] = {}
_SELF_REGISTRATION_DONE = False


def ensure_registered() -> None:
    """Lazy self-registration entry point for eval-gated compressor lanes.

    Strategy modules are imported only on first selection/inspection so merely
    importing the plugin stays cheap. Each strategy registration carries the
    fixture/eval id + frozen threshold that justified opting that lane into the
    registry; runtime config still decides whether compression is allowed for a
    given turn. This keeps the registry discoverable in a fresh process without
    letting incidental module imports bypass config gates.
    """

    global _SELF_REGISTRATION_DONE
    if _SELF_REGISTRATION_DONE:
        return
    _SELF_REGISTRATION_DONE = True

    from . import diff_collapse, json_compact, log_dedup

    json_compact.register()
    log_dedup.register_default_lanes()
    diff_collapse.register()
    # RD-AMEND1 (PRD-5 Amendment 1, 2026-06-16): grep_cluster is FENCED OFF the semantic path.
    # The lossy grep_cluster view destroys path:line citation provenance (RC-1) AND yields
    # negative compression on real grep dumps (RC-3). Grep routes to the lossless lane instead.
    # DO NOT re-add grep_cluster.register() without re-passing the citation gate (see
    # docs/PRD-5-AMEND-1-grep-cluster-citation-fix.md §9 G-4 and test_grep_lane_fenced.py).
    # The strategy module + its unit tests stay in-tree (D-3) for a future trigger-gated fix.


def register_compressor(
    *,
    tool_name: str,
    content_class: str,
    compressor: Compressor,
    eval_run_id: str,
    threshold: str,
    strategy_name: str | None = None,
    params: Mapping[str, object] | None = None,
) -> StrategySelection:
    """Opt a single evaluated (tool, content_class) lane into compression."""

    tool = _normalize_tool(tool_name)
    lane = _normalize_content_class(content_class)
    if not tool:
        raise ValueError("tool_name is required for compressor registration")
    if not lane or lane == "unknown":
        raise ValueError("content_class must be known for compressor registration")
    if not eval_run_id:
        raise ValueError("eval_run_id is required for compressor registration")
    if not threshold:
        raise ValueError("threshold is required for compressor registration")
    name = strategy_name or str(getattr(compressor, "strategy_name", "") or getattr(compressor, "name", "") or compressor.__class__.__name__)
    selection = StrategySelection(
        tool_name=tool,
        content_class=lane,
        compressor=compressor,
        eval_run_id=str(eval_run_id),
        threshold=str(threshold),
        strategy_name=name,
        params=dict(params or {}),
    )
    _REGISTRY[(tool, lane)] = selection
    return selection


def select_compressor(*, tool_name: str, content_class: str) -> StrategySelection | None:
    """Return the eval-passed compressor for a lane, or ``None`` deny-by-default."""

    lane = _normalize_content_class(content_class)
    if not lane or lane == "unknown":
        return None
    ensure_registered()
    return _REGISTRY.get((_normalize_tool(tool_name), lane))


def registered_lanes() -> tuple[StrategySelection, ...]:
    ensure_registered()
    return tuple(_REGISTRY.values())


def clear_registry_for_tests() -> None:
    global _SELF_REGISTRATION_DONE
    _REGISTRY.clear()
    _SELF_REGISTRATION_DONE = False


def _normalize_tool(tool_name: str) -> str:
    return str(tool_name or "").strip()


def _normalize_content_class(content_class: str) -> str:
    return str(content_class or "").strip().lower()
