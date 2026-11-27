"""Stale per-card state that held a card in ``ready`` forever on 2026-11-12 (t_0a0c9ddc).

(a) t_1e785f52, 2 h: ``tasks.branch_name`` still named ``wt/t_7780e8e1`` (the archived parent's branch, 4 commits
    not in the tree) while the card's own worktree sat on ``daedalus-opus/t_1e785f52-revert-amazon-door-v2`` ==
    origin. The port judged it ``own_tree_off_branch`` every tick; studio-override cannot clear that. Fix: a tree
    on another branch whose HEAD == origin/<that branch> re-derives ``branch_name`` (``branch_rebound``). A tree
    whose branch holds commits origin lacks keeps today's refusal.
(b) t_90808015, 8 h: a card ported to ci-box (``worktree_ported``, origin_host ci-box) that then carried
    ``requires: studio-state``. Every tick waited ``not_portable:requires:studio-state`` on ci-box. #292
    (t_f914c529) ends that bind; this pins the 11-12 shape so it holds no card past one tick.
"""

from __future__ import annotations

import json
import subprocess

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worktree_port as kwt

from tests.hermes_cli.test_kanban_worker_pool import _host, _plan, kanban_home  # noqa: F401
from tests.hermes_cli.test_kanban_worktree_port import (  # noqa: F401
    _clone_locally, _commit, _git, _origin_with_push, _ref, _row, _worktree_card, anchor, SLUG,
)
from tests.hermes_cli.test_kanban_studio_pins_and_carry import _studio_last_resort_on  # noqa: F401
from tests.hermes_cli.test_kanban_studio_capability_only import _caps
from tests.hermes_cli.test_kanban_studio_default_deny import _cfg

import pytest

REBOUND = "branch_rebound"  # the event name (kwt.BRANCH_REBOUND_EVENT)
PR_BRANCH = "daedalus-opus/t_x-revert-v2"


@pytest.fixture
def stale_card(kanban_home, anchor, tmp_path, monkeypatch):
    """t_1e785f52's shape: branch_name names the archived parent's branch (commits the tree lacks); the card's own
    .worktrees/<id> is on the PR branch, pushed to origin."""
    bare = _origin_with_push(anchor, tmp_path)
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=bare)  # a resolvable remote default, whatever the host's init
    monkeypatch.setattr(kwt, "origin_slug", lambda repo, runner=None: SLUG)
    monkeypatch.setattr(kbd, "_studio_last_resort", lambda fleet_dir: True)
    _commit(anchor, "base")
    _git("push", "-q", "origin", "main", cwd=anchor)
    _clone_locally(monkeypatch, tmp_path, bare, [])
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        parent_branch = "wt/t_parent"
        _git("branch", parent_branch, cwd=anchor)
        wt = anchor / ".worktrees" / tid
        _git("worktree", "add", "-q", "-b", PR_BRANCH, str(wt), "main", cwd=anchor)
        for i in range(4):  # v1 commits only the parent's branch holds
            _git("checkout", "-q", parent_branch, cwd=anchor)
            _commit(anchor, f"v1-{i}")
        _git("checkout", "-q", "main", cwd=anchor)
        _commit(wt, "v2")
        _git("push", "-q", "origin", PR_BRANCH, cwd=wt)
        _git("fetch", "-q", "origin", cwd=anchor)
        conn.execute("UPDATE tasks SET branch_name=?, workspace_path=? WHERE id=?", (parent_branch, str(wt), tid))
        conn.commit()
    return tid, wt, bare, parent_branch


def _events(conn, tid, kind):
    return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                                                   (tid, kind))]


def test_stale_branch_name_on_a_pushed_tree_is_rebound_and_ports(stale_card):
    tid, wt, _bare, parent_branch = stale_card
    head = _ref(wt, "HEAD")
    with kb.connect_closing() as conn:
        out: dict = {}
        route = kbd._route_row(conn, _row(conn, tid), _plan(free=2), worktree_out=out)
        row = _row(conn, tid)
        ev = _events(conn, tid, REBOUND)
        # a second tick neither re-binds nor writes another event
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), worktree_out={}) == ("portable", None)
        assert len(_events(conn, tid, REBOUND)) == 1
    assert route == ("portable", None), route
    assert out[tid][4] is None  # origin already holds the tree's HEAD: nothing to push
    assert row["branch_name"] == PR_BRANCH
    assert ev == [{"from": parent_branch, "to": PR_BRANCH, "head": head, "tree": str(wt)}]
    assert _ref(wt.parent.parent, f"refs/heads/{parent_branch}")  # the old branch is left as it is


