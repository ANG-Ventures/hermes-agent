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


# --- FleetReview #1339 follow-ups ------------------------------------------


def test_every_primary_ref_is_checked_beyond_the_lookup_cap():
    n = op.MAX_LOOKUPS_PER_COMPLETION + 1
    urls = [f"https://github.com/ANG-Ventures/r/pull/{100 + i}" for i in range(n)]
    last = 100 + n - 1
    with pytest.raises(op.ClosedUnmergedPrError) as exc:
        op.enforce_not_closed_unmerged("t_x", "done", metadata={"pr_urls": urls},
                                       query_fn=lambda repo, k: {"state": "CLOSED" if k == last else "MERGED"},
                                       sha_check=lambda r, s: False)
    assert exc.value.closed == [f"ANG-Ventures/r#{last}"]


def test_recorded_pr_refs_reads_every_persisted_key():
    md = {"pr_url": "ANG-Ventures/r#1", "pr_urls": ["ANG-Ventures/r#2"], "pr": "ANG-Ventures/r#3",
          "own_prs": ["ANG-Ventures/r#4"],
          "survivor": {"kind": "patch", "refs": [{"pr": "ANG-Ventures/r#5"}], "claims": [{"pr": "ANG-Ventures/r#6"}]}}
    assert op.recorded_pr_refs(md) == [f"ANG-Ventures/r#{i}" for i in range(1, 7)]
    assert op.recorded_pr_refs(None) == []
    # FleetReview #1352: auto_routed_open_prs carries prose mentions too; it is not the card's own PR set.
    assert op.recorded_pr_refs({"auto_routed_open_prs": ["ANG-Ventures/x#12"], "own_prs": []}) == []
    # #1363 review 8b3bef2e5f1b: a legacy routed run (no own_prs key) keeps its only copy of --survivor-pr.
    assert op.recorded_pr_refs({"auto_routed_open_prs": ["ANG-Ventures/r#5"]}) == ["ANG-Ventures/r#5"]


def test_decision_must_name_each_closed_pr_when_card_owns_several():
    two = [PR_URL, "https://github.com/ANG-Ventures/r/pull/6"]
    closed_both = states(n5="CLOSED", n6="CLOSED")
    for decision in ("PR ANG-Ventures/r#5 CLOSED: REJECTED -- bad idea", "CLOSED: REJECTED -- bad idea"):
        with pytest.raises(op.ClosedUnmergedPrError):
            op.enforce_not_closed_unmerged("t_x", "done", recorded=two, query_fn=closed_both,
                                           sha_check=lambda r, s: False, verb="archive",
                                           decision_texts=[decision])
    assert op.enforce_not_closed_unmerged(
        "t_x", "done", recorded=two, query_fn=closed_both, sha_check=lambda r, s: False, verb="archive",
        decision_texts=["ANG-Ventures/r#5 CLOSED: REJECTED", "PR #6 CLOSED: ABANDONED"]) != []


def test_mixed_cover_superseder_for_one_decision_for_other():
    two = [PR_URL, "https://github.com/ANG-Ventures/r/pull/6"]
    assert op.enforce_not_closed_unmerged(
        "t_x", "done", recorded=two, query_fn=states(n5="CLOSED", n6="CLOSED", n9="MERGED"),
        sha_check=lambda r, s: False, verb="archive",
        decision_texts=["PR #5 CLOSED: SUPERSEDED-BY #9", "ANG-Ventures/r#6 CLOSED: STALE"]) != []


def test_unqualified_decision_covers_a_sole_pr():
    assert op.enforce_not_closed_unmerged(
        "t_x", "done", recorded=[PR_URL], query_fn=states(n5="CLOSED"), sha_check=lambda r, s: False,
        verb="archive", decision_texts=["CLOSED: THROWAWAY -- spike"]) != []


