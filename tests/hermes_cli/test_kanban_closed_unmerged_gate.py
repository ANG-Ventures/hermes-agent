"""Closed-unmerged done gate (t_a1550189): a card whose own PR was closed without merge is not done.

Would have caught t_ddb3938c (hermes-home#341) and t_a3b910ce (ace-media-homelab#81): stacked PRs
auto-closed by GitHub when their base branch was deleted after a squash, content never on default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_open_pr as op

PR_URL = "https://github.com/ANG-Ventures/r/pull/5"
HOME_ENV = "HERMES_" + "HOME"


def states(**by_number):
    """query_fn stub: {'5': 'CLOSED', '9': 'MERGED'}; unknown numbers are NOT_FOUND."""
    return lambda repo, n: {"state": by_number.get(f"n{n}", op.NOT_FOUND)}


# --- unit ----------------------------------------------------------------


def test_closed_primary_ref_refused():
    with pytest.raises(op.ClosedUnmergedPrError) as exc:
        op.enforce_not_closed_unmerged("t_x", "done", metadata={"pr_url": PR_URL},
                                       query_fn=states(n5="CLOSED"), sha_check=lambda r, s: False)
    assert exc.value.prs == ["ANG-Ventures/r#5"]
    assert "t_x is still in-flight" in str(exc.value)


def test_survivor_pr_is_a_primary_ref():
    with pytest.raises(op.ClosedUnmergedPrError):
        op.enforce_not_closed_unmerged("t_x", "done", survivor_pr="ANG-Ventures/r#5",
                                       query_fn=states(n5="CLOSED"), sha_check=lambda r, s: False)


def test_merged_superseder_token_allows():
    assert op.enforce_not_closed_unmerged(
        "t_x", "CLOSED: SUPERSEDED-BY ANG-Ventures/r#9 -- landed there", metadata={"pr_url": PR_URL},
        query_fn=states(n5="CLOSED", n9="MERGED"), sha_check=lambda r, s: False) != []


def test_re_carried_as_token_allows():
    assert op.enforce_not_closed_unmerged(
        "t_x", "RE-CARRIED-AS https://github.com/ANG-Ventures/r/pull/9", metadata={"pr_url": PR_URL},
        query_fn=states(n5="CLOSED", n9="MERGED"), sha_check=lambda r, s: False) != []


def test_merged_pr_without_token_does_not_allow():
    # Review 13103: "not any readable PR" -- prose naming a merged PR is not a supersession claim.
    for text in ("superseded by ANG-Ventures/r#9", "fixed in ANG-Ventures/r#9", "superseded-by ANG-Ventures/r#9"):
        with pytest.raises(op.ClosedUnmergedPrError):
            op.enforce_not_closed_unmerged("t_x", text, metadata={"pr_url": PR_URL},
                                           query_fn=states(n5="CLOSED", n9="MERGED"), sha_check=lambda r, s: True)


def test_bare_pr_number_resolves_in_the_closed_prs_repo():
    seen = []

    def q(repo, n):
        seen.append((repo, n))
        return {"state": {5: "CLOSED", 9: "MERGED"}.get(n, op.NOT_FOUND)}
    assert op.enforce_not_closed_unmerged("t_x", "SUPERSEDED-BY #9", metadata={"pr_url": PR_URL},
                                          query_fn=q, sha_check=lambda r, s: False) != []
    assert ("ANG-Ventures/r", 9) in seen


def test_unmerged_superseder_does_not_allow():
    with pytest.raises(op.ClosedUnmergedPrError):
        op.enforce_not_closed_unmerged("t_x", "SUPERSEDED-BY ANG-Ventures/r#9", metadata={"pr_url": PR_URL},
                                       query_fn=states(n5="CLOSED", n9="OPEN"), sha_check=lambda r, s: False)


def test_closed_pr_cannot_supersede_itself():
    with pytest.raises(op.ClosedUnmergedPrError):
        op.enforce_not_closed_unmerged("t_x", f"SUPERSEDED-BY {PR_URL}", metadata={"pr_url": PR_URL},
                                       query_fn=states(n5="CLOSED"), sha_check=lambda r, s: True)


def test_sha_on_default_in_superseded_by_allows():
    seen = []
    assert op.enforce_not_closed_unmerged(
        "t_x", "done", metadata={"pr_url": PR_URL}, superseded_by="a149fe12a9",
        query_fn=states(n5="CLOSED"), sha_check=lambda r, s: seen.append((r, s)) or True) != []
    assert seen == [("ANG-Ventures/r", "a149fe12a9")]


def test_sha_token_not_on_default_refused():
    with pytest.raises(op.ClosedUnmergedPrError):
        op.enforce_not_closed_unmerged("t_x", "SUPERSEDED-BY a149fe12a9", metadata={"pr_url": PR_URL},
                                       query_fn=states(n5="CLOSED"), sha_check=lambda r, s: False)


def test_sha_without_token_refused():
    with pytest.raises(op.ClosedUnmergedPrError):
        op.enforce_not_closed_unmerged("t_x", "landed as a149fe12a9", metadata={"pr_url": PR_URL},
                                       query_fn=states(n5="CLOSED"), sha_check=lambda r, s: True)


def test_all_digit_token_is_not_a_sha():
    seen = []
    with pytest.raises(op.ClosedUnmergedPrError):
        op.enforce_not_closed_unmerged("t_x", "SUPERSEDED-BY 36329762", metadata={"pr_url": PR_URL},
                                       query_fn=states(n5="CLOSED"),
                                       sha_check=lambda r, s: seen.append(s) or True)
    assert seen == []


def test_every_closed_pr_needs_its_own_superseder():
    two = {"pr_urls": [PR_URL, "https://github.com/ANG-Ventures/s/pull/6"]}
    with pytest.raises(op.ClosedUnmergedPrError) as exc:
        op.enforce_not_closed_unmerged("t_x", "SUPERSEDED-BY a149fe12a9", metadata=two,
                                       query_fn=states(n5="CLOSED", n6="CLOSED"),
                                       sha_check=lambda r, s: r == "ANG-Ventures/r")
    assert exc.value.closed == ["ANG-Ventures/r#5", "ANG-Ventures/s#6"]


def test_superseder_lookup_failure_refuses():
    def q(repo, n):
        if n == 9:
            raise RuntimeError("rate limited")
        return {"state": "CLOSED"}
    with pytest.raises(op.ClosedUnmergedPrError):
        op.enforce_not_closed_unmerged("t_x", "SUPERSEDED-BY ANG-Ventures/r#9", metadata={"pr_url": PR_URL},
                                       query_fn=q, sha_check=lambda r, s: False)


@pytest.mark.parametrize("state", ["MERGED", "OPEN", op.NOT_FOUND])
def test_non_closed_states_pass(state):
    assert op.enforce_not_closed_unmerged("t_x", "done", metadata={"pr_url": PR_URL},
                                          query_fn=states(n5=state), sha_check=lambda r, s: False) == []


def test_foreign_owner_ref_is_not_gated():
    assert op.enforce_not_closed_unmerged("t_x", "done", metadata={"pr_url": "https://github.com/NousResearch/x/pull/5"},
                                          query_fn=states(n5="CLOSED"), sha_check=lambda r, s: False) == []


def test_prose_only_closed_mention_is_not_gated():
    # Summaries routinely cite closed PRs ("the old #341 closed unmerged"); only the card's own ref counts.
    assert op.enforce_not_closed_unmerged("t_x", f"old attempt {PR_URL} was closed", metadata={},
                                          query_fn=states(n5="CLOSED"), sha_check=lambda r, s: False) == []


@pytest.mark.parametrize("answer", [RuntimeError("rate limited"), None, {}])
def test_unreadable_lookup_fails_closed(answer):
    def q(repo, n):
        if isinstance(answer, Exception):
            raise answer
        return answer
    with pytest.raises(op.ClosedUnmergedPrError) as exc:
        op.enforce_not_closed_unmerged("t_x", "done", metadata={"pr_url": PR_URL}, query_fn=q)
    assert exc.value.unverified == ["ANG-Ventures/r#5"]
    assert "could not be read" in str(exc.value)


def test_no_live_github_under_pytest():
    assert op.enforce_not_closed_unmerged("t_x", "done", metadata={"pr_url": PR_URL}) == []


# --- E2E through complete_task on a temp board ---------------------------


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(HOME_ENV, str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def no_survivor(monkeypatch):
    # Survivor capture is orthogonal to this gate; a temp board has no workspace to preserve.
    import importlib
    ks = importlib.import_module(kb.__name__.rsplit(".", 1)[0] + ".kanban_survivor")
    monkeypatch.setattr(ks, "preserve", lambda *a, **k: None)


def _claimed(conn):
    tid = kb.create_task(conn, title="slice", assignee="worker")
    kb.claim_task(conn, tid)
    run = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    return tid, run


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]


def test_e2e_closed_unmerged_pr_refused_card_stays_running(board, monkeypatch):
    monkeypatch.setattr(op, "_default_query", lambda: states(n5="CLOSED"))
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        with pytest.raises(op.ClosedUnmergedPrError):
            kb.complete_task(conn, tid, summary="shipped", metadata={"pr_url": PR_URL},
                             expected_run_id=run)
        assert _status(conn, tid) == "running"
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
        assert "completion_blocked_closed_unmerged_pr" in kinds


def test_e2e_merged_superseder_completes(board, no_survivor, monkeypatch):
    monkeypatch.setattr(op, "_default_query", lambda: states(n5="CLOSED", n9="MERGED"))
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert kb.complete_task(conn, tid, summary="shipped", result="SUPERSEDED-BY ANG-Ventures/r#9",
                                metadata={"pr_url": PR_URL}, expected_run_id=run)
        assert _status(conn, tid) == "done"


def test_e2e_merged_pr_completes(board, no_survivor, monkeypatch):
    monkeypatch.setattr(op, "_default_query", lambda: states(n5="MERGED"))
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert kb.complete_task(conn, tid, summary="shipped", metadata={"pr_url": PR_URL},
                                expected_run_id=run)
        assert _status(conn, tid) == "done"


def test_e2e_review_approval_is_gated_too(board, monkeypatch):
    # A card parked in review whose PR was then auto-closed must not be approved to done.
    monkeypatch.setattr(op, "_default_query", lambda: states(n5="CLOSED"))
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="slice", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        with pytest.raises(op.ClosedUnmergedPrError):
            kb.complete_task(conn, tid, summary="approved", metadata={"pr_url": PR_URL})
        assert _status(conn, tid) == "review"


def _done_with_pr(conn, monkeypatch):
    monkeypatch.setattr(op, "_default_query", lambda: states(n5="MERGED"))
    tid, run = _claimed(conn)
    assert kb.complete_task(conn, tid, summary="shipped", metadata={"pr_url": PR_URL}, expected_run_id=run)
    return tid


def test_e2e_archive_of_card_with_closed_pr_refused(board, no_survivor, monkeypatch):
    with kb.connect() as conn:
        tid = _done_with_pr(conn, monkeypatch)
        monkeypatch.setattr(op, "_default_query", lambda: states(n5="CLOSED"))
        kb.add_comment(conn, tid, "apollo", "closing this out; CLOSED: CLEANUP -- not a contract token")
        with pytest.raises(op.ClosedUnmergedPrError) as exc:
            kb.archive_task(conn, tid)
        assert "archive refused" in str(exc.value) and "is not archived" in str(exc.value)
        assert _status(conn, tid) == "done"
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
        assert "archive_blocked_closed_unmerged_pr" in kinds


@pytest.mark.parametrize("comment", [
    "PR ANG-Ventures/r#5 CLOSED: REJECTED -- finding r1 killed it. card t_x by apollo",
    "PR #5 CLOSED: SUPERSEDED-BY #9 -- landed via #9",
])
def test_e2e_archive_passes_with_recorded_close_reason(board, no_survivor, monkeypatch, comment):
    with kb.connect() as conn:
        tid = _done_with_pr(conn, monkeypatch)
        monkeypatch.setattr(op, "_default_query", lambda: states(n5="CLOSED", n9="MERGED"))
        kb.add_comment(conn, tid, "apollo", comment)
        assert kb.archive_task(conn, tid)
        assert _status(conn, tid) == "archived"


def test_e2e_archive_without_pr_refs_makes_no_lookup(board, monkeypatch):
    def boom():
        raise AssertionError("no PR ref -> no GitHub lookup")
    monkeypatch.setattr(op, "_default_query", boom)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="plain", assignee="worker")
        assert kb.archive_task(conn, tid)


def test_cli_archive_and_request_review_report_the_refusal_without_traceback(board, no_survivor, monkeypatch,
                                                                              capsys):
    import argparse
    from hermes_cli import kanban as cli
    with kb.connect() as conn:
        tid = _done_with_pr(conn, monkeypatch)
    monkeypatch.setattr(op, "_default_query", lambda: states(n5="CLOSED"))
    assert cli._cmd_archive(argparse.Namespace(task_ids=[tid], purge_ids=None)) == 1
    err = capsys.readouterr().err
    assert f"cannot archive {tid}: archive refused" in err
    with kb.connect() as conn:
        tid2, _run = _claimed(conn)

    def refuse(*a, **k):
        raise op.ClosedUnmergedPrError(tid2, ["ANG-Ventures/r#5"])
    monkeypatch.setattr(kb, "request_review", refuse)
    monkeypatch.setattr(cli, "_goal_mode_handoff_rejection", lambda *a, **k: None)
    ns = argparse.Namespace(task_id=tid2, summary="s", metadata=None, reviewer=None, force=False,
                            allow_same_actor=False)
    assert cli._cmd_request_review(ns) == 1
    assert f"cannot request review for {tid2}: done refused" in capsys.readouterr().err
