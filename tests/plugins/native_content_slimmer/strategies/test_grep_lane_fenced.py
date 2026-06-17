"""Amendment-1 fence: the (terminal, grep) lossy lane must NOT be selectable.

RD-AMEND1, 2026-06-16: grep_cluster fenced off the semantic path (PRD-5 Amendment 1).
Grep output routes to the lossless lane; the lossy grep_cluster view is unreachable.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from plugins.native_content_slimmer.strategies import registry


@pytest.fixture(autouse=True)
def fresh_registry() -> Iterator[None]:
    registry.clear_registry_for_tests()
    yield
    registry.clear_registry_for_tests()


def test_grep_lane_is_fenced_select_returns_none() -> None:
    # The deny-by-default registry must return None for the fenced grep lane.
    assert registry.select_compressor(tool_name="terminal", content_class="grep") is None


def test_grep_cluster_absent_from_registered_lanes() -> None:
    lanes = {(l.tool_name, l.content_class, l.strategy_name) for l in registry.registered_lanes()}
    assert ("terminal", "grep", "grep_cluster") not in lanes
    # Fence is grep-scoped, NOT a global kill — the other eval-gated lanes survive.
    assert ("web_extract", "json", "json_compact") in lanes
    assert ("terminal", "log", "log_dedup") in lanes
    assert ("terminal", "diff", "diff_collapse") in lanes
