"""Same-incident overlap gate (t_ba30f0de).

2026-09-30 18:0x PT two Apollo sessions minted the xAI-426 incident twice in
five minutes: #prism t_f1437191 + t_aa47fad3, #pr-judge t_d979a494 +
t_e6bb7b19. Replayed from the live rows (fixtures/kanban_overlap_0930.json)
at their real timestamps through ``create_task(duplicate_guard=True)``, the
surface the kanban_create tool and CLI use.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_overlap as kov

FIXTURE = Path(__file__).parent / "fixtures" / "kanban_overlap_0930.json"
CARDS = json.loads(FIXTURE.read_text(encoding="utf-8"))["cards"]

INCIDENT = ["t_f1437191", "t_d979a494", "t_e6bb7b19", "t_aa47fad3"]
# Same hour, different sessions, unrelated work that still shares a common
# status + lane in passing (402 + xai; 429 + bpr).
UNRELATED_PAIRS = [("t_fa9695fd", "t_f94e08a4"), ("t_957ca870", "t_bbe0023c")]


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def clock(monkeypatch):
    state = {"now": 0}
    monkeypatch.setattr(kb.time, "time", lambda: float(state["now"]))
    return state


def _replay(conn, clock, fixture_ids, *, guard=True):
    """Create the fixture cards in birth order at their real timestamps."""
    new = {}
    for fid in sorted(fixture_ids, key=lambda f: CARDS[f]["created_at"]):
        card = CARDS[fid]
        clock["now"] = card["created_at"]
        new[fid] = kb.create_task(
            conn, title=card["title"], body=card["body"], assignee="daedalus",
            session_id=card["session_id"], session_explicit=True,
            duplicate_guard=guard,
        )
    return new


def _overlap_events(conn, tid):
    return [
        json.loads(r["payload"])
        for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
            (tid, kov.OVERLAP_EVENT),
        )
    ]


def _overlaps_of(conn, tid):
    return {o["id"] for ev in _overlap_events(conn, tid) for o in ev["overlaps"]}


def _comments(conn, tid):
    return [
        r["body"] for r in conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id", (tid,)
        )
    ]


def test_0930_double_mint_pairs_are_flagged_both_ways(kanban_home, clock):
    with kb.connect_closing() as conn:
        ids = _replay(conn, clock, INCIDENT)
        prism_fix, judge_fix = ids["t_f1437191"], ids["t_d979a494"]
        judge_class, prism_audit = ids["t_e6bb7b19"], ids["t_aa47fad3"]

        # Pair 1: the cliproxy client-version fix, minted by both sessions.
        assert prism_fix in _overlaps_of(conn, judge_fix)
        assert judge_fix in _overlaps_of(conn, prism_fix)
        # Pair 2: the status-classifier class fix vs the sibling-defect audit.
        assert judge_class in _overlaps_of(conn, prism_audit)
        assert prism_audit in _overlaps_of(conn, judge_class)

        new_side = [c for c in _comments(conn, judge_fix) if c.startswith("OVERLAPS")]
        old_side = [c for c in _comments(conn, prism_fix) if c.startswith("OVERLAPS")]
        assert any(c.startswith(f"OVERLAPS {prism_fix} (score ") for c in new_side)
        assert any(c.startswith(f"OVERLAPS {judge_fix} (score ") for c in old_side)
        assert any("426" in c for c in new_side)

        # Only the newer card of a flagged pair carries the hold.
        held = [ev for ev in _overlap_events(conn, judge_fix) if "hold_until" in ev]
        assert held and held[0]["hold_until"] == CARDS["t_d979a494"]["created_at"] + 600
        assert not [ev for ev in _overlap_events(conn, prism_fix) if "hold_until" in ev]
        # Never auto-archived, never refused.
        statuses = {kb.get_task(conn, t).status for t in ids.values()}
        assert "archived" not in statuses


@pytest.mark.parametrize("pair", UNRELATED_PAIRS)
def test_unrelated_same_hour_cards_are_not_flagged(kanban_home, clock, pair):
    with kb.connect_closing() as conn:
        ids = _replay(conn, clock, pair)
        for tid in ids.values():
            assert _overlap_events(conn, tid) == []
            assert not [c for c in _comments(conn, tid) if c.startswith("OVERLAPS")]


def test_one_sessions_own_fanout_is_not_a_double_mint(kanban_home, clock):
    """#pr-judge minted J1 + J2 two seconds apart on purpose: a split."""
    with kb.connect_closing() as conn:
        ids = _replay(conn, clock, ["t_d979a494", "t_e6bb7b19"])
        for tid in ids.values():
            assert _overlap_events(conn, tid) == []


def test_library_create_without_guard_is_untouched(kanban_home, clock):
    with kb.connect_closing() as conn:
        ids = _replay(conn, clock, INCIDENT, guard=False)
        for tid in ids.values():
            assert _overlap_events(conn, tid) == []


