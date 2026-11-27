"""t_a6da8394: a review_deadman wake is answered only by an owner act AFTER the deadman event.

Replays t_9854b4b7 (board rows, 2026-11-13). The card went to human:apollo at 03:22 (review_requested); Apollo's
08:10 comment ended in `deadman: hold 2026-11-13T16:00:00-08:00`; the hold expired at 16:00 and two review_deadman
events fired, 16:56 (review-failsafe attended read) and 17:58 (kanban-review-deadman). The fork dismissed both with
"operator already acted; stale wake ignored" because deadman_owner_acted counted ANY owner event since the review
began (the 08:10 hold comment), and stale_surface_reason would have refused the surface as "owner comment newer than
handback" / "summarized PR merged" too. The card sat 141 min to a tier-2 page.

Substitution: the real comments are author `apollo` from Apollo's human session; here they are `human:apollo`, which
owner_acted accepts by label without a state.db session probe. Same verdict under the old rule (asserted below).
"""
import asyncio
import json

import pytest

from gateway import kanban_boomerang_wake as bw
from hermes_cli import kanban_db as kb

from tests.test_kanban_boomerang_defaults import _runner

OWNER = "human:apollo"
PR = "ANG-Ventures/hermes-home#11316"


@pytest.fixture(autouse=True)
def owner_scope(monkeypatch):
    from types import SimpleNamespace
    from gateway import kanban_owner_wake as ow
    home = SimpleNamespace(origin=SimpleNamespace(chat_id="42", platform="discord"))
    monkeypatch.setattr(ow, "resolve_owner", lambda *a: home)
    monkeypatch.setattr(bw, "BACKOFF_BASE_S", 0.0, raising=False)


def _ev(conn, tid, kind, payload, ts):
    kb._append_event(conn, tid, kind, payload)
    eid = kb.max_event_id(conn)
    conn.execute("UPDATE task_events SET created_at = ? WHERE id = ?", (ts, eid))
    conn.commit()
    return eid


def _cm(conn, tid, author, body, ts):
    kb.add_comment(conn, tid, author, body)
    conn.execute("UPDATE task_comments SET created_at = ? WHERE id = (SELECT MAX(id) FROM task_comments)", (ts,))
    conn.execute("UPDATE task_events SET created_at = ? WHERE id = ?", (ts, kb.max_event_id(conn)))
    conn.commit()


