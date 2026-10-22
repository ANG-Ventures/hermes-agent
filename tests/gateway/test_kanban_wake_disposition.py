"""Boomerang v2 P3a judgment gate (spec §5.4, INV-5, AC-6, AC-13, D-13, D-15).

``wake_disposition`` must never auto-handle a judgment event, must let card tags beat every
kind row, and must leave a green/pending own-repo handback to the merge pass. Retries persist
in ``kanban_auto_handle.json`` across a restart.
"""
from __future__ import annotations

import json

import pytest

from gateway import kanban_owner_wake as ow
from gateway.kanban_auto_handle import AutoHandleState, state_path
from gateway.kanban_wake_disposition import (
    AUTO, NONE, SURFACE, classify, resolve_auto_handle, wake_disposition,
)

# ``kanban_watchers`` defines ``_WAKE_KINDS`` inside the notifier tick, so it is not importable
# (P3b may hoist it; that file is outside this card's fence). Mirrored from the tick.
NOTIFIER_WAKE_KINDS = (
    "completed", "gave_up", "crashed", "timed_out",
    "blocked", "review_requested", "changes_requested",
    "block_loop_detected",
)

# Every owner-wake trigger -> the raw event (kind, payload) that produces it in _classify_row.
OWNER_WAKE_TRIGGER_EVENTS = {
    ow.TRIGGER_NEEDS_INPUT: [("blocked", {"kind": "needs_input", "reason": "pick A or B"})],
    ow.TRIGGER_PRECONDITION: [("blocked", {"reason": "waiting on PR#12"})],
    ow.TRIGGER_STUCK: [(k, {"error": "boom"}) for k in sorted(ow.STUCK_KINDS)],
    ow.TRIGGER_LAST_BLOCKER: [("completed", {"summary": "done"})],
    ow.TRIGGER_RED_HANDBACK: [("review_requested", {"own_repo": True, "pr_state": "red"})],
    ow.TRIGGER_WOKE_NONSPAWNABLE: [("skipped_nonspawnable", {"assignee": "apollo"})],
}

PLAIN = {"id": "t_1", "title": "plain card", "body": "do the thing"}
# Payload classes the gate distinguishes, applied to every kind.
PAYLOAD_CLASSES = (
    None, {}, {"kind": "needs_input"}, {"kind": "capability"},
    {"own_repo": True, "pr_state": "green"}, {"own_repo": True, "pr_state": "pending"},
    {"own_repo": True, "pr_state": "red"}, {"own_repo": True, "pr_state": "dirty"},
    {"own_repo": True}, {"own_repo": False, "pr_state": "red"},
)
EXPECTED = {
    # kind: verdict on a plain card, first occurrence, for a payload with no review fields
    "completed": AUTO, "changes_requested": AUTO,
    "crashed": AUTO, "timed_out": AUTO, "gave_up": AUTO, "stalled": AUTO,
    "blocked": SURFACE, "block_loop_detected": SURFACE,
    "review_requested": SURFACE,   # no own_repo/pr_state -> upstream row -> surface
    "reclaimed": SURFACE, "skipped_nonspawnable": SURFACE,
}


def _all_kinds():
    return sorted(set(NOTIFIER_WAKE_KINDS) | set(ow.SCAN_KINDS))


def test_owner_wake_trigger_map_covers_every_trigger():
    triggers = {v for k, v in vars(ow).items() if k.startswith("TRIGGER_") and isinstance(v, str)}
    assert triggers == set(OWNER_WAKE_TRIGGER_EVENTS), "new owner-wake trigger: add its event row"


@pytest.mark.parametrize("kind", _all_kinds())
def test_every_wake_and_scan_kind_has_a_deliberate_verdict(kind):
    assert kind in EXPECTED, f"{kind} reaches a wake path but the test has no expected verdict"
    assert wake_disposition(kind, {}, PLAIN, retries=0) == EXPECTED[kind]


