"""``--superseded-by <card>``: another card's merged PR superseded this card's closed PR (t_6a853d1c).

t_eb5bf009's fleet-ops-scripts#350 was closed ``CLOSED: SUPERSEDED-BY #351``. #351 is t_be729571's PR,
merged as f02c16eb, and edits the same spawn() block a different way. No commit carries #350's content
and #351 does not name t_eb5bf009, so every close form refused and the card could only be archived.

The case is accepted only when ALL hold: (1) that card is done, (2) its PR merged to the default
branch, (3) the closed PR's own thread carries ``CLOSED: SUPERSEDED-BY <that PR>``, (4) the two PRs'
changed files intersect. Each is broken on its own below and the close still refuses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_open_pr as op

REPO = "ANG-Ventures/fleet-ops-scripts"
CLOSED_PR = f"https://github.com/{REPO}/pull/350"
MERGED_PR = f"https://github.com/{REPO}/pull/351"
MERGE_SHA = "f02c16eb44bf667d407e6e325890cb4dabece1fd"
SHARED = "ci-runners/src/ci-runner-pool.sh"
FILES = {350: {SHARED, "ci-runners/test_pool_work_bind.py"},
         351: {SHARED, "ci-runners/test_pool_spawn_path.py", ".github/workflows/ci.yml"}}
RECORD = ("CLOSED: SUPERSEDED-BY #351 -- #351 (merged f02c16eb) moves the same per-spawn wipe off the "
          "spawn path [card t_eb5bf009]")


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_" + "HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _stub(monkeypatch, *, merged=True, on_default=True, record=RECORD, files=None, head_ref=None):
    """The t_eb5bf009 shape on GitHub, every oracle stubbed. Each kwarg breaks one condition.
    #352 is a third card's closed PR with the same record and files as #350 (the chain case)."""
    files = dict(FILES if files is None else files)
    files.setdefault(352, files.get(350, set()))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    def query(repo, n):
        if n == 351:
            out = {"state": "MERGED", "merge_commit_sha": MERGE_SHA} if merged else {"state": "CLOSED"}
            return dict(out, head_ref=head_ref) if head_ref else out
        return {"state": "CLOSED"} if n in (350, 352) else {"state": op.NOT_FOUND}
    monkeypatch.setattr(op, "_default_query", lambda: query)
    monkeypatch.setattr(op, "sha_on_default", lambda repo, sha: on_default)
    monkeypatch.setattr(op, "_default_close_record", lambda: None)
    monkeypatch.setattr(op, "_default_terminal_record", lambda: None)
    monkeypatch.setattr(op, "_default_superseded_targets",
                        lambda: (lambda repo, n: op._SUPERSEDED_LINE_RE.findall(record) if n in (350, 352) else []))
    monkeypatch.setattr(op, "_default_files", lambda: (lambda repo, n: set(files.get(n, ()))))


def _cards(conn, *, superseder_status="done"):
    """t_be729571 (merged #351) and t_eb5bf009 (running, own PR #350 closed)."""
    sup = kb.create_task(conn, title="spawn path", assignee="worker")
    kb.claim_task(conn, sup)
    run = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (sup,)).fetchone()[0]
    conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", ('{"pr_url": "%s"}' % MERGED_PR, run))
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (superseder_status, sup))
    tid = kb.create_task(conn, title="pacer", assignee="worker")
    kb.claim_task(conn, tid)
    run = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    conn.execute("UPDATE task_runs SET metadata=? WHERE id=?",
                 ('{"pr_url": "%s", "changed_files": ["%s"]}' % (CLOSED_PR, SHARED), run))
    conn.commit()
    return sup, tid, run


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]


def test_t_eb5bf009_shape_closes_superseded_with_both_prs_in_the_event(board, monkeypatch):
    _stub(monkeypatch)
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        assert kb.complete_task(conn, tid, expected_run_id=run, superseded_by=sup) is True
        assert _status(conn, tid) == "done"
        last = kb.list_runs(conn, tid)[-1]
        assert last.outcome == "superseded"
        assert last.metadata["survivor"]["kind"] == "ref"
        assert last.metadata["survivor"]["refs"][0]["sha"] == MERGE_SHA
        done = [e for e in kb.list_events(conn, tid) if e.kind == "completed"][-1].payload
        assert done["superseded_by"] == sup
        assert done["superseded_by_card"] == {"card": sup, "prs": [
            {"closed": f"{REPO}#350", "merged": f"{REPO}#351", "sha": MERGE_SHA}]}


def _refused(conn, tid, run, sup):
    with pytest.raises(op.ClosedUnmergedPrError, match="CLOSED WITHOUT MERGE"):
        kb.complete_task(conn, tid, expected_run_id=run, superseded_by=sup)
    assert _status(conn, tid) == "running"
    kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert "completion_blocked_closed_unmerged_pr" in kinds and "completed" not in kinds


@pytest.mark.parametrize("status", ["review", "running", "blocked", "archived"])
def test_mutation_1_superseding_card_not_done_refuses(board, monkeypatch, status):
    _stub(monkeypatch)
    with kb.connect() as conn:
        sup, tid, run = _cards(conn, superseder_status=status)
        _refused(conn, tid, run, sup)


