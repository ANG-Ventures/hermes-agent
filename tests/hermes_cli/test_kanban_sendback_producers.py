"""Forge L0 SB-P (t_cb6e1433): the kernel half of the sendback producers.

* the two kernel keys SB-K's round cap left for this card (Apollo comment, 2026-10-22):
  d3eaed71c036 -- a ready/todo sendback restores the implementer ONLY when the assignee is the last reviewer;
  766baef3760b -- an operator reassignment of a card in review is the reviewer of record;
* the producer verb `hermes kanban sendback <id> --payload <json>` (hermes-home scripts/lib/sendback.py's sink);
* `completion_blocked_stale_base` is advisory under landing-policy `sendback: on` (spec §2.4 "Also changed").
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

PR = "https://github.com/example/repo/pull/44"


def _payload(**over):
    p = {
        "v": 1,
        "key": "example/repo#44@" + "a" * 40 + ":ci-red:lints-fast",
        "repo": "example/repo", "pr": 44, "head": "a" * 40,
        "source": "ci-red", "reason": "ci-red:lints-fast", "retry": "after_change",
        "summary": "lints-fast red on your diff: tests/test_x.py::test_y",
        "evidence": {"failing_ids": ["tests/test_x.py::test_y"]},
        "round": 1,
    }
    p.update(over)
    return p


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(kb, "_query_github_pr_state", lambda repo, number: "OPEN")
    clock = {"now": int(time.time())}
    monkeypatch.setattr(kb.time, "time", lambda: clock["now"])
    kb.init_db()
    return SimpleNamespace(path=home, clock=clock)


def _tick(home, s=5):
    home.clock["now"] += s


def _stopped_in_review(conn, home, title):
    tid = kb.create_task(conn, title=title, assignee="dev")
    claimed = kb.claim_task(conn, tid)
    kb.add_comment(conn, tid, author="dev", body=f"Opened {PR} for review.")
    _tick(home)
    assert kb.request_review(
        conn, tid, summary="PR ready", reviewer="human",
        expected_run_id=claimed.current_run_id,
    )
    _tick(home)
    return tid


def _review_then_demote(conn, home, title, reviewer="argus"):
    """review (reviewer recorded) -> reopened parent demotes the card to todo, assignee still the reviewer."""
    parent = kb.create_task(conn, title="parent " + title, assignee="planner")
    assert kb.complete_task(conn, parent, result="done")
    tid = kb.create_task(conn, title=title, assignee="dev", parents=[parent])
    claimed = kb.claim_task(conn, tid)
    kb.add_comment(conn, tid, author="dev", body=f"Opened {PR} for review.")
    _tick(home)
    assert kb.request_review(conn, tid, summary="PR ready", reviewer=reviewer,
                             expected_run_id=claimed.current_run_id)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
    assert kb.claim_review_task(conn, tid) is None
    return tid, parent


def test_sendback_keeps_an_operator_reassignment(kanban_home: SimpleNamespace) -> None:
    """d3eaed71c036: a ready/todo card an operator deliberately reassigned (not the
    reviewer it was demoted with) keeps that assignee through a sendback."""
    with kbc.connect() as conn:
        tid, _ = _review_then_demote(conn, kanban_home, "operator moved it")
        assert kb.get_task(conn, tid).assignee == "argus"
        assert kb.assign_task(conn, tid, "dev-b")            # lane outage: the operator moves it
        _tick(kanban_home)
        assert kb.request_sendback(conn, tid, payload=_payload()) == (True, "requeued")
        assert kb.get_task(conn, tid).assignee == "dev-b"


def test_sendback_still_restores_from_the_reviewer(kanban_home: SimpleNamespace) -> None:
    """Control for d3eaed71c036: the demoted-from-review case (assignee IS the last reviewer) restores dev."""
    with kbc.connect() as conn:
        tid, _ = _review_then_demote(conn, kanban_home, "still the reviewer")
        _tick(kanban_home)
        assert kb.request_sendback(conn, tid, payload=_payload()) == (True, "requeued")
        assert kb.get_task(conn, tid).assignee == "dev"


def test_review_provenance_follows_a_reassignment_in_review(kanban_home: SimpleNamespace) -> None:
    """766baef3760b: an operator reassignment of a card IN REVIEW is the reviewer of record; the
    provenance reader and the live assignee never diverge."""
    with kbc.connect() as conn:
        tid = _stopped_in_review(conn, kanban_home, "reviewer swapped")
        assert kb._latest_review_provenance(conn, tid) == "human"
        assert kb.assign_task(conn, tid, "argus", request_changes_reason=None,
                              pr_query=lambda repo, n: "MERGED")
        assert kb.get_task(conn, tid).assignee == "argus"
        assert kb._latest_review_provenance(conn, tid) == "argus"
        # a reassignment OUTSIDE review is not provenance
        other = kb.create_task(conn, title="plain", assignee="dev")
        assert kb.assign_task(conn, other, "dev-b")
        assert kb._latest_review_provenance(conn, other) is None


def test_cli_sendback_verb(kanban_home: SimpleNamespace, capsys) -> None:
    """`hermes kanban sendback <id> --payload <json>` is request_sendback (the producer's sink)."""
    import argparse
    import json
    from hermes_cli import kanban as kc

    def run(*argv):
        sub = argparse.ArgumentParser().add_subparsers(dest="_top")
        return kc.kanban_command(kc.build_parser(sub).parse_args(list(argv)))

    with kbc.connect() as conn:
        tid = _stopped_in_review(conn, kanban_home, "cli")
    assert run("sendback", tid, "--payload", json.dumps(_payload())) == 0
    assert "Sent back %s: requeued" % tid in capsys.readouterr().out
    with kbc.connect() as conn:
        t = kb.get_task(conn, tid)
        assert (t.status, t.assignee) == ("ready", "dev")
        assert [e.kind for e in kb.list_events(conn, tid)][-1] == "sendback"
    assert run("sendback", tid, "--payload", "[1]") == 2
    assert run("sendback", tid, "--payload", json.dumps(_payload(source="nope"))) == 1
    assert "sendback" in kc._HOME_GUARDED_ACTIONS and "sendback" in kc._DELEGATED_CHILD_DENIED_ACTIONS


@pytest.mark.parametrize("knob", [None, "off", "on"])
def test_landing_policy_knob(tmp_path, monkeypatch, knob) -> None:
    pol = tmp_path / "landing-policy.json"
    if knob is not None:
        pol.write_text('{"sendback": "%s"}' % knob)
    monkeypatch.setenv("KANBAN_LANDING_POLICY", str(pol))
    assert kb.landing_policy_knob("sendback") == knob
    monkeypatch.delenv("KANBAN_LANDING_POLICY")
    assert kb.landing_policy_knob("sendback") is None          # pytest never reads the live policy



# ── stale base -> advisory (spec §2.4 "Also changed") ──────────────────────────────────────────────────
def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                               "HOME": str(cwd)}).stdout