def _board_9854(tmp_path, monkeypatch):
    """t_9854b4b7's rows from the 01:22 handback to the second wake, in board order with board timestamps."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("BOOMERANG_DEADMAN_TRIGGER_CURSOR", raising=False)
    monkeypatch.delenv("BOOMERANG_STALE_GUARD", raising=False)
    # PR 11316 merged at 02:00: the merged-PR rule must not answer the wake either
    monkeypatch.setattr(bw, "_health_fn", lambda: (lambda repo, n: {"state": "merged"}))
    kb.init_db()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t_9854b4b7 replay", assignee=OWNER)
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        conn.commit()
        _ev(conn, tid, "review_requested", {"summary": f"auto-routed: PR {PR} still open"}, 1794561757)
        _cm(conn, tid, OWNER, "Post-merge read (Apollo, 2026-11-13) ...", 1794565752)
        _cm(conn, tid, OWNER, "deadman: hold 2026-11-13T10:00:00-08:00", 1794565753)
        _ev(conn, tid, "assigned", {"assignee": OWNER, "from": "_review-hold"}, 1794568978)
        _ev(conn, tid, "review_requested", {"summary": "review hold: checks settled, handed to human:apollo",
                                            "reviewer": OWNER}, 1794568978)
        _cm(conn, tid, OWNER, "Why this waits (Apollo, 2026-11-13) ...\n"
                              "deadman: hold 2026-11-13T16:00:00-08:00", 1794586248)
        _cm(conn, tid, "default", "Apollo (another session, mention only), 15:57 PST 11-13 ...", 1794614177)
        _cm(conn, tid, "review-failsafe", "[review-failsafe] ATTENDED-READ for Apollo ...", 1794617789)
        wake1 = _ev(conn, tid, "review_deadman", {"by": "review-failsafe-attended-read",
                                                  "review_episode": 2549189, "waiting_min": 813}, 1794617789)
        _cm(conn, tid, "boomerang-wake", "boomerang: review_deadman operator already acted; stale wake ignored "
                                         "(delegation del-20261113-165633-e26b)", 1794617843)
        wake2 = _ev(conn, tid, "review_deadman", {"by": "kanban-review-deadman", "episode": 2549189,
                                                  "threshold_min": 15, "waiting_min": 17}, 1794621535)
    return tid, wake1, wake2


def _entry(tid, event_id, did):
    return dict(key=f"claim-{did}", task_id=tid, board=None, row="review_deadman", kind="review_deadman",
                event_id=event_id, delegation_id=did, verdict_nonce="abcd", route={}, mode="fork",
                assignee=OWNER, pr=PR)


def test_old_rule_dismisses_both_wakes(tmp_path, monkeypatch):
    """Step 2 replay: BOOMERANG_DEADMAN_TRIGGER_CURSOR=0 is the rule that ran on 11-13; both wakes read as acted."""
    tid, w1, w2 = _board_9854(tmp_path, monkeypatch)
    monkeypatch.setenv("BOOMERANG_DEADMAN_TRIGGER_CURSOR", "0")
    for eid, did in ((w1, "del-20261113-165633-e26b"), (w2, "del-20261113-175859-c310")):
        e = _entry(tid, eid, did)
        assert bw.deadman_owner_acted(e) is True
        assert bw.stale_surface_reason(e) == "owner comment newer than handback"


def test_both_wakes_are_not_answered(tmp_path, monkeypatch):
    tid, w1, w2 = _board_9854(tmp_path, monkeypatch)
    for eid, did in ((w1, "del-20261113-165633-e26b"), (w2, "del-20261113-175859-c310")):
        e = _entry(tid, eid, did)
        assert bw.deadman_owner_acted(e) is False
        assert bw.stale_surface_reason(e) is None


@pytest.mark.parametrize("wake", [1, 2])
def test_both_wakes_are_delivered_through_run_entry(tmp_path, monkeypatch, wake):
    tid, w1, w2 = _board_9854(tmp_path, monkeypatch)
    did = ("del-20261113-165633-e26b", "del-20261113-175859-c310")[wake - 1] + "-replay"
    entry = _entry(tid, (w1, w2)[wake - 1], did)
    state = bw.BoomerangWakeState(tmp_path / "claims.json")
    state.claim(entry["key"], entry)

    async def parent(*args):
        return object(), "fork"
    monkeypatch.setattr(bw, "parent_resolver", parent)
    monkeypatch.setattr(bw, "ledger_append", lambda *a: None)
    monkeypatch.setattr(bw, "build_goal", lambda entry: "attended read")
    final = "<<<BOOMERANG-VERDICT nonce=abcd>>>\n" + json.dumps(
        {"action": "surface", "summary": "attended read owed on PR 11316"}) + "\n<<<END-VERDICT>>>"
    monkeypatch.setattr(bw, "run_child", lambda *args: ("completed", final))
    turns = []

    async def resurface(runner, e, text=None):
        turns.append(text)
        return True
    monkeypatch.setattr(bw, "resurface", resurface)
    sent = []
    asyncio.run(bw.run_entry(_runner([], sent), entry, state))
    assert sent == []  # t_8ab5557d: no chat line beside a delivered turn
    assert len(turns) == 1
    with kb.connect_closing() as conn:
        mine = [c.body for c in kb.list_comments(conn, tid) if c.body.endswith(f"(delegation {did})")]
    assert f"boomerang: review_deadman attended read owed on PR 11316 (delegation {did})" in mine
    assert not any("operator already acted" in b for b in mine)


@pytest.mark.parametrize("body,acted", [
    ("Read it; landing after CI.", True),                          # a plain comment after the event
    ("deadman: hold 2099-01-01T00:00:00-08:00", True),             # a fresh hold is an act
    ("re-held\ndeadman: hold 2020-01-01T00:00:00Z", False),        # an expired hold is not
])
def test_owner_comment_after_the_event(tmp_path, monkeypatch, body, acted):
    tid, w1, _w2 = _board_9854(tmp_path, monkeypatch)
    with kb.connect_closing() as conn:
        _cm(conn, tid, OWNER, body, 1794617800)
    e = _entry(tid, w1, "d-after")
    assert bw.deadman_owner_acted(e) is acted
    assert (bw.stale_surface_reason(e) is not None) is acted


@pytest.mark.parametrize("kind", ["changes_requested", "blocked", "completed"])
def test_owner_disposition_after_review_still_answers(tmp_path, monkeypatch, kind):
    """A disposition is evidence from the review cursor on; an edit is not (comments are covered above)."""
    tid, w1, _w2 = _board_9854(tmp_path, monkeypatch)
    from gateway import kanban_wake_defer as wd
    seen = []

    def fake_owner_acted(conn, row, skip=None):
        for r in conn.execute("SELECT id, kind, payload, created_at FROM task_events WHERE task_id = ? AND id > ? "
                              "ORDER BY id", (row["task_id"], row["cursor"])):
            if r["kind"] != "commented" and not (skip and skip(r)):
                seen.append(r["kind"])
        return "x" if seen else None
    monkeypatch.setattr(wd, "owner_acted", fake_owner_acted)
    with kb.connect_closing() as conn:
        _ev(conn, tid, "edited", {}, 1794600000)
        _ev(conn, tid, kind, {}, 1794600001)
    assert bw.deadman_owner_acted(_entry(tid, w1, "d-disp")) is True
    assert seen == [kind]


def test_same_second_comments_do_not_turn_an_expired_hold_into_an_act(tmp_path, monkeypatch):
    """Prism d93e70e178e9 repro: owner plain comment, the deadman event, then an expired hold, all in one second.
    The only owner comment AFTER the event is the expired hold, so the wake is not answered."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("BOOMERANG_DEADMAN_TRIGGER_CURSOR", raising=False)
    kb.init_db()
    ts = 1794617789
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="same second", assignee=OWNER)
        _ev(conn, tid, "review_requested", {}, ts - 3600)
        _cm(conn, tid, OWNER, "looking at it now", ts)
        eid = _ev(conn, tid, "review_deadman", {}, ts)
        _cm(conn, tid, OWNER, "deadman: hold 2020-01-01T00:00:00Z", ts)
    assert bw.deadman_owner_acted(_entry(tid, eid, "d-same-second")) is False