def test_e2e_review_approval_gated_on_pr_recorded_by_earlier_run(board, no_survivor, monkeypatch):
    # The implementer's run recorded the PR; the reviewer approves without repeating it.
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        monkeypatch.setattr(op, "_default_query", lambda: states(n5="OPEN"))
        assert kb.complete_task(conn, tid, summary="shipped", metadata={"pr_url": PR_URL}, expected_run_id=run)
        assert _status(conn, tid) == "review"
        monkeypatch.setattr(op, "_default_query", lambda: states(n5="CLOSED"))
        with pytest.raises(op.ClosedUnmergedPrError):
            kb.complete_task(conn, tid, summary="approved")
        assert _status(conn, tid) == "review"


@pytest.mark.parametrize("md", [
    {"own_prs": ["ANG-Ventures/r#5"]},
    {"survivor": {"kind": "ref", "refs": [{"pr": "ANG-Ventures/r#5", "state": "OPEN"}]}},
])
def test_e2e_archive_gated_on_route_and_survivor_evidence(board, no_survivor, monkeypatch, md):
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        monkeypatch.setattr(op, "_default_query", lambda: states(n5="MERGED"))
        assert kb.complete_task(conn, tid, summary="shipped", metadata=md, expected_run_id=run)
        monkeypatch.setattr(op, "_default_query", lambda: states(n5="CLOSED"))
        with pytest.raises(op.ClosedUnmergedPrError):
            kb.archive_task(conn, tid)


# --- FleetReview #1352 follow-ups ------------------------------------------


def test_decision_covers_only_the_pr_in_its_own_clause():
    two = [PR_URL, "https://github.com/ANG-Ventures/r/pull/6"]
    closed_both = states(n5="CLOSED", n6="CLOSED")
    for comment in ("PR #5 CLOSED: REJECTED; PR #6 still needs work",
                    "ANG-Ventures/r#5 CLOSED: REJECTED\nANG-Ventures/r#6 is being reworked"):
        with pytest.raises(op.ClosedUnmergedPrError) as exc:
            op.enforce_not_closed_unmerged("t_x", "done", recorded=two, query_fn=closed_both,
                                           sha_check=lambda r, s: False, verb="archive", decision_texts=[comment])
        assert exc.value.closed == ["ANG-Ventures/r#5", "ANG-Ventures/r#6"]
    # One clause naming both PRs is an explicit decision for both.
    assert op.enforce_not_closed_unmerged(
        "t_x", "done", recorded=two, query_fn=closed_both, sha_check=lambda r, s: False, verb="archive",
        decision_texts=["ANG-Ventures/r#5, ANG-Ventures/r#6 CLOSED: STALE"]) != []


def test_token_naming_its_own_target_as_subject_does_not_cover_the_sole_closed_pr():
    # FleetReview #1363 P1: "r#9 SUPERSEDED-BY r#9" names #9, not #5. Erasing the named #9 (it equals
    # the target) made the token unattributed, so it bound the sole closed PR #5.
    q = states(n5="CLOSED", n9="MERGED")
    for text in ("ANG-Ventures/r#9 SUPERSEDED-BY ANG-Ventures/r#9", "#9 SUPERSEDED-BY #9",
                 "SUPERSEDED-BY ANG-Ventures/r#9 (see ANG-Ventures/r#9)"):
        with pytest.raises(op.ClosedUnmergedPrError):
            op.enforce_not_closed_unmerged("t_x", text, recorded=[PR_URL], query_fn=q, sha_check=lambda r, s: False)
    # The sole-PR fallback still holds for a genuinely unattributed token.
    assert op.enforce_not_closed_unmerged("t_x", "SUPERSEDED-BY ANG-Ventures/r#9", recorded=[PR_URL],
                                          query_fn=q, sha_check=lambda r, s: False) != []


