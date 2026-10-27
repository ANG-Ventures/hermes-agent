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


def _checkout(tmp_path, name, url):
    d = tmp_path / name
    d.mkdir()
    _git(d, "init", "-q")
    _git(d, "remote", "add", "origin", url)
    return d


def test_conflict_advisory_needs_a_pr_on_every_failing_repo(tmp_path) -> None:
    """Prism bda9414a767d: an own PR on repo A does not make a conflict in repo B (no PR) advisory."""
    a = _checkout(tmp_path, "a", "https://github.com/ANG-Ventures/repo-a.git")
    b = _checkout(tmp_path, "b", "git@github.com:ANG-Ventures/repo-b.git")
    rep = lambda path: SimpleNamespace(repo=str(path), failures=["does not merge cleanly"], conflicts=["x"])
    md = {"pr_url": "https://github.com/ANG-Ventures/repo-a/pull/7"}
    assert kb._owns_fleet_pr(md, None, [rep(a)]) is True
    assert kb._owns_fleet_pr(md, None, [rep(a), rep(b)]) is False
    md2 = {"pr_url": ["https://github.com/ANG-Ventures/repo-a/pull/7", "https://github.com/ANG-Ventures/repo-b/pull/9"]}
    assert kb._owns_fleet_pr(md2, None, [rep(a), rep(b)]) is True
    assert kb._owns_fleet_pr(md, None, []) is False


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