def _stale(conflicts=("a.py",), foreign=(), out_of_scope=()):
    fails = (["does not merge cleanly"] if conflicts else []) + (["foreign"] if foreign else []) \
        + (["out of scope"] if out_of_scope else [])
    return SimpleNamespace(repo="ws/repo", failures=fails, conflicts=list(conflicts), foreign_commits=list(foreign),
                           out_of_scope=list(out_of_scope), render=lambda: "ws/repo: stale")


@pytest.mark.parametrize("case,advisory", [
    ("on conflict pr", True),         # the one advisory case
    ("off conflict pr", False),       # kill switch off: refuses as today
    ("on conflict nopr", False),      # no PR: no conflict producer can send it back (Prism 1172e7254787)
    ("on foreign pr", False),         # foreign commits are not a conflict
    ("on scope pr", False),           # out-of-scope edits are not a conflict
])
def test_stale_base_guard_advisory_only_for_a_pr_conflict(kanban_home, tmp_path, monkeypatch, case, advisory) -> None:
    """Under `sendback: on` the guard records-and-proceeds ONLY for a merge-conflict-only failure on a handoff that
    names its own fleet PR; foreign commits, out-of-scope edits and PR-less handoffs still refuse."""
    from hermes_cli import kanban_branch_base as bb
    from hermes_cli import kanban_open_pr as op

    knob, kind, pr = case.split()
    pol = tmp_path / "landing-policy.json"
    pol.write_text('{"sendback": "%s"}' % knob)
    monkeypatch.setenv("KANBAN_LANDING_POLICY", str(pol))
    monkeypatch.setattr(kb, "configured_receipt_gate", lambda: False)   # not the subject (as test_kanban_receipt_gate)
    report = {"conflict": _stale(), "foreign": _stale(conflicts=(), foreign=("abc x",)),
              "scope": _stale(conflicts=(), out_of_scope=("z.py",))}[kind]

    def stale(*a, **k):
        raise bb.StaleBaseError("t_x", [report])
    monkeypatch.setattr(bb, "enforce_handoff", stale)
    monkeypatch.setattr(kb, "_owns_fleet_pr", lambda metadata, survivor_pr, reports=(): pr == "pr")
    # keep the open-PR route out of it: this test is the guard alone
    monkeypatch.setattr(op, "open_pr_refs", lambda *a, **k: [], raising=False)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="slice", assignee="dev")
        run = kb.claim_task(conn, tid).current_run_id
        if advisory:
            assert kb.complete_task(conn, tid, summary="done", expected_run_id=run)
            assert kb.get_task(conn, tid).status == "done"
        else:
            with pytest.raises(bb.StaleBaseError):
                kb.complete_task(conn, tid, summary="done", expected_run_id=run)
            assert kb.get_task(conn, tid).status == "running"
        ev = [e for e in kb.list_events(conn, tid) if e.kind == "completion_blocked_stale_base"]
        assert len(ev) == 1 and bool(ev[0].payload.get("advisory")) is advisory