def test_one_superseder_token_does_not_cover_a_second_closed_pr():
    two = [PR_URL, "https://github.com/ANG-Ventures/r/pull/6"]
    q = states(n5="CLOSED", n6="CLOSED", n9="MERGED", n10="MERGED")
    for text in ("ANG-Ventures/r#5 SUPERSEDED-BY #9", "SUPERSEDED-BY #9"):
        with pytest.raises(op.ClosedUnmergedPrError):
            op.enforce_not_closed_unmerged("t_x", text, recorded=two, query_fn=q, sha_check=lambda r, s: False)
    with pytest.raises(op.ClosedUnmergedPrError):  # the archive twin of the #1339 mixed test, minus #6's decision
        op.enforce_not_closed_unmerged("t_x", "done", recorded=two, query_fn=q, sha_check=lambda r, s: False,
                                       verb="archive", decision_texts=["PR #5 CLOSED: SUPERSEDED-BY #9"])
    with pytest.raises(op.ClosedUnmergedPrError):  # --superseded-by without a subject is ambiguous here
        op.enforce_not_closed_unmerged("t_x", "done", recorded=two, query_fn=q, sha_check=lambda r, s: False,
                                       superseded_by="ANG-Ventures/r#9")
    for text in ("ANG-Ventures/r#5 SUPERSEDED-BY #9; ANG-Ventures/r#6 RE-CARRIED-AS #10",
                 "ANG-Ventures/r#5 SUPERSEDED-BY #9 and ANG-Ventures/r#6 RE-CARRIED-AS #10",
                 "SUPERSEDED-BY #9 (was #5)\nRE-CARRIED-AS #10 (was #6)"):
        assert op.enforce_not_closed_unmerged("t_x", text, recorded=two, query_fn=q,
                                              sha_check=lambda r, s: False) != [], text
    assert op.enforce_not_closed_unmerged("t_x", "done", recorded=two, query_fn=q, sha_check=lambda r, s: False,
                                          superseded_by="ANG-Ventures/r#5 SUPERSEDED-BY #9; #6 SUPERSEDED-BY #10") != []


def test_unattributed_token_covers_the_sole_closed_pr_of_several_own_prs():
    two = [PR_URL, "https://github.com/ANG-Ventures/r/pull/6"]
    assert op.enforce_not_closed_unmerged(
        "t_x", "SUPERSEDED-BY #9", recorded=two, query_fn=states(n5="MERGED", n6="CLOSED", n9="MERGED"),
        sha_check=lambda r, s: False) == [op.extract_pr_refs("ANG-Ventures/r#6")[0]]


def test_e2e_prose_mentioned_pr_closed_elsewhere_does_not_gate(board, no_survivor, monkeypatch):
    # Routed to review on the card's own open PR; the handoff also names another team's open PR.
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        monkeypatch.setattr(op, "_default_query", lambda: states(n5="OPEN", n12="OPEN"))
        assert kb.complete_task(conn, tid, summary="shipped; depends on ANG-Ventures/x#12",
                                metadata={"pr_url": PR_URL}, expected_run_id=run)
        assert _status(conn, tid) == "review"
        meta = kb.list_runs(conn, tid)[-1].metadata
        assert "ANG-Ventures/x#12" in meta["auto_routed_open_prs"] and meta["own_prs"] == ["ANG-Ventures/r#5"]
        monkeypatch.setattr(op, "_default_query", lambda: states(n5="MERGED", n12="CLOSED"))
        assert kb.complete_task(conn, tid, summary="approved")
        assert _status(conn, tid) == "done"
        assert kb.archive_task(conn, tid)


def test_e2e_survivor_pr_only_route_still_gates_approval(board, monkeypatch):
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        monkeypatch.setattr(op, "_default_query", lambda: states(n5="OPEN"))
        assert kb.complete_task(conn, tid, summary="shipped", survivor_pr="ANG-Ventures/r#5", expected_run_id=run)
        assert _status(conn, tid) == "review"
        monkeypatch.setattr(op, "_default_query", lambda: states(n5="CLOSED"))
        with pytest.raises(op.ClosedUnmergedPrError):
            kb.complete_task(conn, tid, summary="approved")
        assert _status(conn, tid) == "review"


