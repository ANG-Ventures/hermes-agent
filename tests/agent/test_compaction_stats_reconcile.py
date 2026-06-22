"""Reconciliation guard tests for CompactionStats — the CI lint gate that makes a
non-reconciling compaction announce structurally impossible to ship.

Covers (per the approved spec, Part 5 layer-3):
- property: consistent stats -> ok; deliberately-broken -> not ok / assert raises
- producer-shape: every frozen Phase-0 fixture shape reconciles
- (c1) partition-break negative test (row in two/zero buckets)
- (c2) misclassification negative test (row moved between two VALID buckets — the
       222->222 bug class; only the cross-identity catches it)
- zero-fold + zero-clear edges accepted
"""

from __future__ import annotations

import json
import os

import pytest

from agent.compaction_stats import CompactionStats

_FIXTURE = os.path.join(os.path.dirname(__file__), os.pardir, "fixtures", "compaction_phase0.json")


def _stats(**overrides) -> CompactionStats:
    """A baseline consistent stats object; override fields to break it."""
    base = dict(
        pre_messages=748, post_messages=34, eligible_count=332,
        kept_messages=32, summary_messages=1, anchor_messages=1,
        cleared_count=416, folded_count=300,
        pre_tokens=397767, post_tokens=17811,
        kept_tokens=11211, summary_tokens=4800, anchor_tokens=1800,
        cleared_tokens=247262, folded_tokens=139294,
    )
    base.update(overrides)
    return CompactionStats(**base)


# ---------------------------------------------------------------------------
# property: baseline reconciles; freed math
# ---------------------------------------------------------------------------

def test_baseline_reconciles():
    ok, reason = _stats().validate()
    assert ok, reason


def test_freed_tokens_and_pct():
    s = _stats()
    assert s.freed_tokens == 397767 - 17811
    assert s.freed_pct == 96


def test_freed_pct_none_when_pre_zero():
    # pre_tokens<=0 must be rejected by validate AND freed_pct guarded
    s = _stats(pre_tokens=0)
    assert s.freed_pct is None
    ok, _ = s.validate()
    assert not ok


# ---------------------------------------------------------------------------
# (c1) partition-break: a row double-counted or dropped -> message axis fails
# ---------------------------------------------------------------------------

def test_producer_rejects_partition_break_extra_row():
    # one extra cleared row (double-count): cleared+folded+kept > pre
    ok, reason = _stats(cleared_count=417).validate()
    assert not ok and "msg axis" in reason


def test_producer_rejects_partition_break_dropped_row():
    # a dropped row: cleared+folded+kept < pre
    ok, reason = _stats(kept_messages=31, post_messages=33).validate()
    assert not ok


# ---------------------------------------------------------------------------
# (c2) misclassification: move a row between two VALID buckets so the partition
# is STILL TOTAL (cleared+folded+kept==pre stays true) — the 222->222 bug class.
# Only the eligible cross-identity catches it.
# ---------------------------------------------------------------------------

def test_producer_rejects_misclassified_population():
    # move one row cleared->folded: 415+301+32==748 still TRUE (partition total),
    # but eligible was measured at 332, so kept+folded=333 != eligible -> caught.
    s = _stats(cleared_count=415, folded_count=301)
    # partition sum still total:
    assert s.cleared_count + s.folded_count + s.kept_messages == s.pre_messages
    ok, reason = s.validate()
    assert not ok and "eligible" in reason, reason


def test_assert_reconciles_raises_on_broken():
    with pytest.raises(ValueError, match="does not reconcile"):
        _stats(cleared_count=415, folded_count=301).assert_reconciles()


def test_assert_reconciles_silent_on_ok():
    _stats().assert_reconciles()  # no raise


# ---------------------------------------------------------------------------
# zero-fold + zero-clear edges accepted
# ---------------------------------------------------------------------------

def test_zero_fold_222_shape_is_valid():
    # folded==0, summary==0, kept==eligible (the literal triggering shape)
    s = CompactionStats(
        pre_messages=658, post_messages=222, eligible_count=222,
        kept_messages=222, summary_messages=0, anchor_messages=0,
        cleared_count=436, folded_count=0,
        pre_tokens=329000, post_tokens=30000,
        kept_tokens=30000, summary_tokens=0, anchor_tokens=0,
        cleared_tokens=299000, folded_tokens=0,
    )
    ok, reason = s.validate()
    assert ok, reason


def test_zero_fold_rejects_nonzero_summary():
    # folded==0 but summary_messages==1 is inconsistent
    s = CompactionStats(
        pre_messages=658, post_messages=223, eligible_count=222,
        kept_messages=222, summary_messages=1, anchor_messages=0,
        cleared_count=436, folded_count=0,
        pre_tokens=329000, post_tokens=34800,
        kept_tokens=30000, summary_tokens=4800, anchor_tokens=0,
        cleared_tokens=299000, folded_tokens=0,
    )
    ok, reason = s.validate()
    assert not ok and "zero-fold" in reason


def test_zero_clear_allchat_is_valid():
    s = CompactionStats(
        pre_messages=120, post_messages=34, eligible_count=120,
        kept_messages=32, summary_messages=1, anchor_messages=1,
        cleared_count=0, folded_count=88,
        pre_tokens=58000, post_tokens=12500,
        kept_tokens=8000, summary_tokens=3000, anchor_tokens=1500,
        cleared_tokens=0, folded_tokens=50000,
    )
    ok, reason = s.validate()
    assert ok, reason


# ---------------------------------------------------------------------------
# token-axis + freed-identity (anchor term) negative cases
# ---------------------------------------------------------------------------

def test_token_pre_axis_break():
    ok, reason = _stats(cleared_tokens=200000).validate()
    assert not ok and "token pre" in reason


def test_freed_identity_missing_anchor_would_fail():
    # If a producer forgot the anchor term, post_tokens would be wrong by anchor.
    # Simulate: anchor really 1800 but post computed without it.
    ok, reason = _stats(post_tokens=17811 - 1800).validate()
    assert not ok  # token post axis catches it


# ---------------------------------------------------------------------------
# optional cleared sub-split must sum to cleared
# ---------------------------------------------------------------------------

def test_cleared_subsplit_must_sum():
    ok, reason = _stats(
        cleared_tool_count=300, cleared_other_count=100,  # 400 != 416
    ).validate()
    assert not ok and "sub-split" in reason


def test_cleared_subsplit_ok():
    ok, reason = _stats(
        cleared_tool_count=356, cleared_other_count=60,  # 416 == cleared
        cleared_tool_tokens=200000, cleared_other_tokens=47262,
    ).validate()
    assert ok, reason


# ---------------------------------------------------------------------------
# frozen-fixture shapes all reconcile (fixture-is-oracle)
# ---------------------------------------------------------------------------

def test_all_frozen_fixture_shapes_reconcile():
    fix = json.load(open(_FIXTURE))
    for name, s in fix["shapes"].items():
        kw = {k: v for k, v in s.items() if not k.startswith("_")}
        kw.pop("session_id", None)
        kw.pop("fresh_tail_count", None)
        kw.pop("eligible_tokens", None)
        cs = CompactionStats(**kw)
        ok, reason = cs.validate()
        assert ok, f"fixture shape {name!r}: {reason}"