def test_sendback_restores_over_a_legacy_reviewer_swap(kanban_home: SimpleNamespace) -> None:
    """Prism 1953ce5049b5: a reviewer swap made while the card sat in review (an `assigned` row of any version, no
    status field) is a reviewer of the cycle, so after the card is demoted its sendback restores the implementer."""
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="planner")
        assert kb.complete_task(conn, parent, result="done")
        tid = kb.create_task(conn, title="legacy swap", assignee="dev", parents=[parent])
        claimed = kb.claim_task(conn, tid)
        kb.add_comment(conn, tid, author="dev", body=f"Opened {PR} for review.")
        _tick(kanban_home)
        assert kb.request_review(conn, tid, summary="PR ready", reviewer="reviewer-a",
                                 expected_run_id=claimed.current_run_id)
        with kb.write_txn(conn):   # the legacy row: a reviewer swap while in review, payload without status
            conn.execute("UPDATE tasks SET assignee = 'reviewer-b' WHERE id = ?", (tid,))
            kb._append_event(conn, tid, "assigned", {"assignee": "reviewer-b", "from": "reviewer-a"})
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
        assert kb.claim_review_task(conn, tid) is None
        assert (kb.get_task(conn, tid).status, kb.get_task(conn, tid).assignee) == ("todo", "reviewer-b")
        assert kb._latest_review_provenance(conn, tid) == "reviewer-b"
        _tick(kanban_home)
        assert kb.request_sendback(conn, tid, payload=_payload()) == (True, "requeued")
        assert kb.get_task(conn, tid).assignee == "dev"


def _checkout(tmp_path, name, url, branch="wt/feature"):
    d = tmp_path / name
    d.mkdir()
    _git(d, "init", "-q")
    _git(d, "checkout", "-q", "-b", branch)
    if url:
        _git(d, "remote", "add", "origin", url)
    return d


def _prs(**states):
    """query_fn stub: ``repo-a_7="OPEN"`` -> ANG-Ventures/repo-a#7 is OPEN from branch wt/feature of the same repo;
    ``"OPEN@branch"`` names another head branch, ``"OPEN@branch@owner/repo"`` another head repository."""
    table = {}
    for k, v in states.items():
        repo, num = k.rsplit("_", 1)
        slug = f"ang-ventures/{repo.replace('_', '-')}"
        state, _, rest = v.partition("@")
        head, _, head_repo = rest.partition("@")
        table[(slug, int(num))] = {"state": state, "head_ref": head or "wt/feature", "head_repo": head_repo or slug}
    return lambda repo, number: table.get((repo.lower(), number), {"state": "NOT_FOUND"})