def test_mixed_style_tokens_in_one_clause_bind_nothing_for_several_prs():
    # #1363 review cd058b2a2992: the post-style subject of token 1 must not swallow token 2's PR.
    two = [PR_URL, "https://github.com/ANG-Ventures/r/pull/6"]
    q = states(n5="CLOSED", n6="CLOSED", n9="MERGED", n10="OPEN")
    with pytest.raises(op.ClosedUnmergedPrError):
        op.enforce_not_closed_unmerged("t_x", "SUPERSEDED-BY #9 (was #5) and ANG-Ventures/r#6 RE-CARRIED-AS #10",
                                       recorded=two, query_fn=q, sha_check=lambda r, s: False)


def test_previous_token_target_is_not_the_next_tokens_subject():
    # #1363 review 11069765a188: the card owns closed #9 and #6; only #6's re-carry (#10) merged.
    own = ["https://github.com/ANG-Ventures/r/pull/9", "https://github.com/ANG-Ventures/r/pull/6"]
    q = states(n5="CLOSED", n6="CLOSED", n9="CLOSED", n10="MERGED")
    with pytest.raises(op.ClosedUnmergedPrError) as exc:
        op.enforce_not_closed_unmerged("t_x", "PR #5 SUPERSEDED-BY #9 and PR #6 RE-CARRIED-AS #10",
                                       recorded=own, query_fn=q, sha_check=lambda r, s: False)
    assert exc.value.closed == ["ANG-Ventures/r#9", "ANG-Ventures/r#6"]
    assert op._unsuperseded([op.extract_pr_refs("ANG-Ventures/r#9")[0], op.extract_pr_refs("ANG-Ventures/r#6")[0]],
                            "PR #5 SUPERSEDED-BY #9 and PR #6 RE-CARRIED-AS #10", query_fn=q,
                            sha_check=lambda r, s: False) == [op.extract_pr_refs("ANG-Ventures/r#9")[0]]


def test_ambiguous_clause_naming_another_pr_never_covers_the_sole_pr():
    # #1363 review 610e8af06dcf: the card owns closed r#5; the clause is about #6.
    q = states(n5="CLOSED", n6="CLOSED", n9="MERGED", n10="MERGED")
    for text in ("PR #6 SUPERSEDED-BY #9 and RE-CARRIED-AS #10", "SUPERSEDED-BY #9 and PR #6 RE-CARRIED-AS #10"):
        with pytest.raises(op.ClosedUnmergedPrError):
            op.enforce_not_closed_unmerged("t_x", text, metadata={"pr_url": PR_URL}, query_fn=q,
                                           sha_check=lambda r, s: False)
    for d in ("PR #6 CLOSED: STALE and CLOSED: REJECTED",):
        with pytest.raises(op.ClosedUnmergedPrError):
            op.enforce_not_closed_unmerged("t_x", "done", recorded=[PR_URL], query_fn=q,
                                           sha_check=lambda r, s: False, verb="archive", decision_texts=[d])
    # No other PR named: still an unattributed cover for the sole PR.
    assert op.enforce_not_closed_unmerged("t_x", "SUPERSEDED-BY #9 and RE-CARRIED-AS #10",
                                          metadata={"pr_url": PR_URL}, query_fn=q,
                                          sha_check=lambda r, s: False) != []


def test_e2e_legacy_routed_survivor_only_card_still_gated(board, no_survivor, monkeypatch):
    # A run routed before own_prs existed persisted the survivor PR only in auto_routed_open_prs.
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        monkeypatch.setattr(op, "_default_query", lambda: states(n5="MERGED"))
        assert kb.complete_task(conn, tid, summary="shipped",
                                metadata={"auto_routed_open_prs": ["ANG-Ventures/r#5"]}, expected_run_id=run)
        monkeypatch.setattr(op, "_default_query", lambda: states(n5="CLOSED"))
        with pytest.raises(op.ClosedUnmergedPrError):
            kb.archive_task(conn, tid)


