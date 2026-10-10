"""A survivor claim may not close a card past its own still-OPEN routed PR (t_829fce95).

Incident 2026-10-10: t_cee802d5 was routed to review on hermes-home#3421 (OPEN), then an operator ran
``complete --survivor-pr hermes-home#3328 --survivor-unbound`` (an unrelated merged PR) and it went done.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_open_pr as op

ROUTED = "ANG-Ventures/home#3421"
OTHER = "ANG-Ventures/home#3328"


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect() as conn:
        yield conn


def _oracle(monkeypatch, states: dict):
    def query(repo, number):
        state = states.get(number)
        if isinstance(state, Exception):
            raise state
        return None if state is None else {"state": state}
    monkeypatch.setattr(op, "_default_query", lambda: query)


def _routed_card(conn, monkeypatch):
    """A card the open-PR route parked in review on its own PR ROUTED."""
    _oracle(monkeypatch, {3421: "OPEN"})
    tid = kb.create_task(conn, title="impl", assignee="worker")
    kb.claim_task(conn, tid)
    assert kb.complete_task(conn, tid, summary="shipped", metadata={"pr_url": ROUTED})
    status = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]
    assert status == "review"
    return tid


def _stub_preserve(monkeypatch):
    """Survivor verification is the survivor module's job; record the call only."""
    from hermes_cli import kanban_survivor
    calls = []
    monkeypatch.setattr(kanban_survivor, "preserve", lambda *a, **k: calls.append(k) or None)
    return calls


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]


def _events(conn, tid, kind):
    return conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, kind)).fetchall()


def test_unbound_survivor_naming_another_pr_is_refused_while_routed_pr_open(board, monkeypatch):
    tid = _routed_card(board, monkeypatch)
    calls = _stub_preserve(monkeypatch)
    _oracle(monkeypatch, {3421: "OPEN", 3328: "MERGED"})
    with pytest.raises(op.RoutedPrOpenError) as exc:
        kb.complete_task(board, tid, summary="landed", survivor_pr=OTHER, survivor_unbound=True)
    assert ROUTED in str(exc.value) and "still in-flight" in str(exc.value)
    assert _status(board, tid) == "review"
    assert calls == []
    assert len(_events(board, tid, "completion_blocked_routed_pr_open")) == 1


def test_unreadable_routed_pr_fails_closed(board, monkeypatch):
    tid = _routed_card(board, monkeypatch)
    _stub_preserve(monkeypatch)
    # The closed-unmerged gate also fails closed on this; isolate this gate's own verdict.
    monkeypatch.setattr(op, "enforce_not_closed_unmerged", lambda *a, **k: [])
    _oracle(monkeypatch, {3421: RuntimeError("gh down"), 3328: "MERGED"})
    with pytest.raises(op.RoutedPrOpenError) as exc:
        kb.complete_task(board, tid, summary="landed", survivor_pr=OTHER, survivor_unbound=True)
    assert exc.value.unverified == [ROUTED]
    assert _status(board, tid) == "review"


@pytest.mark.parametrize("routed_state", ["MERGED", "CLOSED"])
def test_routed_pr_no_longer_open_allows(board, monkeypatch, routed_state):
    tid = _routed_card(board, monkeypatch)
    _stub_preserve(monkeypatch)
    # CLOSED is the closed-unmerged gate's concern, and it only gates the card's own refs; this gate
    # must not refuse once the routed PR is no longer OPEN.
    monkeypatch.setattr(op, "enforce_not_closed_unmerged", lambda *a, **k: [])
    _oracle(monkeypatch, {3421: routed_state, 3328: "MERGED"})
    assert kb.complete_task(board, tid, summary="landed", survivor_pr=OTHER, survivor_unbound=True)
    assert _status(board, tid) == "done"
    assert _events(board, tid, "completion_blocked_routed_pr_open") == []


def test_abandon_override_allows_and_is_recorded(board, monkeypatch):
    tid = _routed_card(board, monkeypatch)
    _stub_preserve(monkeypatch)
    _oracle(monkeypatch, {3421: "OPEN", 3328: "MERGED"})
    assert kb.complete_task(board, tid, summary="landed", survivor_pr=OTHER, survivor_unbound=True,
                            abandon_routed_pr="re-carried on #3328, closing #3421")
    assert _status(board, tid) == "done"
    rows = _events(board, tid, "routed_pr_abandoned")
    assert len(rows) == 1 and ROUTED in rows[0]["payload"] and "re-carried" in rows[0]["payload"]


def test_blank_abandon_reason_is_refused(board, monkeypatch):
    tid = _routed_card(board, monkeypatch)
    _stub_preserve(monkeypatch)
    _oracle(monkeypatch, {3421: "OPEN", 3328: "MERGED"})
    with pytest.raises(op.RoutedPrOpenError, match="non-empty reason"):
        kb.complete_task(board, tid, summary="landed", survivor_pr=OTHER, survivor_unbound=True,
                         abandon_routed_pr="   ")
    assert _status(board, tid) == "review"