# --- a disposition answers the wake only after the event that last put the card into review ---------------------
# Board rows (kind, created_at, payload keys that matter) from the review entry to the deadman event, in board order.
# ``OWN`` marks the owner's disposition: written from Apollo's human session (here: session "apollo-sess", accepted
# by a patched _session_is_owner, since the replay has no state.db). Other rows carry no owner provenance.
OWN = "own"
RESUME_REPLAYS = {
    # t_8406de76: blocked by the owner 11-08 12:48, unblocked to review 11-09 08:01, deadman 2367105 11-10 02:11
    "t_8406de76": ([("review_requested", 1794138993, {"reviewer": OWNER}),
                    ("blocked", 1794170901, {"kind": "transient", "source_status": "review"}, OWN),
                    ("unblocked", 1794240101, {"status": "review", "resume_status": "review"})],
                   ("review_deadman", 1794305499, {"by": "review-failsafe-attended-read", "review_episode": 2184564,
                                                   "waiting_min": 2775}), False),
    # t_e7d728d3: completed by the owner 11-06, reopened to review, blocked, unblocked to review 11-08 19:52,
    # deadman 2294413 11-08 21:54
    "t_e7d728d3": ([("review_requested", 1793971907, {"reviewer": OWNER}),
                    ("completed", 1793973250, {"result_len": 121}, OWN),
                    ("reopened", 1794015937, {"to_status": "review", "assignee": OWNER}),
                    ("blocked", 1794023492, {"kind": "transient", "source_status": "review"}),
                    ("unblocked", 1794196339, {"status": "review", "resume_status": "review"})],
                   ("review_deadman", 1794203663, {"by": "kanban-review-deadman", "episode": 2047184,
                                                   "threshold_min": 15, "waiting_min": 15}), False),
    # t_1d522c59: blocked by the owner 11-09 02:09, unblocked 06:45, promoted to review 07:58, deadman 2336145 11:51
    "t_1d522c59": ([("review_requested", 1794197215, {"reviewer": OWNER}),
                    ("review_deadman", 1794198665, {"by": "kanban-review-deadman", "episode": 2287721}),
                    ("blocked", 1794218954, {"kind": "transient", "source_status": "review"}, OWN),
                    ("unblocked", 1794235505, {"status": "review", "resume_status": "review"}),
                    ("promoted", 1794239901, {"status": "review"})],
                   ("review_deadman", 1794253908, {"by": "review-failsafe-attended-read", "review_episode": 2287721,
                                                   "waiting_min": 944}), False),
    # t_333519f1: the owner completed the card (2341843) after its review_requested, deadman 2341852 29 s later:
    # still answered
    "t_333519f1": ([("review_requested", 1794258993, {"reviewer": OWNER}),
                    ("completed", 1794260404, {"result_len": 313}, OWN)],
                   ("review_deadman", 1794260433, {"by": "kanban-review-deadman", "episode": 2340768,
                                                   "threshold_min": 15, "waiting_min": 20}), True),
}