@pytest.mark.parametrize("kind", _all_kinds())
@pytest.mark.parametrize("payload", PAYLOAD_CLASSES, ids=repr)
def test_every_kind_x_payload_returns_a_known_verdict(kind, payload):
    d = classify(kind, payload, PLAIN, retries=0)
    assert d.verdict in (AUTO, SURFACE, NONE)
    assert (d.mode is not None) == (d.verdict == AUTO)
    if kind == "blocked" or kind == "block_loop_detected":
        assert d.verdict == SURFACE
    if d.verdict == NONE:
        assert kind == "review_requested" and payload["own_repo"] is True


@pytest.mark.parametrize("trigger", sorted(OWNER_WAKE_TRIGGER_EVENTS))
def test_every_owner_wake_reason_is_dispositioned(trigger):
    for kind, payload in OWNER_WAKE_TRIGGER_EVENTS[trigger]:
        verdict = wake_disposition(kind, payload, PLAIN, retries=0)
        if trigger in (ow.TRIGGER_NEEDS_INPUT, ow.TRIGGER_PRECONDITION, ow.TRIGGER_WOKE_NONSPAWNABLE):
            assert verdict == SURFACE, (trigger, kind)
        else:
            assert verdict == AUTO, (trigger, kind)


@pytest.mark.parametrize("payload_extra", [{}, {"reason": "x"}, {"own_repo": True, "pr_state": "red"}])
@pytest.mark.parametrize("retries", [0, 1, 5])
@pytest.mark.parametrize("card", [PLAIN, {"title": "t", "body": "auto_handle: on"}, None])
def test_needs_input_surfaces_always(payload_extra, retries, card):
    payload = {"kind": "needs_input", **payload_extra}
    assert wake_disposition("blocked", payload, card, retries=retries) == SURFACE


@pytest.mark.parametrize("kind", ["status", "heartbeat", "unblocked", "archived", "brand_new_kind", ""])
def test_unlisted_kinds_surface(kind):
    assert wake_disposition(kind, {}, PLAIN, retries=0) == SURFACE


# --- AC-13 precedence ----------------------------------------------------------------


@pytest.mark.parametrize("tag_card", [
    {"title": "send the email", "body": "tags: outbound"},
    {"title": "[outbound] send the email", "body": ""},
    {"title": "x", "body": "", "tags": ["Outbound"]},
])
def test_completed_on_outbound_card_surfaces(tag_card):
    assert wake_disposition("completed", {}, tag_card, retries=0) == SURFACE


def test_changes_requested_on_irreversible_card_surfaces():
    card = {"title": "drop prod table", "body": "tags: irreversible, db"}
    assert wake_disposition("changes_requested", {}, card, retries=0) == SURFACE


def test_red_own_repo_on_spend_card_surfaces():
    card = {"title": "buy", "body": "tags: spend"}
    assert wake_disposition("review_requested", {"own_repo": True, "pr_state": "red"}, card) == SURFACE


@pytest.mark.parametrize("state", ["green", "pending", "GREEN"])
def test_own_repo_green_or_pending_is_none(state):
    assert wake_disposition("review_requested", {"own_repo": True, "pr_state": state}, PLAIN) == NONE


@pytest.mark.parametrize("state", ["red", "dirty"])
def test_own_repo_red_is_auto(state):
    d = classify("review_requested", {"own_repo": True, "pr_state": state}, PLAIN)
    assert d.verdict == AUTO and d.mode == "fork"


# --- t_6e9f48cc: every open Prism key answered in the handback -> a mechanical fork ----------


@pytest.mark.parametrize("state", ["green", "pending", "red", "dirty", None])
def test_own_repo_review_all_keys_answered_is_auto_fork(state):
    payload = {"own_repo": True, "prism_all_answered": True, **({"pr_state": state} if state else {})}
    d = classify("review_requested", payload, PLAIN)
    assert (d.verdict, d.mode, d.row) == (AUTO, "fork", "own_repo_review_all_keys_answered")