def _rep(path):
    return SimpleNamespace(repo=str(path), failures=["does not merge cleanly"], conflicts=["x"])


@pytest.mark.parametrize("url,slug", [
    ("https://github.com/ANG-Ventures/repo-a.git", "ang-ventures/repo-a"),
    ("https://github.com/ANG-Ventures/repo-a", "ang-ventures/repo-a"),
    ("https://github.com/ANG-Ventures/repo-a/", "ang-ventures/repo-a"),
    ("https://x-access-token:t@github.com/ANG-Ventures/repo-a.git", "ang-ventures/repo-a"),
    ("git@github.com:ANG-Ventures/repo-a.git", "ang-ventures/repo-a"),
    ("git@github.com:ANG-Ventures/repo-a", "ang-ventures/repo-a"),
    ("ssh://git@github.com/ANG-Ventures/repo-a.git", "ang-ventures/repo-a"),
    ("ssh://git@ssh.github.com:443/ANG-Ventures/repo-a.git", "ang-ventures/repo-a"),
    ("https://github.com/Kyzcreig/repo-a.git", "kyzcreig/repo-a"),            # fork remote
    ("https://git.example.org/github.com/ANG-Ventures/repo-a.git", None),    # e0b806db9787: github.com in the path
    ("git@git.example.org:github.com/ANG-Ventures/repo-a.git", None),
    ("https://github.com.evil.org/ANG-Ventures/repo-a.git", None),
    ("https://evil.org/x?github.com/ANG-Ventures/repo-a", None),
    ("https://github.com/ANG-Ventures/repo-a/pull/7", None),
    ("/srv/mirrors/github.com/ANG-Ventures/repo-a.git", None),               # local path
    ("file:///srv/github.com/ANG-Ventures/repo-a.git", None),
    ("", None),
])
def test_github_remote_slug_shapes(url, slug) -> None:
    assert kb._github_remote_slug(url) == slug


def test_checkout_identity_fork_and_missing_remote(tmp_path) -> None:
    fork = _checkout(tmp_path, "f", "https://github.com/ANG-Ventures/repo-a.git")
    _git(fork, "remote", "add", "fork", "git@github.com:Kyzcreig/repo-a.git")
    assert kb._checkout_pr_identity(fork) == ({"ang-ventures/repo-a", "kyzcreig/repo-a"}, "wt/feature")
    bare = _checkout(tmp_path, "n", None)
    assert kb._checkout_pr_identity(bare) == (set(), "wt/feature")
    md = {"pr_url": "https://github.com/ANG-Ventures/repo-a/pull/7"}
    assert kb._owns_fleet_pr(md, None, [_rep(bare)], query_fn=_prs(repo_a_7="OPEN")) is False


def test_conflict_advisory_needs_a_pr_on_every_failing_repo(tmp_path) -> None:
    """Prism bda9414a767d: an own PR on repo A does not make a conflict in repo B (no PR) advisory."""
    a = _checkout(tmp_path, "a", "https://github.com/ANG-Ventures/repo-a.git")
    b = _checkout(tmp_path, "b", "git@github.com:ANG-Ventures/repo-b.git")
    q = _prs(repo_a_7="OPEN", repo_b_9="OPEN")
    md = {"pr_url": "https://github.com/ANG-Ventures/repo-a/pull/7"}
    assert kb._owns_fleet_pr(md, None, [_rep(a)], query_fn=q) is True
    assert kb._owns_fleet_pr(md, None, [_rep(a), _rep(b)], query_fn=q) is False
    md2 = {"pr_url": ["https://github.com/ANG-Ventures/repo-a/pull/7", "https://github.com/ANG-Ventures/repo-b/pull/9"]}
    assert kb._owns_fleet_pr(md2, None, [_rep(a), _rep(b)], query_fn=q) is True
    assert kb._owns_fleet_pr(md, None, [], query_fn=q) is False


def test_conflict_advisory_ignores_a_github_path_on_another_host(tmp_path) -> None:
    """Prism e0b806db9787: a remote on git.example.org whose PATH holds github.com/o/r is not that repo."""
    a = _checkout(tmp_path, "a", "https://git.example.org/github.com/ANG-Ventures/repo-a.git")
    md = {"pr_url": "https://github.com/ANG-Ventures/repo-a/pull/7"}
    assert kb._owns_fleet_pr(md, None, [_rep(a)], query_fn=_prs(repo_a_7="OPEN")) is False