def _board_resume(tmp_path, monkeypatch, card):
    from gateway import kanban_wake_defer as wd
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("BOOMERANG_DEADMAN_TRIGGER_CURSOR", raising=False)
    monkeypatch.delenv("BOOMERANG_STALE_GUARD", raising=False)
    monkeypatch.setattr(bw, "_health_fn", lambda: (lambda repo, n: {"state": "open"}))
    monkeypatch.setattr(wd, "_session_is_owner", lambda conn, sid, *a: sid == "apollo-sess")
    rows, deadman, _acted = RESUME_REPLAYS[card]
    kb.init_db()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title=f"{card} replay", assignee=OWNER)
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        conn.commit()
        for kind, ts, payload, *own in rows:
            eid = _ev(conn, tid, kind, payload, ts)
            if own:
                conn.execute("UPDATE task_events SET actor_profile = 'apollo', actor_session_id = 'apollo-sess' "
                             "WHERE id = ?", (eid,))
                conn.commit()
        kind, ts, payload = deadman
        wake = _ev(conn, tid, kind, payload, ts)
    return tid, wake


@pytest.mark.parametrize("card", sorted(RESUME_REPLAYS))
def test_disposition_counts_only_after_the_last_review_entry(tmp_path, monkeypatch, card):
    tid, wake = _board_resume(tmp_path, monkeypatch, card)
    assert bw.deadman_owner_acted(_entry(tid, wake, f"d-{card}")) is RESUME_REPLAYS[card][2]


@pytest.mark.parametrize("card", ["t_8406de76", "t_e7d728d3", "t_1d522c59"])
def test_old_rule_dismissed_the_resumed_reviews(tmp_path, monkeypatch, card):
    """The merged rule (cursor = latest review_requested) answered these wakes with the ended park; so does the kill
    switch."""
    tid, wake = _board_resume(tmp_path, monkeypatch, card)
    monkeypatch.setenv("BOOMERANG_DEADMAN_TRIGGER_CURSOR", "0")
    assert bw.deadman_owner_acted(_entry(tid, wake, f"d-{card}")) is True


@pytest.mark.parametrize("card", ["t_8406de76", "t_e7d728d3", "t_1d522c59"])
def test_resumed_review_wake_is_delivered_through_run_entry(tmp_path, monkeypatch, card):
    tid, wake = _board_resume(tmp_path, monkeypatch, card)
    did = f"del-{card}-replay"
    entry = _entry(tid, wake, did)
    state = bw.BoomerangWakeState(tmp_path / "claims.json")
    state.claim(entry["key"], entry)

    async def parent(*args):
        return object(), "fork"
    monkeypatch.setattr(bw, "parent_resolver", parent)
    monkeypatch.setattr(bw, "ledger_append", lambda *a: None)
    monkeypatch.setattr(bw, "build_goal", lambda entry: "attended read")
    final = "<<<BOOMERANG-VERDICT nonce=abcd>>>\n" + json.dumps(
        {"action": "surface", "summary": "review owed"}) + "\n<<<END-VERDICT>>>"
    monkeypatch.setattr(bw, "run_child", lambda *args: ("completed", final))
    turns = []

    async def resurface(runner, e, text=None):
        turns.append(text)
        return True
    monkeypatch.setattr(bw, "resurface", resurface)
    sent = []
    asyncio.run(bw.run_entry(_runner([], sent), entry, state))
    assert sent == []  # t_8ab5557d: no chat line beside a delivered turn
    assert len(turns) == 1
    with kb.connect_closing() as conn:
        mine = [c.body for c in kb.list_comments(conn, tid) if c.body.endswith(f"(delegation {did})")]
    assert not any("operator already acted" in b for b in mine)
