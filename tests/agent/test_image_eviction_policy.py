"""Arithmetic contract of the shared send-path image-eviction policy (#113517).

Both outbound passes (``context_compressor.evict_stale_outbound_tool_images`` and
``anthropic_message_convert._evict_old_screenshots``) translate their message shapes into
``(carrier blocks newest-first, reserved)`` and call this one function; the shape tests in
``test_outbound_stale_vision.py`` / ``test_computer_use.py`` cover the translation, this file
covers the numbers once.
"""

from __future__ import annotations

import pytest

from agent.image_eviction_policy import (
    IMAGE_EVICTION_BATCH,
    OUTBOUND_IMAGE_BUDGET_BYTES,
    OUTBOUND_IMAGE_FLOOR,
    OUTBOUND_IMAGE_LIMIT,
    outbound_image_retire_count,
)

MB = 1_000_000


@pytest.mark.parametrize(
    ("blocks", "reserved", "sizes", "reserved_bytes", "expected"),
    [
        # at the limit: append-only, nothing rewritten
        ([1] * OUTBOUND_IMAGE_LIMIT, 0, None, 0, 0),
        # one over: exactly one batch
        ([1] * (OUTBOUND_IMAGE_LIMIT + 1), 0, None, 0, IMAGE_EVICTION_BATCH),
        # still within the first window after a batch: same retire count (frontier holds)
        ([1] * (OUTBOUND_IMAGE_LIMIT + IMAGE_EVICTION_BATCH), 0, None, 0, IMAGE_EVICTION_BATCH),
        # second window: two batches, never a fixed single batch
        ([1] * (OUTBOUND_IMAGE_LIMIT + IMAGE_EVICTION_BATCH + 1), 0, None, 0, 2 * IMAGE_EVICTION_BATCH),
        # one carrier breaching alone: fixable, floor yields
        ([OUTBOUND_IMAGE_LIMIT + 5], 0, None, 0, 1),
        # four 10-block carriers: retire until it fits, not everything
        ([10, 10, 10, 10], 0, None, 0, 2),
        # uploads alone breach: unfixable, keep the floor
        ([1] * 5, OUTBOUND_IMAGE_LIMIT + 1, None, 0, 5 - OUTBOUND_IMAGE_FLOOR),
        # uploads alone breach with fewer carriers than the floor: nothing to gain
        ([1, 1], OUTBOUND_IMAGE_LIMIT + 5, None, 0, 0),
        # byte pressure from uploads: floor never shelters a 413
        ([1, 1, 1], 5, [3 * MB] * 3, OUTBOUND_IMAGE_BUDGET_BYTES + MB, 3),
        # bytes bind before blocks: 13 frames of budget/12 each, so 12 fit
        ([1] * 13, 0, [OUTBOUND_IMAGE_BUDGET_BYTES // 12] * 13, 0, IMAGE_EVICTION_BATCH),
        # empty
        ([], 0, None, 0, 0),
    ],
)
def test_retire_count(blocks, reserved, sizes, reserved_bytes, expected):
    assert (
        outbound_image_retire_count(
            blocks, reserved, carrier_bytes_newest_first=sizes, reserved_bytes=reserved_bytes
        )
        == expected
    )


def test_quantum_shrinks_to_the_fit_window_for_heavy_carriers():
    """Three-block carriers: a batch of eight is wider than the window, so the retire count
    must still be a step function (hold for window - floor turns), never ``total - floor``."""
    window = OUTBOUND_IMAGE_LIMIT // 3
    counts = [outbound_image_retire_count([3] * n, 0) for n in range(window + 1, window + 10)]
    assert all(c > 0 for c in counts)
    assert all(3 * (n - c) <= OUTBOUND_IMAGE_LIMIT for n, c in zip(range(window + 1, window + 10), counts))
    moves = sum(a != b for a, b in zip(counts, counts[1:]))
    assert moves <= len(counts) // (window - OUTBOUND_IMAGE_FLOOR)


# --- catalog-driven defaults (card t_3a0b1ce4) ---------------------------------------------------------------
# The default limit / byte budget are read from the fleet catalog (``images.stricter_cap_threshold_count`` /
# ``images.outbound_budget_bytes``) at CALL time; the module literals are only the fallback. Every test points
# the fleet-root resolver at a temp root via HERMES_HOME; the real catalog is never touched.


@pytest.fixture
def fleet_root(tmp_path, monkeypatch):
    from agent import anthropic_limits

    r = tmp_path / "fleet-root"
    (r / "config").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(r))
    anthropic_limits._reset_for_tests()
    yield r
    anthropic_limits._reset_for_tests()


def _write_catalog(root, images):
    import json

    (root / "config" / "anthropic-limits.json").write_text(
        json.dumps({"version": 1, "request_body_bytes": {"value": 128_000_000}, "images": images})
    )


# Eight 1 MB frames: 8 MB is far under the 96 MB default budget but over a 5 MB catalog budget.
EIGHT_FRAMES = ([1] * 8, 0)
EIGHT_SIZES = [MB] * 8


def test_literal_budget_matches_2026_10_29_limits():
    from agent.image_eviction_policy import OUTBOUND_IMAGE_BUDGET_BYTES

    # 75% of the 128 MB request-size limit (was 24 MB under the 32 MB cap); hermes-home's
    # anthropic-limits-fallback-lint holds this literal equal to images.outbound_budget_bytes.
    assert OUTBOUND_IMAGE_BUDGET_BYTES == 96_000_000
    assert OUTBOUND_IMAGE_LIMIT == 20


def test_catalog_budget_drives_default(fleet_root):
    blocks, reserved = EIGHT_FRAMES
    _write_catalog(fleet_root, {"outbound_budget_bytes": {"value": 5_000_000}})
    retire = outbound_image_retire_count(blocks, reserved, carrier_bytes_newest_first=EIGHT_SIZES)
    assert retire > 0
    assert sum(EIGHT_SIZES[: len(EIGHT_SIZES) - retire]) <= 5_000_000
    # an explicit budget= still wins over the catalog
    assert outbound_image_retire_count(
        blocks, reserved, carrier_bytes_newest_first=EIGHT_SIZES, budget=96_000_000
    ) == 0


def test_catalog_threshold_drives_default(fleet_root):
    _write_catalog(fleet_root, {"stricter_cap_threshold_count": {"value": 5}})
    assert outbound_image_retire_count([1] * 8, 0) > 0
    assert outbound_image_retire_count([1] * 8, 0, limit=OUTBOUND_IMAGE_LIMIT) == 0


def test_catalog_edit_takes_effect_without_reimport(fleet_root):
    import os

    blocks, reserved = EIGHT_FRAMES
    _write_catalog(fleet_root, {"outbound_budget_bytes": {"value": 96_000_000}})
    assert outbound_image_retire_count(blocks, reserved, carrier_bytes_newest_first=EIGHT_SIZES) == 0
    p = fleet_root / "config" / "anthropic-limits.json"
    _write_catalog(fleet_root, {"outbound_budget_bytes": {"value": 5_000_000}})
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert outbound_image_retire_count(blocks, reserved, carrier_bytes_newest_first=EIGHT_SIZES) > 0


@pytest.mark.parametrize("content", ["{not json", None])
def test_unreadable_or_missing_catalog_uses_literals(fleet_root, content):
    if content is not None:
        (fleet_root / "config" / "anthropic-limits.json").write_text(content)
    blocks, reserved = EIGHT_FRAMES
    assert outbound_image_retire_count(blocks, reserved, carrier_bytes_newest_first=EIGHT_SIZES) == 0
    assert outbound_image_retire_count([1] * OUTBOUND_IMAGE_LIMIT, 0) == 0
    assert outbound_image_retire_count([1] * (OUTBOUND_IMAGE_LIMIT + 1), 0) == IMAGE_EVICTION_BATCH


@pytest.mark.parametrize("bad", [0, -1, True, "5000000", None])
def test_invalid_catalog_value_uses_literal(fleet_root, bad):
    _write_catalog(fleet_root, {"outbound_budget_bytes": {"value": bad}, "stricter_cap_threshold_count": {"value": bad}})
    blocks, reserved = EIGHT_FRAMES
    assert outbound_image_retire_count(blocks, reserved, carrier_bytes_newest_first=EIGHT_SIZES) == 0
    assert outbound_image_retire_count([1] * OUTBOUND_IMAGE_LIMIT, 0) == 0