def test_outside_30_min_window_is_not_flagged(kanban_home, clock):
    with kb.connect_closing() as conn:
        first = _replay(conn, clock, ["t_f1437191"])["t_f1437191"]
        card = CARDS["t_d979a494"]
        clock["now"] = CARDS["t_f1437191"]["created_at"] + kov.OVERLAP_WINDOW_SECONDS + 1
        second = kb.create_task(
            conn, title=card["title"], body=card["body"], assignee="daedalus",
            session_id=card["session_id"], session_explicit=True,
            duplicate_guard=True,
        )
        assert _overlap_events(conn, second) == []
        assert _overlap_events(conn, first) == []


def test_dispatcher_holds_newer_card_10_min_then_releases(
    kanban_home, clock, all_assignees_spawnable,
):
    with kb.connect_closing() as conn:
        ids = _replay(conn, clock, ["t_f1437191", "t_d979a494"])
        older, newer = ids["t_f1437191"], ids["t_d979a494"]
        born = CARDS["t_d979a494"]["created_at"]

        clock["now"] = born + 60
        assert kb.check_respawn_guard(conn, newer) == "overlap_hold"
        assert kb.check_respawn_guard(conn, older) is None
        result = kb.dispatch_once(conn, dry_run=True)
        assert (newer, "overlap_hold") in result.respawn_guarded
        assert newer not in [s[0] for s in result.spawned]
        assert older in [s[0] for s in result.spawned]

        clock["now"] = born + kov.OVERLAP_HOLD_SECONDS
        assert kb.check_respawn_guard(conn, newer) != "overlap_hold"


def test_operator_requeue_releases_the_hold_early(kanban_home, clock):
    with kb.connect_closing() as conn:
        ids = _replay(conn, clock, ["t_f1437191", "t_d979a494"])
        newer = ids["t_d979a494"]
        clock["now"] = CARDS["t_d979a494"]["created_at"] + 30
        assert kb.check_respawn_guard(conn, newer) == "overlap_hold"
        with kb.write_txn(conn):
            kb._append_event(conn, newer, "requeued", {"by": "apollo"})
        assert kb.check_respawn_guard(conn, newer) != "overlap_hold"


def test_later_overlap_on_a_held_card_does_not_release_its_hold(kanban_home, clock):
    """Prism P1 00beb1de4c64 (#1600 @5cfd5466): a third card matching the
    held one appends an informational overlap_detected event (no hold_until)
    to it. The hold must survive that event until its own deadline."""
    with kb.connect_closing() as conn:
        ids = _replay(conn, clock, ["t_f1437191", "t_d979a494"])
        held = ids["t_d979a494"]
        born = CARDS["t_d979a494"]["created_at"]
        clock["now"] = born + 60
        assert kb.check_respawn_guard(conn, held) == "overlap_hold"

        card = CARDS["t_f1437191"]
        clock["now"] = born + 120
        third = kb.create_task(
            conn, title=card["title"], body=card["body"], assignee="daedalus",
            session_id=card["session_id"], session_explicit=True,
            duplicate_guard=True, force_reason="third mint of the same incident",
        )
        # Precondition: the held card's NEWEST overlap event is informational.
        newest = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
            "ORDER BY id DESC LIMIT 1", (held, kov.OVERLAP_EVENT),
        ).fetchone()
        assert third in {o["id"] for o in json.loads(newest["payload"])["overlaps"]}
        assert "hold_until" not in json.loads(newest["payload"])

        clock["now"] = born + 180
        assert kov.overlap_hold_until(conn, held, ("requeued",)) == born + kov.OVERLAP_HOLD_SECONDS
        assert kb.check_respawn_guard(conn, held) == "overlap_hold"
        # Requeue after the hold still releases it early.
        with kb.write_txn(conn):
            kb._append_event(conn, held, "requeued", {"by": "apollo"})
        assert kb.check_respawn_guard(conn, held) != "overlap_hold"


def test_score_pair_features_on_the_live_rows():
    f = {k: kov.features(v["title"], v["body"]) for k, v in CARDS.items()}
    score, reasons = kov.score_pair(f["t_f1437191"], f["t_d979a494"])
    assert score >= kov.OVERLAP_THRESHOLD
    assert any(r.startswith("HTTP 426") for r in reasons)
    assert any(r.startswith("quoted error") for r in reasons)
    for a, b in UNRELATED_PAIRS:
        assert kov.score_pair(f[a], f[b])[0] < kov.OVERLAP_THRESHOLD


def test_origin_line_beats_a_restamped_home():
    """All four 09-30 cards ended up homed on one session after a takeover;
    the birth line still names who minted each."""
    a = CARDS["t_d979a494"]
    b = CARDS["t_f1437191"]
    assert a["session_id"] == b["session_id"]
    assert kov.origin_session(a["body"], a["session_id"]) != kov.origin_session(
        b["body"], b["session_id"]
    )
