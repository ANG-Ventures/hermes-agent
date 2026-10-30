"""A closed own PR whose trusted ``CLOSED: SUPERSEDED-BY #N`` names an OPEN #N parks the card on #N (t_ec8ceae2).

t_d781ecd9: hermes-home#7730 closed `CLOSED: SUPERSEDED-BY #7816` (ang-fleet-interactive[bot]) while #7816 was open;
`complete --superseded-by ANG-Ventures/hermes-home#7816` and `--abandon-routed-pr` both refused, so the card sat
blocked. Now the card moves to review with #7816 as its own PR and the closer completes it when #7816 merges.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_open_pr as op
from hermes_cli import kanban_superseded_open as so

REPO = "ANG-Ventures/hermes-home"
OLD, NEW = 7730, 7816
BOT = "ang-fleet-interactive[bot]"


def _thread(tid, *, login=BOT, token=f"CLOSED: SUPERSEDED-BY #{NEW} -- contention cut", names_card=True):
    def read(repo, n):
        if n == OLD:
            return {"body": "", "comments": [{"body": "looks good", "login": "x", "assoc": "NONE"},
                                             {"body": f"{token} [card {tid}]", "login": login, "assoc": "NONE"}]}
        if n == NEW:
            return {"body": f"cut contention (t_cc223ad3){'; supersedes ' + tid if names_card else ''}",
                    "comments": []}
        return None
    return read


@pytest.mark.parametrize("login,assoc,ok", [(BOT, "NONE", True), ("Kyzcreig", "NONE", True),
                                            ("someone", "MEMBER", True), ("someone", "NONE", False),
                                            ("ang-x[bot]x", "NONE", False)])
def test_trusted_authors(login, assoc, ok):
    assert so.trusted(login, assoc) is ok


def test_close_token_reads_the_newest_trusted_line():
    cs = [{"body": "CLOSED: SUPERSEDED-BY #1", "login": BOT}, {"body": "CLOSED: ABANDONED -- x", "login": "evil"}]
    assert so.close_token(cs) == ("SUPERSEDED-BY", "#1")


# --- E2E through complete_task on a temp board -------------------------------


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_" + "HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _blocked_card(conn, monkeypatch, *, new_state="OPEN", **thread_kw):
    """The t_d781ecd9 shape: routed to review on #7730, then blocked once #7730 closed SUPERSEDED-BY #7816."""
    tid = kb.create_task(conn, title="starve-only", assignee="worker")
    kb.claim_task(conn, tid)
    run = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    assert kb.block_task(conn, tid, reason="closed SUPERSEDED-BY #7816", kind="needs_input")
    # the routed handoff's own PR, as the open-PR route records it
    conn.execute("UPDATE task_runs SET metadata=? WHERE id=?",
                 ('{"pr_url": "https://github.com/%s/pull/%d", "own_prs": ["%s#%d"], '
                  '"auto_routed_open_prs": ["%s#%d"]}' % (REPO, OLD, REPO, OLD, REPO, OLD), run))
    conn.commit()
    states = {OLD: "CLOSED", NEW: new_state}
    monkeypatch.setattr(op, "_default_query", lambda: (lambda r, n: {"state": states.get(n, op.NOT_FOUND)}))
    monkeypatch.setattr(so, "_default_thread", lambda: _thread(tid, **thread_kw))
    return tid


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]


def test_e2e_superseded_by_open_pr_parks_on_it(board, monkeypatch):
    with kb.connect() as conn:
        tid = _blocked_card(conn, monkeypatch)
        assert kb.complete_task(conn, tid, superseded_by=f"{REPO}#{NEW}") is True
        assert _status(conn, tid) == "review"
        ev = [e for e in kb.list_events(conn, tid) if e.kind == "routed_pr_superseded"]
        assert ev and ev[0].payload["targets"][0]["successor"] == f"{REPO}#{NEW}"
        meta = kb.list_runs(conn, tid)[-1].metadata
        assert meta["own_prs"] == [f"{REPO}#{NEW}"]          # what the closer / merge pass reads
        comments = [c.body for c in kb.list_comments(conn, tid)]
        assert any(op.ROUTE_COMMENT in b and f"#{NEW}" in b for b in comments)