def test_conflict_advisory_scrubs_inherited_git_dir(tmp_path, monkeypatch) -> None:
    """Prism 92574423ea6e: GIT_DIR pointing at checkout B must not lend B's remotes to checkout A."""
    a = _checkout(tmp_path, "a", "https://github.com/ANG-Ventures/repo-a.git")
    b = _checkout(tmp_path, "b", "https://github.com/ANG-Ventures/repo-b.git")
    monkeypatch.setenv("GIT_DIR", str(b / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(b))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    md = {"pr_url": "https://github.com/ANG-Ventures/repo-b/pull/9"}
    assert kb._checkout_pr_identity(a)[0] == {"ang-ventures/repo-a"}
    assert kb._owns_fleet_pr(md, None, [_rep(a)], query_fn=_prs(repo_b_9="OPEN")) is False


@pytest.mark.parametrize("state,backed", [
    ("OPEN", True),
    ("OPEN@other/branch", False),   # an open PR of the repo for other work does not carry this checkout
    ("OPEN@wt/feature@other/repo-a", False),   # 16462e5a9800: another fork's same-named branch
    ("OPEN@wt/feature@ANG-Ventures/repo-a", True),   # head repo compared case-insensitively
    ("MERGED", False),              # 47c7fe02343a: a merged PR cannot receive the conflict sendback
    ("CLOSED", False),
    ("NOT_FOUND", False),
])
def test_conflict_advisory_needs_a_live_pr_from_the_checkout_branch(tmp_path, state, backed) -> None:
    a = _checkout(tmp_path, "a", "https://github.com/ANG-Ventures/repo-a.git")
    md = {"pr_url": "https://github.com/ANG-Ventures/repo-a/pull/7"}
    assert kb._owns_fleet_pr(md, None, [_rep(a)], query_fn=_prs(repo_a_7=state)) is backed
    assert kb._owns_fleet_pr(md, None, [_rep(a)], query_fn=lambda r, n: None) is False   # unreadable


def test_untied_merged_pr_keeps_the_stale_base_refusal(kanban_home, tmp_path, monkeypatch) -> None:
    """Prism b570e340e7e7: when the declared PRs are merged but not tied to the failing checkout
    (`survivor_merged_untied`), the advisory exception never applies, whatever the PR lookup says."""
    from hermes_cli import kanban_branch_base as bb
    from hermes_cli import kanban_open_pr as op

    pol = tmp_path / "landing-policy.json"
    pol.write_text('{"sendback": "on"}')
    monkeypatch.setenv("KANBAN_LANDING_POLICY", str(pol))
    monkeypatch.setattr(kb, "configured_receipt_gate", lambda: False)

    def stale(*a, **k):
        raise bb.StaleBaseError("t_x", [_stale()])
    monkeypatch.setattr(bb, "enforce_handoff", stale)
    monkeypatch.setattr(kb, "_merged_survivor_prs", lambda md, sp: [("ANG-Ventures/repo#7 @ abc", "h", "m")])
    monkeypatch.setattr(bb, "pr_landed_checkout", lambda rep, prs: False)
    monkeypatch.setattr(kb, "_owns_fleet_pr", lambda *a, **k: True)   # even a lookup that says "backed"
    monkeypatch.setattr(op, "open_pr_refs", lambda *a, **k: [], raising=False)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="slice", assignee="dev")
        run = kb.claim_task(conn, tid).current_run_id
        with pytest.raises(bb.StaleBaseError):
            kb.complete_task(conn, tid, summary="done", expected_run_id=run)
        assert kb.get_task(conn, tid).status == "running"
        ev = [e for e in kb.list_events(conn, tid) if e.kind == "completion_blocked_stale_base"]
        assert len(ev) == 1 and not ev[0].payload.get("advisory")
        assert ev[0].payload.get("survivor_merged_untied") == ["ws/repo"]


