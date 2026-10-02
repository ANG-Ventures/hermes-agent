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
FOREIGN_URL = "https://github.com/stephenschoettler/hermes-lcm/pull/638"


@pytest.fixture(autouse=True)
def _fixture_owners_are_fleet(monkeypatch):
    """The ``o/r`` / ``a/b`` fixture repos stand in for fleet repos (t_06dccfe3)."""
    monkeypatch.setattr(op, "FLEET_OWNERS", op.FLEET_OWNERS | {"o", "a"})


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


@pytest.mark.parametrize("failure", [RuntimeError("boom"), None])
def test_complete_fails_closed_when_lookup_cannot_read_state(kanban_home, monkeypatch, failure):
    """t_36d0114e: the worker lane's gh READ cap made every lookup time out;
    fail-open wrote ``done`` over 14 open PRs. Unreadable state -> review."""
    _use_oracle(monkeypatch, {("ang-ventures/example-home", 670): failure})
    with kb.connect() as conn:
        parent, child = _parent_child(conn)
        assert kb.complete_task(conn, parent, summary=PR_URL) is True
        assert _status(conn, parent) == "review"
        assert _status(conn, child) == "todo"


def test_complete_with_not_found_ref_goes_done(kanban_home, monkeypatch):
    """A definite 404 (the ref names no PR) is not a reason to hold the card."""
    _use_oracle(monkeypatch, {("ang-ventures/example-home", 670): op.NOT_FOUND})
    with kb.connect() as conn:
        parent, _child = _parent_child(conn)
        assert kb.complete_task(conn, parent, summary=PR_URL) is True
        assert _status(conn, parent) == "done"


def _comments(conn, tid):
    return [r["body"] for r in conn.execute(
        "SELECT body FROM task_comments WHERE task_id=? ORDER BY id", (tid,))]


def test_survivor_pr_open_routes_to_review_with_one_comment(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "configured_review_assignee", lambda: "human:apollo")
    _use_oracle(monkeypatch, {("o/r", 7): "OPEN"})
    with kb.connect() as conn:
        parent, child = _parent_child(conn)
        assert kb.complete_task(conn, parent, summary="shipped", survivor_pr="o/r#7") is True
        row = conn.execute("SELECT status, assignee FROM tasks WHERE id=?", (parent,)).fetchone()
        assert (row["status"], row["assignee"]) == ("review", "human:apollo")
        assert _status(conn, child) == "todo"
        assert _comments(conn, parent) == [
            "survivor PR open; card closes on merged=true (o/r#7)"]


def test_survivor_pr_merged_goes_done(kanban_home, monkeypatch):
    q = _use_oracle(monkeypatch, {("o/r", 7): "MERGED"})
    monkeypatch.setattr("hermes_cli.kanban_survivor.preserve", lambda *a, **k: None)
    with kb.connect() as conn:
        parent, child = _parent_child(conn)
        assert kb.complete_task(conn, parent, summary="landed", survivor_pr="o/r#7") is True
        assert _status(conn, parent) == "done"
        assert _status(conn, child) == "ready"
        assert _comments(conn, parent) == []
    assert q.calls == [("o/r", 7)]


def test_primary_refs_are_checked_past_the_prose_cap():
    prose = " ".join(f"a/b#{n}" for n in range(1, 16))
    q = _stub({("o/r", 7): "OPEN", **{("a/b", n): "MERGED" for n in range(1, 16)}})
    got = op.open_pr_refs(prose, survivor_pr="o/r#7", query_fn=q)
    assert [(r.repo, r.number) for r in got] == [("o/r", 7)]
    assert len([c for c in q.calls if c[0] == "a/b"]) == op.MAX_LOOKUPS_PER_COMPLETION


class _Proc:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


@pytest.mark.parametrize("proc, want", [
    (_Proc(0, json.dumps({"state": "open", "merged_at": None})), {"state": "OPEN"}),
    (_Proc(0, json.dumps({"state": "closed", "merged_at": "2026-09-27T00:00:00Z"})), {"state": "MERGED"}),
    (_Proc(1, '{"message":"Not Found","status":"404"}', "gh: Not Found (HTTP 404)"), {"state": op.NOT_FOUND}),
    (_Proc(75, "", "cap exceeded"), None),
    (_Proc(0, "not json"), None),
])
def test_query_pr_state_separates_not_found_from_unknown(monkeypatch, proc, want):
    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: proc)
    assert op.query_pr_state("o/r", 1) == want