@pytest.mark.parametrize("kw", [{"merged": False}, {"on_default": False}])
def test_mutation_2_superseding_pr_not_merged_to_default_refuses(board, monkeypatch, kw):
    _stub(monkeypatch, **kw)
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        _refused(conn, tid, run, sup)


@pytest.mark.parametrize("record", [
    "",                                                     # no close record at all
    "CLOSED: SUPERSEDED-BY #999 -- some other PR",          # names a different PR
    "superseded by #351, see there",                        # prose, not the contract line
    "CLOSED: ABANDONED -- #351 does it",                    # a different token
])
def test_mutation_3_closed_pr_lacks_superseded_by_that_pr_refuses(board, monkeypatch, record):
    _stub(monkeypatch, record=record)
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        _refused(conn, tid, run, sup)


def test_mutation_4_disjoint_changed_files_refuses(board, monkeypatch):
    _stub(monkeypatch, files={350: {"a.sh"}, 351: {"b.sh"}})
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        _refused(conn, tid, run, sup)


def test_unreadable_files_refuses(board, monkeypatch):
    _stub(monkeypatch)
    monkeypatch.setattr(op, "_default_files", lambda: (lambda repo, n: None))
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        _refused(conn, tid, run, sup)


def test_unknown_card_and_self_pointer_refuse(board, monkeypatch):
    _stub(monkeypatch)
    with kb.connect() as conn:
        _sup, tid, run = _cards(conn)
        _refused(conn, tid, run, "t_00000000")
        _refused(conn, tid, run, tid)


def test_a_bare_pr_pointer_is_unchanged(board, monkeypatch):
    """The other paths are unchanged: `--superseded-by owner/repo#351` (no card) still meets the old gates,
    here the merged-PR token, which accepts a merged PR exactly as before."""
    _stub(monkeypatch)
    with kb.connect() as conn:
        _sup, tid, run = _cards(conn)
        cover = []
        op.enforce_not_closed_unmerged(tid, metadata={"pr_url": CLOSED_PR}, superseded_by=f"{REPO}#351",
                                       query_fn=op._default_query(), card_covered_out=cover)
        assert cover == []


def test_superseding_card_parses_one_card_only():
    assert op.superseding_card("t_be729571") == "t_be729571"
    assert op.superseding_card("t_be729571 (fos#351)") == "t_be729571"
    assert op.superseding_card("t_be729571 or t_eb5bf009") is None
    assert op.superseding_card("ANG-Ventures/fleet-ops-scripts#351") is None
    assert op.superseding_card(None) is None


@pytest.mark.parametrize("head_ref", ["daedalus/t_be729571-spawn-path", "feature/no-card-id"])
def test_merged_pr_on_the_superseding_cards_branch_or_no_card_branch_still_covers(board, monkeypatch, head_ref):
    _stub(monkeypatch, head_ref=head_ref)
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        conn.execute("UPDATE tasks SET id=? WHERE id=?", ("t_be729571", sup))
        conn.execute("UPDATE task_runs SET task_id=? WHERE task_id=?", ("t_be729571", sup))
        conn.commit()
        assert kb.complete_task(conn, tid, expected_run_id=run, superseded_by="t_be729571") is True


def test_merged_pr_whose_branch_names_another_card_is_not_the_superseders_own(board, monkeypatch):
    """Rule 2 "its own PR": a recorded PR whose head branch names a DIFFERENT card was never its work."""
    _stub(monkeypatch, head_ref="daedalus/t_deadbeef-other-work")
    with kb.connect() as conn:
        sup, tid, run = _cards(conn)
        _refused(conn, tid, run, sup)


def test_supersession_does_not_chain_through_a_survivor_ref(board, monkeypatch):
    """Prism 7004158a070b: once B closes --superseded-by A, A's #351 sits in B's survivor.refs[].pr. A third
    card C (closed #352, `CLOSED: SUPERSEDED-BY #351`, same files) must not close --superseded-by B on it."""
    _stub(monkeypatch)
    with kb.connect() as conn:
        sup, b, b_run = _cards(conn)
        assert kb.complete_task(conn, b, expected_run_id=b_run, superseded_by=sup) is True
        assert any("#351" in str(r.get("pr")) for r in kb.list_runs(conn, b)[-1].metadata["survivor"]["refs"])
        c = kb.create_task(conn, title="third", assignee="worker")
        kb.claim_task(conn, c)
        c_run = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (c,)).fetchone()[0]
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=?",
                     ('{"pr_url": "https://github.com/%s/pull/352"}' % REPO, c_run))
        conn.commit()
        _refused(conn, c, c_run, b)
        # control: C's real superseder still covers it
        assert kb.complete_task(conn, c, expected_run_id=c_run, superseded_by=sup) is True


def test_authored_pr_refs_drops_survivor_evidence_only():
    md = {"pr_url": "o/r#1", "own_prs": ["o/r#2"],
          "survivor": {"refs": [{"pr": "o/r#3"}], "claims": [{"pr": "o/r#4"}]}}
    assert op.authored_pr_refs(md) == ["o/r#1", "o/r#2"]
    assert set(op.recorded_pr_refs(md)) == {"o/r#1", "o/r#2", "o/r#3", "o/r#4"}
    assert op.authored_pr_refs(None) == []
