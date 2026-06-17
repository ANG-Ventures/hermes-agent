"""Standing guard: re-enabling grep_cluster must turn the fence test RED (G-4),
and a fenced grep input must take the lossless route (AC-1 route-proof, GT-4).

RD-AMEND1, 2026-06-16 — PRD-5 Amendment 1 §9 G-4 + AC-1.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from plugins.native_content_slimmer.config import NativeContentSlimmerConfig
from plugins.native_content_slimmer.hook import NativeContentSlimmerHooks
from plugins.native_content_slimmer.marker import parse_marker
from plugins.native_content_slimmer.store import ArtifactStore
from plugins.native_content_slimmer.strategies import registry

_GREP_RAW = (
    "src/recovery.py:48:rescue_mode = \"auto\"\n"
    "docs/recovery.md:12:rescue_mode is discussed here\n"
    "tests/test_recovery.py:31:assert rescue_mode\n"
)


@pytest.fixture(autouse=True)
def fresh_registry() -> Iterator[None]:
    registry.clear_registry_for_tests()
    yield
    registry.clear_registry_for_tests()


def _hooks(tmp_path: Path) -> NativeContentSlimmerHooks:
    cfg = NativeContentSlimmerConfig(
        enabled=True,
        mode="active_lossless",
        compression_mode="active",
        min_bytes=0,
        preview_bytes=120,
    )
    return NativeContentSlimmerHooks(
        cfg,
        store=ArtifactStore(tmp_path / "artifacts"),
        secret=b"fence-guard-secret",
    )


def test_fenced_grep_input_takes_lossless_route(tmp_path: Path) -> None:
    """AC-1: a grep-class input in active mode must NOT yield strategy=grep_cluster."""
    hooks = _hooks(tmp_path)
    out = hooks.transform_terminal_output(
        output=_GREP_RAW,
        command="rg rescue_mode .",
        returncode=0,
        session_id="s-fence",
        tool_call_id="c-fence",
    )

    assert out is not None
    marker = parse_marker(out)
    assert marker is not None
    assert marker.fields.get("strategy") != "grep_cluster"
    assert marker.fields.get("lossy_view", "false") in ("false", None, "")
    assert "grep_cluster" not in out


def test_reenabling_grep_lane_breaks_the_fence_invariant() -> None:
    """G-4 mutation-with-teeth: re-registering grep flips select from None to non-None."""
    registry.clear_registry_for_tests()
    # HEAD (fenced): grep lane is absent.
    assert registry.select_compressor(tool_name="terminal", content_class="grep") is None

    # Simulate the forbidden mutation (re-enable the lane) and prove the invariant breaks.
    from plugins.native_content_slimmer.strategies import grep_cluster

    grep_cluster.register()
    assert registry.select_compressor(tool_name="terminal", content_class="grep") is not None
    # Cleanup is handled by the autouse fixture (clear_registry_for_tests).
