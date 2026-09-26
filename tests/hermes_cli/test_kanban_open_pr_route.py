"""A completion naming a still-OPEN PR lands in ``review``, never ``done``.

Incident t_1bd02e0b (2026-09-25): a worker completed its card while its PR was
OPEN; the card's children unblocked and nobody owned the merge.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_open_pr as op

PR_URL = "https://github.com/ANG-Ventures/example-home/pull/670"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _stub(states: dict):
    calls = []

    def query(repo, number):
        calls.append((repo, number))
        state = states.get((repo.lower(), number))
        if isinstance(state, Exception):
            raise state
        return None if state is None else {"state": state}

    query.calls = calls
    return query


def _use_oracle(monkeypatch, states):
    q = _stub(states)
    monkeypatch.setattr(op, "_default_query", lambda: q)
    return q


def _parent_child(conn):
    parent = kb.create_task(conn, title="impl", assignee="worker")
    child = kb.create_task(conn, title="follow-on", assignee="worker", parents=[parent])
    kb.claim_task(conn, parent)
    return parent, child


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]


def _kinds(conn, tid):
    return [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]


# --- unit: extraction ------------------------------------------------------


def test_extract_url_qualified_metadata_and_survivor_dedup():
    refs = op.extract_pr_refs(
        f"shipped, see {PR_URL} and Kyzcreig/other#12; also bare #99",
        "summary repeats ANG-Ventures/example-home#670",
        metadata={"pr_url": ["https://github.com/o/r/pull/5"]},
        survivor_pr="o/r#6",
    )
    assert [(r.repo, r.number) for r in refs] == [
        ("ANG-Ventures/example-home", 670), ("Kyzcreig/other", 12),
        ("o/r", 5), ("o/r", 6),
    ]


def test_extract_ignores_bare_numbers_and_empty():
    assert op.extract_pr_refs("fixed #12 and pull/13", None, metadata=None) == []
    assert op.extract_pr_refs(None, "") == []


# --- unit: state check -----------------------------------------------------


def test_open_refs_keeps_only_open():
    refs = op.extract_pr_refs("a/b#1 a/b#2 a/b#3")
    q = _stub({("a/b", 1): "OPEN", ("a/b", 2): "MERGED", ("a/b", 3): "CLOSED"})
    assert [r.number for r in op.open_refs(refs, query_fn=q)] == [1]


def test_open_refs_fails_open_on_lookup_error_and_logs(caplog):
    refs = op.extract_pr_refs("a/b#1 a/b#2")
    q = _stub({("a/b", 1): RuntimeError("gh down"), ("a/b", 2): None})
    with caplog.at_level("WARNING"):
        assert op.open_refs(refs, query_fn=q) == []
    assert "fail-open" in caplog.text


def test_default_oracle_is_disabled_under_pytest():
    assert op._default_query() is None
    assert op.open_pr_refs(PR_URL) == []  # no network from the unit suite


# --- E2E through complete_task on a temp board -----------------------------


def test_complete_with_open_pr_routes_to_review_and_keeps_child_gated(kanban_home, monkeypatch):
    q = _use_oracle(monkeypatch, {("ang-ventures/example-home", 670): "OPEN"})
    with kb.connect() as conn:
        parent, child = _parent_child(conn)
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (parent,)).fetchone()[0]
        ok = kb.complete_task(conn, parent, summary=f"Phase 0 done: {PR_URL}",
                              expected_run_id=run_id)
        assert ok is True
        assert _status(conn, parent) == "review"
        assert _status(conn, child) == "todo"
        kinds = _kinds(conn, parent)
        assert "completion_routed_to_review" in kinds
        assert "review_requested" in kinds and "completed" not in kinds
        run = conn.execute("SELECT outcome, summary FROM task_runs WHERE task_id=? "
                           "ORDER BY id DESC LIMIT 1", (parent,)).fetchone()
        assert run["outcome"] == "review_requested"
        assert run["summary"].startswith("auto-routed: PR ANG-Ventures/example-home#670 still open")
    assert q.calls == [("ANG-Ventures/example-home", 670)]


def test_complete_with_merged_pr_goes_done_and_releases_child(kanban_home, monkeypatch):
    _use_oracle(monkeypatch, {("ang-ventures/example-home", 670): "MERGED"})
    with kb.connect() as conn:
        parent, child = _parent_child(conn)
        assert kb.complete_task(conn, parent, summary=f"landed {PR_URL}") is True
        assert _status(conn, parent) == "done"
        assert _status(conn, child) == "ready"
        assert "completion_routed_to_review" not in _kinds(conn, parent)


def test_complete_fails_open_when_lookup_errors(kanban_home, monkeypatch):
    _use_oracle(monkeypatch, {("ang-ventures/example-home", 670): RuntimeError("boom")})
    with kb.connect() as conn:
        parent, _child = _parent_child(conn)
        assert kb.complete_task(conn, parent, summary=PR_URL) is True
        assert _status(conn, parent) == "done"


def test_open_pr_in_metadata_pr_url_is_caught(kanban_home, monkeypatch):
    _use_oracle(monkeypatch, {("o/r", 5): "OPEN"})
    with kb.connect() as conn:
        parent, _child = _parent_child(conn)
        assert kb.complete_task(conn, parent, summary="done",
                                metadata={"pr_url": "https://github.com/o/r/pull/5"}) is True
        assert _status(conn, parent) == "review"


def test_review_approval_is_not_rerouted(kanban_home, monkeypatch):
    """A card already in review being approved must reach done even if the PR is open."""
    _use_oracle(monkeypatch, {("ang-ventures/example-home", 670): "OPEN"})
    with kb.connect() as conn:
        parent, _child = _parent_child(conn)
        assert kb.complete_task(conn, parent, summary=PR_URL) is True
        assert _status(conn, parent) == "review"
        assert kb.complete_task(conn, parent, summary="approved") is True
        assert _status(conn, parent) == "done"


def test_milestone_only_policy_skip_still_routes_open_pr_to_review(kanban_home, monkeypatch):
    """request_review under milestone_only completes slice cards in place;
    an OPEN PR must still land the card in review, not done."""
    _use_oracle(monkeypatch, {("ang-ventures/example-home", 670): "OPEN"})
    monkeypatch.setattr(kb, "configured_review_policy", lambda: "milestone_only")
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="slice", assignee="worker")
        kb.claim_task(conn, tid)
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        ok, reason = kb.request_review(conn, tid, summary=f"PR {PR_URL}",
                                       expected_run_id=run_id, with_reason=True)
        assert ok is True, reason
        assert _status(conn, tid) == "review"
        assert "review_skipped" not in _kinds(conn, tid)


# --- lint ------------------------------------------------------------------


def test_lint_lists_recent_done_cards_naming_open_pr(kanban_home):
    with kb.connect() as conn:
        a = kb.create_task(conn, title="slipped", assignee="w")
        b = kb.create_task(conn, title="merged fine", assignee="w")
        c = kb.create_task(conn, title="no pr", assignee="w")
        old = kb.create_task(conn, title="old", assignee="w")
        for tid, text in ((a, PR_URL), (b, "o/r#2"), (c, "nothing"), (old, PR_URL)):
            kb.complete_task(conn, tid, result=text)  # pytest: routing oracle disabled
        conn.execute("UPDATE tasks SET completed_at = completed_at - 30*86400 WHERE id=?", (old,))
        conn.commit()
        q = _stub({("ang-ventures/example-home", 670): "OPEN", ("o/r", 2): "MERGED"})
        hits = op.find_done_with_open_pr(conn, query_fn=q)
    assert [h["id"] for h in hits] == [a]
    assert hits[0]["open_prs"] == ["ANG-Ventures/example-home#670"]
    # one lookup per distinct PR, old card outside the window never queried
    assert sorted(q.calls) == [("ANG-Ventures/example-home", 670), ("o/r", 2)]
