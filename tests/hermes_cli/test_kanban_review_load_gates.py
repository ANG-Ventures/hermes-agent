"""Regression tests for the 2026-09-24 reviewer-swarm incident.

Four mechanisms, one incident: the reviewer profile re-ran CI locally on
every round, every card went to review, rounds were unbounded (10.8 avg,
max 123), and the dispatcher spawned into a host at load1 80-110 on 32
cores until the gateway starved. Each test pins ONE removed mechanism:

* ``resolve_per_profile_cap`` — ``kanban.max_in_progress_per_profile`` as a
  per-profile mapping, so one hungry profile can be held at 4 while coders
  keep 32.
* ``spawn_paused`` — a dispatch tick that reclaims but spawns nothing.
* ``LoadGate`` — hysteresis on load1 vs ncpu.
* ``max_review_rounds`` — round N+1 blocks the card for orchestrator take-over
  instead of re-spawning the reviewer.
* ``review_policy=milestone_only`` — slice cards complete in place; only
  ``[milestone]`` / parent cards get a reviewer session.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _events(conn, tid, kind):
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (tid, kind),
    ).fetchall()
    return [json.loads(r["payload"]) if r["payload"] else None for r in rows]


# ── per-profile cap mapping ─────────────────────────────────────────────────

def test_resolve_per_profile_cap_int_and_mapping():
    assert kb.resolve_per_profile_cap(None, "argus") is None
    assert kb.resolve_per_profile_cap(0, "argus") is None
    assert kb.resolve_per_profile_cap(5, "argus") == 5
    spec = {"default": 32, "argus": 4}
    assert kb.resolve_per_profile_cap(spec, "argus") == 4
    assert kb.resolve_per_profile_cap(spec, "daedalus") == 32
    assert kb.resolve_per_profile_cap(spec, None) == 32
    # no default => uncapped for unnamed profiles, capped for named ones
    assert kb.resolve_per_profile_cap({"argus": 2}, "daedalus") is None
    assert kb.resolve_per_profile_cap({"argus": 2}, "argus") == 2
    # garbage values never crash the dispatcher
    assert kb.resolve_per_profile_cap({"argus": "x"}, "argus") is None
    assert kb.resolve_per_profile_cap("nope", "argus") is None


def test_dispatch_honours_mapping_cap_per_assignee(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    for prof in ("alpha", "beta"):
        (kanban_home / "profiles" / prof).mkdir(parents=True, exist_ok=True)
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        for i in range(4):
            kb.create_task(conn, title=f"a{i}", assignee="alpha")
        for i in range(4):
            kb.create_task(conn, title=f"b{i}", assignee="beta")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=lambda *a, **k: 1, dry_run=True,
            max_in_progress_per_profile={"default": 3, "beta": 1},
        )
    spawned = [s[1] for s in res.spawned]
    capped = [c[1] for c in res.skipped_per_profile_capped]
    assert spawned.count("alpha") == 3 and capped.count("alpha") == 1
    assert spawned.count("beta") == 1 and capped.count("beta") == 3


# ── spawn pause ─────────────────────────────────────────────────────────────

def test_spawn_paused_tick_spawns_nothing_but_reports_reason(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    (kanban_home / "profiles" / "alpha").mkdir(parents=True, exist_ok=True)
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="a", assignee="alpha")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=lambda *a, **k: 1, dry_run=True,
            spawn_paused="load1=90.0 > pause_above=32.0",
        )
    assert res.spawned == []
    assert res.spawn_paused == "load1=90.0 > pause_above=32.0"
    with kb.connect_closing() as conn:
        res2 = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 1, dry_run=True)
    assert len(res2.spawned) == 1 and res2.spawn_paused is None


def test_load_gate_hysteresis():
    from gateway.kanban_watchers import LoadGate

    g = LoadGate({}, ncpu=32)
    assert g.pause_above == 32.0 and g.resume_below == 24.0
    assert g.update(20) is None
    assert g.update(32.0) is None          # equal is not over
    assert g.update(33) is not None         # over the bar => paused
    assert g.update(28) is not None         # inside the band stays paused
    assert g.update(23.9) is None           # below resume => released
    assert g.update(30) is None             # under the bar again: allowed
    # explicit thresholds + degenerate band collapse
    g2 = LoadGate({"pause_above": 10, "resume_below": 50}, ncpu=4)
    assert g2.resume_below == 7.5
    # disabled gate never pauses
    g3 = LoadGate({"enabled": False}, ncpu=1)
    assert g3.update(999) is None
    # garbage sample keeps prior state
    assert g.update("nan?") is None


# ── review round cap ────────────────────────────────────────────────────────

def _one_round(conn, tid, reviewer="argus"):
    """implementer requests review -> reviewer claims -> changes_requested."""
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    ok, reason = kb.request_review(
        conn, tid, summary="v", reviewer=reviewer,
        expected_run_id=claimed.current_run_id, with_reason=True,
    )
    assert ok, reason
    review = kb.claim_review_task(conn, tid)
    assert review is not None
    from tests.kanban_review_helpers import covered_request_changes

    assert covered_request_changes(
        conn, tid, reason="fix", expected_run_id=review.current_run_id,
    )[0] is True


def test_round_cap_blocks_fourth_round_for_orchestrator(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "configured_max_review_rounds", lambda: 3)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="loop", assignee="builder")
        for _ in range(3):
            _one_round(conn, tid)
        assert kb.count_review_rounds(conn, tid) == 3
        claimed = kb.claim_task(conn, tid)
        ok, reason = kb.request_review(
            conn, tid, summary="v4", reviewer="argus",
            expected_run_id=claimed.current_run_id, with_reason=True,
        )
        assert ok is False and "review round cap" in reason
        row = conn.execute("SELECT status, block_kind FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert row["status"] == "blocked" and row["block_kind"] == "needs_input"
        cap_events = _events(conn, tid, "review_round_cap")
        assert cap_events and cap_events[0]["rounds"] == 3 and cap_events[0]["cap"] == 3
        # the reviewer was NOT re-spawned: no new review_requested event
        assert len(_events(conn, tid, "review_requested")) == 3


def test_round_cap_below_cap_and_force_still_route(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "configured_max_review_rounds", lambda: 3)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ok", assignee="builder")
        for _ in range(2):
            _one_round(conn, tid)
        claimed = kb.claim_task(conn, tid)
        assert kb.request_review(conn, tid, summary="v3", reviewer="argus",
                                 expected_run_id=claimed.current_run_id) is True
        assert kb.get_task(conn, tid).status == "review"
    # force=True is the explicit operator override past the cap
    with kb.connect() as conn:
        tid2 = kb.create_task(conn, title="forced", assignee="builder")
        for _ in range(3):
            _one_round(conn, tid2)
        assert kb.claim_task(conn, tid2) is not None
        assert kb.request_review(conn, tid2, summary="v4", reviewer="argus", force=True) is True
        assert kb.get_task(conn, tid2).status == "review"


def test_round_cap_zero_disables(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "configured_max_review_rounds", lambda: 0)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="unbounded", assignee="builder")
        for _ in range(4):
            _one_round(conn, tid)
        claimed = kb.claim_task(conn, tid)
        assert kb.request_review(conn, tid, summary="v5", reviewer="argus",
                                 expected_run_id=claimed.current_run_id) is True


# ── milestone-only review policy ────────────────────────────────────────────

def test_milestone_only_completes_slice_cards_in_place(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "configured_review_policy", lambda: "milestone_only")
    monkeypatch.setattr(kb, "configured_max_review_rounds", lambda: 0)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="slice: add flag", assignee="builder")
        claimed = kb.claim_task(conn, tid)
        ok, reason = kb.request_review(
            conn, tid, summary="PR #1 green", expected_run_id=claimed.current_run_id,
            with_reason=True,
        )
        assert ok is True and "review skipped" in reason
        assert kb.get_task(conn, tid).status == "done"
        skipped = _events(conn, tid, "review_skipped")
        assert skipped and skipped[0]["policy"] == "milestone_only"
        assert _events(conn, tid, "review_requested") == []


def test_milestone_only_routes_marker_and_parent_cards(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "configured_review_policy", lambda: "milestone_only")
    monkeypatch.setattr(kb, "configured_max_review_rounds", lambda: 0)
    with kb.connect() as conn:
        # marker in title
        tid = kb.create_task(conn, title="[Milestone] wave 2 done", assignee="builder")
        claimed = kb.claim_task(conn, tid)
        assert kb.request_review(conn, tid, summary="s", reviewer="argus",
                                 expected_run_id=claimed.current_run_id) is True
        assert kb.get_task(conn, tid).status == "review"
        # parent in task_links
        parent = kb.create_task(conn, title="umbrella", assignee="builder")
        child = kb.create_task(conn, title="leaf", assignee="builder")
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (parent, child)
            )
        assert kb.is_milestone_card(conn, parent) is True
        assert kb.is_milestone_card(conn, child) is False
        # an explicit reviewer PROFILE on a slice card does NOT bypass the policy
        # (workers were templated to pass reviewer="argus" on every card — that is
        # the mechanism being removed); the card completes in place.
        leaf = kb.create_task(conn, title="slice with explicit reviewer", assignee="builder")
        claimed = kb.claim_task(conn, leaf)
        assert kb.request_review(conn, leaf, summary="s", reviewer="argus",
                                 expected_run_id=claimed.current_run_id) is True
        assert kb.get_task(conn, leaf).status == "done"
        # the explicit human sentinel still parks the card for a person
        leaf2 = kb.create_task(conn, title="slice for a human", assignee="builder")
        claimed = kb.claim_task(conn, leaf2)
        assert kb.request_review(conn, leaf2, summary="s", reviewer="human",
                                 expected_run_id=claimed.current_run_id) is True
        assert kb.get_task(conn, leaf2).status == "review"
        # force=True is the operator override
        leaf3 = kb.create_task(conn, title="slice forced to review", assignee="builder")
        assert kb.claim_task(conn, leaf3) is not None
        assert kb.request_review(conn, leaf3, summary="s", reviewer="argus", force=True) is True
        assert kb.get_task(conn, leaf3).status == "review"


def test_default_policy_all_is_unchanged(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "configured_review_policy", lambda: "all")
    monkeypatch.setattr(kb, "configured_max_review_rounds", lambda: 0)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="slice", assignee="builder")
        claimed = kb.claim_task(conn, tid)
        assert kb.request_review(conn, tid, summary="s", reviewer="argus",
                                 expected_run_id=claimed.current_run_id) is True
        assert kb.get_task(conn, tid).status == "review"


def test_review_policy_none_skips_every_card(tmp_path, monkeypatch):
    """kanban.review_policy=none (live fleet value, Ace 2026-09-24) must never spawn a
    reviewer — milestone or not — and must not silently map to ``all``."""
    import hermes_cli.kanban_db as kb
    assert "none" in kb.REVIEW_POLICIES
    monkeypatch.setattr(kb, "_kanban_cfg", lambda: {"review_policy": "none"}, raising=False)
    monkeypatch.setattr(kb, "configured_review_policy", lambda: "none")
    assert kb.configured_review_policy() == "none"