def test_sendback_keeps_an_operator_assignment_of_a_former_reviewer(kanban_home: SimpleNamespace) -> None:
    """Prism 4509b37e638c: reviewer argus -> swap to reviewer-b -> demoted -> the operator assigns argus: argus is a
    reviewer of the cycle, but the assignment came after the card left review, so the sendback keeps it."""
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="planner")
        assert kb.complete_task(conn, parent, result="done")
        tid = kb.create_task(conn, title="former reviewer", assignee="dev", parents=[parent])
        claimed = kb.claim_task(conn, tid)
        kb.add_comment(conn, tid, author="dev", body=f"Opened {PR} for review.")
        _tick(kanban_home)
        assert kb.request_review(conn, tid, summary="PR ready", reviewer="argus",
                                 expected_run_id=claimed.current_run_id)
        assert kb.assign_task(conn, tid, "reviewer-b", request_changes_reason=None,
                              pr_query=lambda repo, n: "MERGED")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
        assert kb.claim_review_task(conn, tid) is None
        assert kb.get_task(conn, tid).status == "todo"
        assert kb.assign_task(conn, tid, "argus")              # explicit, after the review exit
        _tick(kanban_home)
        assert kb.request_sendback(conn, tid, payload=_payload()) == (True, "requeued")
        assert kb.get_task(conn, tid).assignee == "argus"


def test_reviewer_swap_after_a_review_resume_counts(kanban_home: SimpleNamespace) -> None:
    """Prism 2a08733c4d2a: block + unblock restores a review card to review without a new review_requested; a
    swap made then is a reviewer of the cycle, so the demoted card's sendback restores the implementer."""
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="planner")
        assert kb.complete_task(conn, parent, result="done")
        tid = kb.create_task(conn, title="resumed review", assignee="dev", parents=[parent])
        claimed = kb.claim_task(conn, tid)
        kb.add_comment(conn, tid, author="dev", body=f"Opened {PR} for review.")
        _tick(kanban_home)
        assert kb.request_review(conn, tid, summary="PR ready", reviewer="human",
                                 expected_run_id=claimed.current_run_id)
        assert kb.block_task(conn, tid, reason="park")
        assert kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "review"
        assert kb.assign_task(conn, tid, "argus", request_changes_reason=None,
                              pr_query=lambda repo, n: "MERGED")
        assert "argus" in kb._review_cycle_reviewers(conn, tid)
        assert kb._latest_review_provenance(conn, tid) == "argus"
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
        assert kb.claim_review_task(conn, tid) is None
        assert (kb.get_task(conn, tid).status, kb.get_task(conn, tid).assignee) == ("todo", "argus")
        _tick(kanban_home)
        assert kb.request_sendback(conn, tid, payload=_payload()) == (True, "requeued")
        assert kb.get_task(conn, tid).assignee == "dev"


def test_parent_reopen_demotion_ends_the_review_cycle(kanban_home: SimpleNamespace) -> None:
    """Prism f696ae12d330: the REAL parent-reopen path (descendant_invalidated + legacy status, no review-exit kind
    before this fix) demotes the review card; the operator then assigns the reviewer itself. That assignment came
    after the card left review, so the sendback keeps it and provenance does not name it."""
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="planner")
        assert kb.complete_task(conn, parent, result="done")
        tid = kb.create_task(conn, title="reopen demotes", assignee="dev", parents=[parent])
        claimed = kb.claim_task(conn, tid)
        kb.add_comment(conn, tid, author="dev", body=f"Opened {PR} for review.")
        _tick(kanban_home)
        assert kb.request_review(conn, tid, summary="PR ready", reviewer="argus",
                                 expected_run_id=claimed.current_run_id)
        assert kb.get_task(conn, tid).status == "review"
        out = kb.invalidate_descendants_for_parent_reopen(conn, parent, author="operator")
        assert [e["id"] for e in out["invalidated"]] == [tid]
        assert kb.get_task(conn, tid).status == "todo"
        _tick(kanban_home)
        assert kb.assign_task(conn, tid, "argus")              # the operator's explicit choice, out of review
        assert kb._review_cycle_scan(conn, tid)[1] is False
        _tick(kanban_home)
        assert kb.request_sendback(conn, tid, payload=_payload()) == (True, "requeued")
        assert kb.get_task(conn, tid).assignee == "argus"
