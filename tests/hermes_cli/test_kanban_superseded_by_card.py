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


def _stub(monkeypatch, *, merged=True, on_default=True, record=RECORD, files=None):
    """The t_eb5bf009 shape on GitHub, every oracle stubbed. Each kwarg breaks one condition."""
    files = FILES if files is None else files
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    def query(repo, n):
        if n == 351:
            return {"state": "MERGED", "merge_commit_sha": MERGE_SHA} if merged else {"state": "CLOSED"}
        return {"state": "CLOSED"} if n == 350 else {"state": op.NOT_FOUND}
    monkeypatch.setattr(op, "_default_query", lambda: query)
    monkeypatch.setattr(op, "sha_on_default", lambda repo, sha: on_default)
    monkeypatch.setattr(op, "_default_close_record", lambda: None)
    monkeypatch.setattr(op, "_default_terminal_record", lambda: None)
    monkeypatch.setattr(op, "_default_superseded_targets",
                        lambda: (lambda repo, n: op._SUPERSEDED_LINE_RE.findall(record) if n == 350 else []))
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