def test_query_pr_state_timeout_is_unknown(monkeypatch):
    def boom(*a, **k):
        raise op.subprocess.TimeoutExpired("gh", 10)
    monkeypatch.setattr(op.subprocess, "run", boom)
    assert op.query_pr_state("o/r", 1) is None


def test_reopen_done_card_to_review_assigns_review_assignee(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "configured_review_assignee", lambda: "human:apollo")
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="stranded", assignee="worker")
        assert kb.complete_task(conn, tid, result=PR_URL) is True  # oracle off under pytest
        assert _status(conn, tid) == "done"
        ok, err = kb.reopen_task(conn, tid, actor="closer", reason="PR still open",
                                 to_status="review")
        assert ok, err
        row = conn.execute("SELECT status, assignee, completed_at FROM tasks WHERE id=?",
                           (tid,)).fetchone()
        assert (row["status"], row["assignee"], row["completed_at"]) == ("review", "human:apollo", None)
        assert kb.reopen_task(conn, tid, actor="c", reason="x", to_status="blocked")[0] is False


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


def _same_actor_changes_requested(conn, monkeypatch):
    """Seed t_503df7c5's history: a review drain returned the card with
    ``changes_requested`` reviewer == implementer, then the worker re-claimed."""
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(kb, "spawnable_reviewer_profiles", lambda: ["worker", "argus"])
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    monkeypatch.setattr(kb, "configured_review_assignee", lambda: "argus")
    tid = kb.create_task(conn, title="slice", assignee="worker")
    kb.claim_task(conn, tid)
    rid = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET outcome='changes_requested', status='ready', "
                     "ended_at=1 WHERE id=?", (rid,))
        conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL, "
                     "claim_lock=NULL WHERE id=?", (tid,))
        kb._append_event(conn, tid, "changes_requested",
                         {"reason": "rework", "implementer": "worker",
                          "reviewer": "worker", "status": "ready"}, run_id=rid)
    kb.claim_task(conn, tid)
    run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    return tid, run_id


def test_open_pr_route_survives_same_actor_review_provenance(kanban_home, monkeypatch):
    """t_503df7c5: status=running, current_run_id == the worker's run id, yet
    complete_task returned False ('unknown id or already terminal') because the
    open-PR route inherited reviewer == implementer from changes_requested."""
    _use_oracle(monkeypatch, {("ang-ventures/example-home", 670): "OPEN"})
    with kb.connect() as conn:
        tid, run_id = _same_actor_changes_requested(conn, monkeypatch)
        assert kb.complete_task(conn, tid, summary=f"done {PR_URL}",
                                expected_run_id=run_id) is True
        row = conn.execute("SELECT status, assignee FROM tasks WHERE id=?", (tid,)).fetchone()
        assert (row["status"], row["assignee"]) == ("review", "argus")
        assert "completion_routed_to_review" in _kinds(conn, tid)


def test_milestone_only_in_place_completion_uses_worker_run_id(kanban_home, monkeypatch):
    """request_review under milestone_only passes the SAME expected_run_id into
    complete_task, and completes a same-actor-provenance card in place."""
    _use_oracle(monkeypatch, {})
    monkeypatch.setattr(kb, "configured_review_policy", lambda: "milestone_only")
    with kb.connect() as conn:
        tid, run_id = _same_actor_changes_requested(conn, monkeypatch)
        ok, reason = kb.request_review(conn, tid, summary="done",
                                       expected_run_id=run_id + 1, with_reason=True)
        assert ok is False and "expected_run_id" in reason
        assert _status(conn, tid) == "running"
        ok, reason = kb.request_review(conn, tid, summary="done",
                                       metadata={"tests_run": 1},
                                       expected_run_id=run_id, with_reason=True)
        assert ok is True, reason
        assert _status(conn, tid) == "done"


