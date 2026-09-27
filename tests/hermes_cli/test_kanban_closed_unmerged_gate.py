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


def test_merged_superseder_in_result_allows():
    assert op.enforce_not_closed_unmerged(
        "t_x", "superseded by ANG-Ventures/r#9", metadata={"pr_url": PR_URL},
        query_fn=states(n5="CLOSED", n9="MERGED"), sha_check=lambda r, s: False) != []


def test_unmerged_superseder_does_not_allow():
    with pytest.raises(op.ClosedUnmergedPrError):
        op.enforce_not_closed_unmerged("t_x", "superseded by ANG-Ventures/r#9", metadata={"pr_url": PR_URL},
                                       query_fn=states(n5="CLOSED", n9="OPEN"), sha_check=lambda r, s: False)


def test_closed_pr_cannot_supersede_itself():
    with pytest.raises(op.ClosedUnmergedPrError):
        op.enforce_not_closed_unmerged("t_x", f"see {PR_URL}", metadata={"pr_url": PR_URL},
                                       query_fn=states(n5="CLOSED"), sha_check=lambda r, s: False)


def test_sha_on_default_in_superseded_by_allows():
    seen = []
    assert op.enforce_not_closed_unmerged(
        "t_x", "done", metadata={"pr_url": PR_URL}, superseded_by="a149fe12a9",
        query_fn=states(n5="CLOSED"), sha_check=lambda r, s: seen.append((r, s)) or True) != []
    assert seen == [("ANG-Ventures/r", "a149fe12a9")]


def test_sha_not_on_default_refused():
    with pytest.raises(op.ClosedUnmergedPrError):
        op.enforce_not_closed_unmerged("t_x", "landed as a149fe12a9", metadata={"pr_url": PR_URL},
                                       query_fn=states(n5="CLOSED"), sha_check=lambda r, s: False)


def test_all_digit_token_is_not_a_sha():
    seen = []
    with pytest.raises(op.ClosedUnmergedPrError):
        op.enforce_not_closed_unmerged("t_x", "run 36329762 green", metadata={"pr_url": PR_URL},
                                       query_fn=states(n5="CLOSED"),
                                       sha_check=lambda r, s: seen.append(s) or True)
    assert seen == []


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


def test_unreadable_lookup_is_not_a_refusal():
    def boom(repo, n):
        raise RuntimeError("rate limited")
    assert op.enforce_not_closed_unmerged("t_x", "done", metadata={"pr_url": PR_URL}, query_fn=boom) == []


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
        assert kb.complete_task(conn, tid, summary="shipped", result="superseded by ANG-Ventures/r#9",
                                metadata={"pr_url": PR_URL}, expected_run_id=run)
        assert _status(conn, tid) == "done"


def test_e2e_merged_pr_completes(board, no_survivor, monkeypatch):
    monkeypatch.setattr(op, "_default_query", lambda: states(n5="MERGED"))
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert kb.complete_task(conn, tid, summary="shipped", metadata={"pr_url": PR_URL},
                                expected_run_id=run)
        assert _status(conn, tid) == "done"