def test_off_branch_tree_with_unpushed_commits_still_refuses(stale_card):
    """A legit off-branch tree: its branch holds a commit origin lacks. Today's refusal, branch_name untouched."""
    tid, wt, _bare, parent_branch = stale_card
    _commit(wt, "unpushed")
    with kb.connect_closing() as conn:
        route = kbd._route_row(conn, _row(conn, tid), _plan(free=2))
        assert _row(conn, tid)["branch_name"] == parent_branch
        assert _events(conn, tid, REBOUND) == []
    assert route == ("local", "not_portable:workspace_kind:own_tree_off_branch"), route


def test_a_tree_on_the_default_branch_is_never_rebound_to_it(stale_card):
    tid, wt, _bare, parent_branch = stale_card
    _git("checkout", "-q", "--detach", cwd=wt.parent.parent)
    _git("checkout", "-q", "main", cwd=wt)
    with kb.connect_closing() as conn:
        kbd._route_row(conn, _row(conn, tid), _plan(free=2))
        assert _row(conn, tid)["branch_name"] == parent_branch
        assert _events(conn, tid, REBOUND) == []


def test_a_detached_tree_is_not_rebound(stale_card):
    tid, wt, _bare, parent_branch = stale_card
    _git("checkout", "-q", "--detach", cwd=wt)
    with kb.connect_closing() as conn:
        kbd._route_row(conn, _row(conn, tid), _plan(free=2))
        assert _row(conn, tid)["branch_name"] == parent_branch


# -- (b) t_90808015: ported to ci-box, then `requires: studio-state` ---------------------------------------------

def test_card_ported_to_a_host_its_requires_rules_out_is_released_in_one_tick(kanban_home, monkeypatch):
    _cfg(kanban_home, True, [])
    monkeypatch.setattr(kwt, "probe_clone", lambda host, path, runner=None: (True, "abc123"))
    kwt._probe_memo.clear()
    body = ("host: studio\nrequires: studio-state  # re-routed after capability block: Placed on ci-box\n"
            "fleet-port-lint residue")
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="port lint", assignee="alpha", body=body)
        path = str(kb.kanban_home() / "fleet-scratch" / "workspaces" / "default" / tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_path=?, origin_host='ci-box' WHERE id=?", (path, tid))
            kb._append_event(conn, tid, kwt.PORTED_EVENT, {"host": "ci-box", "repo": "ANG-Ventures/hermes-home",
                                                            "mirror": "~/.hermes/mirrors/x.git",
                                                            "branch": f"wt/{tid}", "workspace": path})
            kb._append_event(conn, tid, "worker_placed", {"host": "ci-box", "workspace": path})
            kb._append_event(conn, tid, "blocked", {"kind": "capability", "reason": "Placed on ci-box"})
        route = kbd._route_row(conn, _row(conn, tid), _plan(free=2, hosts=[_host("ci-box")]), caps=_caps(),
                               rules_out={}, carry_out={}, studio_out={})
        row = _row(conn, tid)
    assert not route[1].startswith("not_portable:requires"), route
    assert route[0] == "wait" and route[1].startswith("studio_default_deny:studio-state"), route
    assert row["workspace_path"] == f"{path}-from-ci-box" and row["origin_host"] is None


# -- Prism round 1 (9c711a6dd8f7, aef8b937133f): proof comes from the remote itself, never a local ref ------------

def _no_rebind(tid, parent_branch):
    with kb.connect_closing() as conn:
        kbd._route_row(conn, _row(conn, tid), _plan(free=2))
        assert _row(conn, tid)["branch_name"] == parent_branch
        assert _events(conn, tid, REBOUND) == []


def test_a_tree_on_a_custom_remote_default_is_never_rebound(stale_card):
    """9c711a6dd8f7: remote default is the tree's branch and no local refs/remotes/origin/HEAD exists."""
    tid, wt, bare, parent_branch = stale_card
    _git("symbolic-ref", "HEAD", f"refs/heads/{PR_BRANCH}", cwd=bare)
    subprocess.run(["git", "-C", str(wt), "symbolic-ref", "-d", "refs/remotes/origin/HEAD"], capture_output=True)
    subprocess.run(["git", "-C", str(wt), "update-ref", "-d", "refs/remotes/origin/HEAD"], capture_output=True)
    assert not _ref(wt, "refs/remotes/origin/HEAD")
    _no_rebind(tid, parent_branch)


def test_an_unresolvable_remote_default_refuses_the_rebind(stale_card):
    tid, _wt, bare, parent_branch = stale_card
    _git("symbolic-ref", "HEAD", "refs/heads/no-such-branch", cwd=bare)
    _no_rebind(tid, parent_branch)


def test_a_stale_tracking_ref_is_not_proof_of_push(stale_card):
    """aef8b937133f: origin/<branch> still equals HEAD locally, but the branch is gone from the remote."""
    tid, wt, bare, parent_branch = stale_card
    _git("branch", "-D", PR_BRANCH, cwd=bare)
    assert _ref(wt, f"refs/remotes/origin/{PR_BRANCH}") == _ref(wt, "HEAD")
    _no_rebind(tid, parent_branch)