@pytest.mark.parametrize("answered", [False, None, "true", 1])
@pytest.mark.parametrize("state,row", [("green", "own_repo_review_green_or_pending"),
                                       ("pending", "own_repo_review_green_or_pending"),
                                       ("red", "own_repo_review_red"), (None, "default")])
def test_unanswered_or_unreadable_keys_keep_todays_rows(answered, state, row):
    """One key missing (False), unreadable prism state (None) or a non-bool: today's row."""
    payload = {"own_repo": True, "prism_all_answered": answered, **({"pr_state": state} if state else {})}
    assert classify("review_requested", payload, PLAIN).row == row


def test_all_answered_upstream_or_spend_card_surfaces():
    assert wake_disposition("review_requested", {"own_repo": False, "prism_all_answered": True}, PLAIN) == SURFACE
    card = {"title": "buy", "body": "tags: spend"}
    assert wake_disposition("review_requested", {"own_repo": True, "prism_all_answered": True}, card) == SURFACE


@pytest.mark.parametrize("payload", [
    {"own_repo": False, "pr_state": "red"}, {"own_repo": False, "pr_state": "green"},
    {"own_repo": True, "pr_state": "unknown"}, {"own_repo": True}, {"own_repo": "yes", "pr_state": "red"},
])
def test_upstream_or_unknown_review_surfaces(payload):
    assert wake_disposition("review_requested", payload, PLAIN) == SURFACE


@pytest.mark.parametrize("kind", sorted(ow.STUCK_KINDS))
def test_crash_class_retries_once_then_surfaces(kind):
    assert wake_disposition(kind, {}, PLAIN, retries=0) == AUTO
    assert wake_disposition(kind, {}, PLAIN, retries=1) == SURFACE


def test_auto_rows_default_to_fork_mode():
    for kind in ("completed", "changes_requested", "crashed"):
        assert classify(kind, {}, PLAIN).mode == "fork"


# --- D-15 tri-state ------------------------------------------------------------------


@pytest.mark.parametrize("card,session,board,expected", [
    (None, True, None, True),       # card null + session on -> True
    (False, True, None, False),     # card false + session on -> False
    (None, None, None, False),      # all null -> False
    (True, False, False, True),
    (None, False, True, False),     # session beats board
    (None, None, True, True),       # board default when session unset
    ({"auto_handle": None}, "on", None, True),
    ({"auto_handle": False}, "on", True, False),
    (None, "garbage", None, False),
])
def test_resolve_auto_handle_tri_state(card, session, board, expected):
    assert resolve_auto_handle(card, session, board) is expected


# --- retry state ---------------------------------------------------------------------


def test_retries_persist_across_restart_and_reset_on_completed(tmp_path):
    path = tmp_path / "gateway" / "kanban_auto_handle.json"
    first = AutoHandleState(path)
    assert first.retries("t_a") == 0
    assert first.bump_retry("t_a") == 1
    first.bump_retry("t_b")

    restarted = AutoHandleState(path)          # simulated gateway restart: fresh object, same file
    assert restarted.retries("t_a") == 1
    assert wake_disposition("crashed", {}, PLAIN, retries=restarted.retries("t_a")) == SURFACE

    assert restarted.observe_event("t_a", "review_requested") is False
    assert restarted.observe_event("t_a", "completed") is True
    assert AutoHandleState(path).retries("t_a") == 0
    assert AutoHandleState(path).retries("t_b") == 1
    assert restarted.observe_event("t_b", "archived") is True
    assert json.loads(path.read_text())["retries"] == {}
    assert not list(path.parent.glob(".auto-handle-*")), "temp file left behind"


def test_retry_state_preserves_unknown_keys_and_lives_under_hermes_home(tmp_path):
    path = state_path()
    assert path.name == "kanban_auto_handle.json" and path.parent.name == "gateway"
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"claims": {"k": 1}, "retries": {"t_x": "2"}}))
    st = AutoHandleState(p)
    assert st.retries("t_x") == 2
    st.bump_retry("t_x")
    data = json.loads(p.read_text())
    assert data["claims"] == {"k": 1} and data["retries"] == {"t_x": 3}