def test_survivor_naming_the_routed_pr_is_not_this_gates_business(board, monkeypatch):
    tid = _routed_card(board, monkeypatch)
    _stub_preserve(monkeypatch)
    _oracle(monkeypatch, {3421: "OPEN"})
    assert op.routed_prs_still_open([ROUTED], survivor_pr=ROUTED,
                                    query_fn=lambda r, n: {"state": "OPEN"}) == ([], [])
    kb.complete_task(board, tid, summary="approve", survivor_pr=ROUTED)
    assert _events(board, tid, "completion_blocked_routed_pr_open") == []


def test_card_never_routed_is_ungated(board, monkeypatch):
    _stub_preserve(monkeypatch)
    _oracle(monkeypatch, {3328: "MERGED"})
    tid = kb.create_task(conn := board, title="impl", assignee="worker")
    kb.claim_task(conn, tid)
    assert kb.complete_task(conn, tid, summary="landed", survivor_pr=OTHER, survivor_unbound=True)
    assert _status(conn, tid) == "done"


# --------------------------------------------------------------------------- Prism round 2 (t_571d174a)

def test_repository_qualifier_is_not_parsed_as_a_named_pr():
    # Prism 59942060bd14: `pull/3421=` is the qualifier `preserve` strips; only #3328 is claimed.
    claim = f"pull/3421={OTHER}"
    refs = op.extract_pr_refs(survivor_pr=[claim])
    assert [(r.repo, r.number) for r in refs] == [("ANG-Ventures/home", 3328)]
    assert op.routed_prs_still_open([ROUTED], survivor_pr=[claim],
                                    query_fn=lambda r, n: {"state": "OPEN"}) == ([ROUTED], [])


def test_qualified_claim_naming_another_pr_is_refused(board, monkeypatch):
    tid = _routed_card(board, monkeypatch)
    calls = _stub_preserve(monkeypatch)
    _oracle(monkeypatch, {3421: "OPEN", 3328: "MERGED"})
    with pytest.raises(op.RoutedPrOpenError):
        kb.complete_task(board, tid, summary="landed", survivor_pr=f"pull/3421={OTHER}",
                         survivor_unbound=True)
    assert _status(board, tid) == "review" and calls == []


def test_qualified_claim_naming_the_routed_pr_passes():
    assert op.routed_prs_still_open([ROUTED], survivor_pr=[f"repo={ROUTED}"],
                                    query_fn=lambda r, n: {"state": "OPEN"}) == ([], [])


def test_prose_only_reroute_keeps_earlier_owned_pr(board, monkeypatch):
    # Prism b2d45aef4595: a second route whose handoff only MENTIONS the PR records own_prs=[];
    # the first route's ownership must survive it.
    tid = _routed_card(board, monkeypatch)
    _stub_preserve(monkeypatch)
    from tests.kanban_review_helpers import covered_request_changes
    assert kb.claim_review_task(board, tid) is not None
    ok, detail = covered_request_changes(board, tid, reason="rework please",
                                         expected_run_id=kb.get_task(board, tid).current_run_id)
    assert ok, detail
    kb.claim_task(board, tid)
    _oracle(monkeypatch, {3421: "OPEN"})
    assert kb.complete_task(board, tid, summary=f"rework pushed to {ROUTED}")
    assert _status(board, tid) == "review"
    newest = kb.list_runs(board, tid)[-1].metadata
    assert newest.get("own_prs") == [] and "auto_routed_open_prs" in newest
    _oracle(monkeypatch, {3421: "OPEN", 3328: "MERGED"})
    with pytest.raises(op.RoutedPrOpenError):
        kb.complete_task(board, tid, summary="landed", survivor_pr=OTHER, survivor_unbound=True)
    assert _status(board, tid) == "review"


def test_abandon_audit_not_written_when_completion_fails(board, monkeypatch):
    # Prism d3eaf84a36bd: the override is recorded with `done`, not before survivor verification.
    from hermes_cli import kanban_survivor
    tid = _routed_card(board, monkeypatch)
    _oracle(monkeypatch, {3421: "OPEN", 3328: "MERGED"})

    def refuse(*a, **k):
        raise kanban_survivor.SurvivorUnavailable("survivor_unavailable: test")
    monkeypatch.setattr(kanban_survivor, "preserve", refuse)
    with pytest.raises(kanban_survivor.SurvivorUnavailable):
        kb.complete_task(board, tid, summary="landed", survivor_pr=OTHER, survivor_unbound=True,
                         abandon_routed_pr="re-carried on #3328")
    assert _status(board, tid) == "review"
    assert _events(board, tid, "routed_pr_abandoned") == []