# --- t_9aff2951: merged --survivor-pr + the closed PR's own CLOSED: <TOKEN> record --------------------------


def merged(**by_number):
    """query_fn stub where MERGED numbers carry a merge sha (``abc<n>def``)."""
    def q(repo, n):
        state = by_number.get(f"n{n}", op.NOT_FOUND)
        return {"state": state, "merge_commit_sha": f"abc{n}def"} if state == "MERGED" else {"state": state}
    return q


SURVIVOR = "ANG-Ventures/r#9"


def test_has_close_record_matches_the_contract_tokens_only():
    assert op.has_close_record("CLOSED: RE-CARRIED-AS #9 -- folded into #9")
    assert op.has_close_record("note\nCLOSED: SUPERSEDED-BY #9 -- landed via #9 (a56c889)")
    assert op.has_close_record("CLOSED: DUPLICATE-OF #9 -- same change")
    assert not op.has_close_record("CLOSED: CLEANUP -- not a contract token")
    assert not op.has_close_record("closing, superseded by #9")
    assert not op.has_close_record(None)


def test_merged_survivor_plus_close_record_allows_done():
    # t_3ec660ca shape: #5 closed with 'CLOSED: RE-CARRIED-AS #9', --survivor-pr #9 merged on default.
    seen = []

    def record(repo, n):
        seen.append((repo, n))
        return True
    assert op.enforce_not_closed_unmerged(
        "t_x", "done", metadata={"pr_url": PR_URL}, survivor_pr=SURVIVOR,
        query_fn=merged(n5="CLOSED", n9="MERGED"), sha_check=lambda r, s: s == "abc9def",
        close_record_fn=record) != []
    assert seen == [("ANG-Ventures/r", 5)]


def test_merged_survivor_without_close_record_refuses_and_names_the_pr():
    with pytest.raises(op.ClosedUnmergedPrError) as exc:
        op.enforce_not_closed_unmerged(
            "t_x", "done", metadata={"pr_url": PR_URL}, survivor_pr=SURVIVOR,
            query_fn=merged(n5="CLOSED", n9="MERGED"), sha_check=lambda r, s: True,
            close_record_fn=lambda r, n: False)
    msg = str(exc.value)
    assert "ANG-Ventures/r#5 is CLOSED WITHOUT MERGE, so the work is not on the default branch" in msg
    assert exc.value.untokened == ["ANG-Ventures/r#5"]
    assert "does not cover ANG-Ventures/r#5" in msg and "CLOSED: <TOKEN>" in msg


def test_close_record_unreadable_refuses():
    with pytest.raises(op.ClosedUnmergedPrError) as exc:
        op.enforce_not_closed_unmerged(
            "t_x", "done", metadata={"pr_url": PR_URL}, survivor_pr=SURVIVOR,
            query_fn=merged(n5="CLOSED", n9="MERGED"), sha_check=lambda r, s: True,
            close_record_fn=lambda r, n: None)
    assert exc.value.untokened == ["ANG-Ventures/r#5"]


@pytest.mark.parametrize("n9,on_default", [("OPEN", True), ("CLOSED", True), ("MERGED", False)])
def test_survivor_must_be_merged_on_default(n9, on_default):
    with pytest.raises(op.ClosedUnmergedPrError) as exc:
        op.enforce_not_closed_unmerged(
            "t_x", "done", metadata={"pr_url": PR_URL}, survivor_pr=SURVIVOR,
            query_fn=merged(n5="CLOSED", n9=n9), sha_check=lambda r, s: on_default,
            close_record_fn=lambda r, n: True)
    assert exc.value.untokened == []