def test_refused_open_pr_route_records_reason_event(kanban_home, monkeypatch):
    _use_oracle(monkeypatch, {("ang-ventures/example-home", 670): "OPEN"})
    monkeypatch.setattr(kb, "resolve_reviewer",
                        lambda *a, **k: (None, "reviewer gate says no"))
    with kb.connect() as conn:
        tid, run_id = _same_actor_changes_requested(conn, monkeypatch)
        assert kb.complete_task(conn, tid, summary=PR_URL, expected_run_id=run_id) is False
        payload = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='completion_route_refused'",
            (tid,)).fetchone()["payload"])
        assert payload["reason"] == "reviewer gate says no"
        assert _status(conn, tid) == "running"


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


# --- foreign-owner PRs are mentions, not a gate (t_06dccfe3) ----------------


def test_fleet_owner_match_is_case_insensitive():
    refs = op.extract_pr_refs(
        "ang-ventures/x#1 KYZCREIG/y#2 NousResearch/hermes-agent#3", FOREIGN_URL)
    fleet, foreign = op.split_fleet(refs)
    assert [(r.repo, r.number) for r in fleet] == [("ang-ventures/x", 1), ("KYZCREIG/y", 2)]
    assert [(r.repo, r.number) for r in foreign] == [
        ("NousResearch/hermes-agent", 3), ("stephenschoettler/hermes-lcm", 638)]


def test_open_pr_refs_never_looks_up_a_foreign_ref():
    q = _stub({("stephenschoettler/hermes-lcm", 638): "OPEN"})
    assert op.open_pr_refs(FOREIGN_URL, survivor_pr="NousResearch/x#1", query_fn=q) == []
    assert q.calls == []


def test_complete_naming_only_foreign_open_pr_goes_done_with_mention(kanban_home, monkeypatch):
    q = _use_oracle(monkeypatch, {("stephenschoettler/hermes-lcm", 638): "OPEN"})
    with kb.connect() as conn:
        parent, child = _parent_child(conn)
        assert kb.complete_task(conn, parent,
                                summary=f"upstreamed as {FOREIGN_URL}") is True
        assert _status(conn, parent) == "done"
        assert _status(conn, child) == "ready"
        assert "completion_routed_to_review" not in _kinds(conn, parent)
        meta = kb.latest_run(conn, parent).metadata
        assert meta["mentioned_foreign_prs"] == ["stephenschoettler/hermes-lcm#638"]
        assert "auto_routed_open_prs" not in meta
        assert _comments(conn, parent) == []
    assert q.calls == []


def test_mixed_fleet_and_foreign_routes_to_review_with_fleet_ref_only(kanban_home, monkeypatch):
    q = _use_oracle(monkeypatch, {
        ("ang-ventures/example-home", 670): "OPEN",
        ("stephenschoettler/hermes-lcm", 638): "OPEN",
    })
    with kb.connect() as conn:
        parent, child = _parent_child(conn)
        assert kb.complete_task(conn, parent, summary=f"{PR_URL} upstream {FOREIGN_URL}",
                                metadata={"pr_url": [PR_URL, FOREIGN_URL]}) is True
        assert _status(conn, parent) == "review"
        assert _status(conn, child) == "todo"
        meta = kb.latest_run(conn, parent).metadata
        assert meta["auto_routed_open_prs"] == ["ANG-Ventures/example-home#670"]
        assert meta["mentioned_foreign_prs"] == ["stephenschoettler/hermes-lcm#638"]
        assert _comments(conn, parent) == [
            "survivor PR open; card closes on merged=true (ANG-Ventures/example-home#670)"]
    assert q.calls == [("ANG-Ventures/example-home", 670)]


def test_lint_ignores_foreign_open_pr(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="upstreamed", assignee="w")
        kb.complete_task(conn, tid, result=FOREIGN_URL)  # pytest: routing oracle disabled
        q = _stub({("stephenschoettler/hermes-lcm", 638): "OPEN"})
        assert op.find_done_with_open_pr(conn, query_fn=q) == []
    assert q.calls == []