def test_e2e_abandon_flag_also_parks(board, monkeypatch):
    with kb.connect() as conn:
        tid = _blocked_card(conn, monkeypatch)
        assert kb.complete_task(conn, tid, abandon_routed_pr="superseded by #7816 (contention cut)",
                                survivor_none=True, survivor_reason="r") is True
        assert _status(conn, tid) == "review"


def test_e2e_after_the_successor_merges_the_card_completes(board, monkeypatch):
    with kb.connect() as conn:
        tid = _blocked_card(conn, monkeypatch)
        assert kb.complete_task(conn, tid, superseded_by=f"{REPO}#{NEW}")
        states = {OLD: "CLOSED", NEW: "MERGED"}
        monkeypatch.setattr(op, "_default_query", lambda: (lambda r, n: {"state": states.get(n, op.NOT_FOUND)}))
        assert kb.complete_task(conn, tid, superseded_by=f"{REPO}#{NEW}", summary="#7816 merged") is True
        assert _status(conn, tid) == "done"


@pytest.mark.parametrize("kw", [
    {"names_card": False},                                   # #7816 does not name the card
    {"login": "someone"},                                     # untrusted CLOSED: line
    {"token": "CLOSED: SUPERSEDED-BY #9999 -- other"},        # names a PR that is not #7816
    {"token": "CLOSED: STALE -- nope"},                       # not a supersession token
    {"new_state": "CLOSED"},                                  # successor closed too
])
def test_e2e_mutants_still_refused(board, monkeypatch, kw):
    with kb.connect() as conn:
        tid = _blocked_card(conn, monkeypatch, **kw)
        with pytest.raises(op.ClosedUnmergedPrError):
            kb.complete_task(conn, tid, superseded_by=f"{REPO}#{NEW}")
        assert _status(conn, tid) == "blocked"
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "completion_blocked_closed_unmerged_pr" in kinds and "routed_pr_superseded" not in kinds


def test_e2e_plain_complete_without_an_explicit_flag_is_refused(board, monkeypatch):
    with kb.connect() as conn:
        tid = _blocked_card(conn, monkeypatch)
        with pytest.raises(op.ClosedUnmergedPrError):
            kb.complete_task(conn, tid, summary="done")
        assert _status(conn, tid) == "blocked"


# --- Prism round 1 (#2465) -----------------------------------------------------


def test_e2e_park_keeps_the_cards_other_open_pr(board, monkeypatch):
    # Prism 0eca5d119ed6: an unaffected own PR (#7900, still open) stays an own PR next to the successor
    with kb.connect() as conn:
        tid = _blocked_card(conn, monkeypatch)
        run = kb.list_runs(conn, tid)[-1].id
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=?",
                     ('{"own_prs": ["%s#%d", "%s#7900"], "auto_routed_open_prs": ["%s#%d", "%s#7900"]}'
                      % (REPO, OLD, REPO, REPO, OLD, REPO), run))
        conn.commit()
        assert kb.complete_task(conn, tid, superseded_by=f"{REPO}#{NEW}") is True
        assert kb.list_runs(conn, tid)[-1].metadata["own_prs"] == [f"{REPO}#7900", f"{REPO}#{NEW}"]


def test_e2e_park_refuses_a_live_claim_without_ownership(board, monkeypatch):
    # Prism 28a20c93323b: the same live-claim fence as complete_task
    with kb.connect() as conn:
        tid = _blocked_card(conn, monkeypatch)
        monkeypatch.setattr(kb, "_claim_is_live", lambda trow: True)
        with pytest.raises(kb.LiveClaimError):
            kb.complete_task(conn, tid, superseded_by=f"{REPO}#{NEW}")
        assert _status(conn, tid) == "blocked"


def test_e2e_park_keeps_result_and_records_review_provenance(board, monkeypatch):
    # Prism 4ba1d3ae3905 + 2a212f2d5c68: --result survives; a review_requested event names the implementer
    with kb.connect() as conn:
        tid = _blocked_card(conn, monkeypatch)
        assert kb.complete_task(conn, tid, superseded_by=f"{REPO}#{NEW}", result="RESULT-EVIDENCE-LINE") is True
        assert "RESULT-EVIDENCE-LINE" in (kb.list_runs(conn, tid)[-1].summary or "")
        ev = [e for e in kb.list_events(conn, tid) if e.kind == "review_requested"]
        assert ev and ev[-1].payload["implementer"] == "worker"