def test_close_record_without_survivor_still_refuses_done():
    with pytest.raises(op.ClosedUnmergedPrError):
        op.enforce_not_closed_unmerged(
            "t_x", "done", metadata={"pr_url": PR_URL},
            query_fn=merged(n5="CLOSED"), sha_check=lambda r, s: True,
            close_record_fn=lambda r, n: True)


def test_every_closed_pr_needs_its_own_close_record():
    two = [PR_URL, "https://github.com/ANG-Ventures/r/pull/6"]
    with pytest.raises(op.ClosedUnmergedPrError) as exc:
        op.enforce_not_closed_unmerged(
            "t_x", "done", recorded=two, survivor_pr=SURVIVOR,
            query_fn=merged(n5="CLOSED", n6="CLOSED", n9="MERGED"), sha_check=lambda r, s: True,
            close_record_fn=lambda r, n: n == 5)
    assert exc.value.untokened == ["ANG-Ventures/r#6"]


def test_e2e_complete_with_merged_survivor_and_close_record(board, no_survivor, monkeypatch):
    # t_60269760 shape: the card is blocked on its closed PR; the operator completes with --survivor-pr.
    monkeypatch.setattr(op, "_default_query", lambda: merged(n5="CLOSED", n9="MERGED"))
    monkeypatch.setattr(op, "_default_sha_check", lambda: (lambda r, s: True))
    monkeypatch.setattr(op, "_default_close_record", lambda: (lambda r, n: True))
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        with pytest.raises(op.ClosedUnmergedPrError):  # no survivor named yet
            kb.complete_task(conn, tid, summary="shipped", metadata={"pr_url": PR_URL}, expected_run_id=run)
        assert kb.complete_task(conn, tid, summary="closing on the merged survivor",
                                survivor_pr=SURVIVOR, metadata={"pr_url": PR_URL}, expected_run_id=run)
        assert _status(conn, tid) == "done"


def test_e2e_complete_with_merged_survivor_without_close_record_refused(board, no_survivor, monkeypatch):
    monkeypatch.setattr(op, "_default_query", lambda: merged(n5="CLOSED", n9="MERGED"))
    monkeypatch.setattr(op, "_default_sha_check", lambda: (lambda r, s: True))
    monkeypatch.setattr(op, "_default_close_record", lambda: (lambda r, n: False))
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        with pytest.raises(op.ClosedUnmergedPrError) as exc:
            kb.complete_task(conn, tid, summary="closing", survivor_pr=SURVIVOR,
                             metadata={"pr_url": PR_URL}, expected_run_id=run)
        assert "does not cover ANG-Ventures/r#5" in str(exc.value)
        assert _status(conn, tid) == "running"


def test_e2e_archive_with_merged_own_pr_and_close_record(board, no_survivor, monkeypatch):
    # Archive has no --survivor-pr: a MERGED own PR of the card is the survivor.
    with kb.connect() as conn:
        tid2, run2 = _claimed(conn)
        monkeypatch.setattr(op, "_default_query", lambda: merged(n5="MERGED", n9="MERGED"))
        assert kb.complete_task(conn, tid2, summary="s", metadata={"pr_urls": [PR_URL, "ANG-Ventures/r#9"]},
                                expected_run_id=run2)
        monkeypatch.setattr(op, "_default_query", lambda: merged(n5="CLOSED", n9="MERGED"))
        monkeypatch.setattr(op, "_default_sha_check", lambda: (lambda r, s: True))
        monkeypatch.setattr(op, "_default_close_record", lambda: (lambda r, n: False))
        with pytest.raises(op.ClosedUnmergedPrError):
            kb.archive_task(conn, tid2)
        monkeypatch.setattr(op, "_default_close_record", lambda: (lambda r, n: True))
        assert kb.archive_task(conn, tid2)
        assert _status(conn, tid2) == "archived"
