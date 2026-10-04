"""One running card per PR (t_cb70d390).

2026-10-03: ANG-Ventures/hermes-agent#1624 had its review card t_7c46c0d0 and
helpers t_91f21ce8 / t_fba53592 / t_34200a86 all pushing to one branch, each
push restarting CI for the others, until Apollo posted STOP on every card.
Rule: a PR may have one running card. A second spawn is refused (owner named),
a rebase helper is refused at birth, and a stale owner is reclaimed, never
duplicated.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_pr_owner as kpo

PR = "ANG-Ventures/hermes-agent#1624"
URL = "https://github.com/ANG-Ventures/hermes-agent/pull/1624"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def head_at(monkeypatch):
    """PR head commit time served to the guard; None = unknown (no network)."""
    box = {"at": None}
    monkeypatch.setattr(kpo, "pr_head_committed_at", lambda repo, n: box["at"])
    return box


def _dead_pid() -> int:
    pid = 999_999
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except PermissionError:
            pass
        pid -= 1


def _run_owner(conn, *, title=f"parity sync {PR}", age=0, metadata=None) -> str:
    """A card RUNNING on the PR, its run started ``age`` seconds ago, claimer dead."""
    tid = kb.create_task(conn, title=title, assignee="daedalus-opus")
    assert kb.claim_task(conn, tid, claimer=f"{socket.gethostname()}:{_dead_pid()}")
    started = int(time.time()) - age
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET started_at = ?, metadata = ? WHERE task_id = ?",
                     (started, json.dumps(metadata) if metadata else None, tid))
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (started, tid))
    return tid


def test_second_card_on_a_running_pr_is_refused_with_owner_named(kanban_home, head_at):
    with kb.connect() as conn:
        owner = _run_owner(conn)
        helper = kb.create_task(conn, title=f"fix red slice on {PR}", assignee="daedalus-fable")
        detail: dict = {}
        assert kb.check_respawn_guard(conn, helper, detail=detail) == kpo.GUARD_REASON
        assert detail["owner"] == owner
        assert f"owner {owner}" in detail["hold"]
        assert detail["pr"] == "ang-ventures/hermes-agent#1624"


def test_rebase_helper_is_refused_at_birth_naming_the_owner(kanban_home, head_at):
    with kb.connect() as conn:
        owner = _run_owner(conn)
        with pytest.raises(kpo.PrOwnerBusyError) as exc:
            kb.create_task(
                conn, title=f"rebase {PR} @9339c44b: PR in review is DIRTY (card {owner})",
                assignee="daedalus-opus", idempotency_key=f"rebase:{PR}@9339c44b",
            )
        assert f"owner {owner}" in str(exc.value)
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_rebase_helper_is_born_when_no_card_runs_on_the_pr(kanban_home, head_at):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title=f"rebase {PR} @9339c44b", assignee="daedalus-opus",
                             idempotency_key=f"rebase:{PR}@9339c44b")
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.check_respawn_guard(conn, tid) is None


def test_metadata_pr_url_makes_the_owner(kanban_home, head_at):
    """The real owner t_7c46c0d0 named #1624 only in its run handoff metadata."""
    with kb.connect() as conn:
        owner = _run_owner(conn, title="W7-1: upstream parity sync", metadata={"pr_url": URL})
        helper = kb.create_task(conn, title=f"rebase {PR} @24c05fd5", assignee="daedalus-fable")
        detail: dict = {}
        assert kb.check_respawn_guard(conn, helper, detail=detail) == kpo.GUARD_REASON
        assert detail["owner"] == owner


def test_body_mention_and_foreign_repo_do_not_make_a_target(kanban_home, head_at):
    with kb.connect() as conn:
        _run_owner(conn)
        mention = kb.create_task(conn, title="unrelated lane", assignee="alice",
                                 body=f"context: see {URL}")
        assert kb.check_respawn_guard(conn, mention) is None
        _run_owner(conn, title="upstream NousResearch/hermes-agent#1624 port")
        other = kb.create_task(conn, title="NousResearch/hermes-agent#1624 follow-up",
                               assignee="alice")
        assert kb.check_respawn_guard(conn, other) is None


def test_operator_review_claim_is_not_an_owner(kanban_home, head_at):
    with kb.connect() as conn:
        owner = _run_owner(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET profile = 'human:apollo' WHERE task_id = ?", (owner,))
        helper = kb.create_task(conn, title=f"fix {PR}", assignee="alice")
        assert kb.check_respawn_guard(conn, helper) is None


@pytest.mark.parametrize("head_age, run_age", [(None, 3 * 3600), (600, 3 * 3600), (3 * 3600, 600)],
                         ids=["head-unknown", "fresh-push", "fresh-run"])
def test_owner_is_fresh_unless_run_and_push_both_older_than_2h(kanban_home, head_at, head_age, run_age):
    with kb.connect() as conn:
        _run_owner(conn, age=run_age)
        head_at["at"] = None if head_age is None else int(time.time()) - head_age
        helper = kb.create_task(conn, title=f"fix {PR}", assignee="alice")
        assert kb.check_respawn_guard(conn, helper) == kpo.GUARD_REASON


def test_stale_owner_is_reassigned_not_duplicated(kanban_home, head_at, all_assignees_spawnable):
    with kb.connect() as conn:
        owner = _run_owner(conn, age=3 * 3600)
        head_at["at"] = int(time.time()) - 3 * 3600
        helper = kb.create_task(conn, title=f"fix {PR}", assignee="alice")
        spawned = []
        res = kb.dispatch_once(conn, spawn_fn=lambda task, ws, *a, **k: spawned.append(task.id) or 4242)
        assert (helper, kpo.GUARD_REASON) not in res.respawn_guarded
        assert spawned == [helper]
        assert kb.get_task(conn, owner).status != "running"
        kinds = {e.kind: e.payload for e in kb.list_events(conn, owner)}
        assert kinds[kpo.REASSIGN_EVENT]["to"] == helper
        assert "reclaimed" in kinds
        # One running card on the PR, and the old owner cannot come back while it runs.
        assert [o["task_id"] for o in kpo.running_owners(
            conn, {("ang-ventures/hermes-agent", 1624)})] == [helper]
        assert kb.check_respawn_guard(conn, owner) == kpo.GUARD_REASON


def test_unprovable_stale_owner_holds_instead_of_duplicating(kanban_home, head_at, monkeypatch):
    with kb.connect() as conn:
        owner = _run_owner(conn, age=3 * 3600)
        head_at["at"] = int(time.time()) - 3 * 3600
        helper = kb.create_task(conn, title=f"fix {PR}", assignee="alice")
        monkeypatch.setattr(kb, "reclaim_task", lambda *a, **k: False)
        detail: dict = {}
        stale = kpo.spawn_owners(conn, helper)
        assert kb._reassign_stale_pr_owners(conn, helper, stale, detail) == kpo.GUARD_REASON
        assert detail["owner"] == owner and "could not be reclaimed" in detail["hold"]


def test_show_lists_the_prs_other_cards(kanban_home, head_at):
    with kb.connect() as conn:
        owner = _run_owner(conn)
        helper = kb.create_task(conn, title=f"fix {PR}", assignee="alice")
    out = kc.run_slash(f"show {helper}")
    line = [ln for ln in out.splitlines() if ln.strip().startswith("pr-cards:")]
    assert line and "ang-ventures/hermes-agent#1624" in line[0]
    assert f"{owner} (running, daedalus-opus)" in line[0]
    payload = json.loads(kc.run_slash(f"show {helper} --json"))
    assert [c["id"] for c in payload["pr_cards"]["ang-ventures/hermes-agent#1624"]] == [owner]


def test_concurrent_pr_runs_counts_overlapping_pairs(kanban_home, head_at):
    with kb.connect() as conn:
        a = _run_owner(conn, age=100)
        b = _run_owner(conn, title=f"rebase {PR}", age=50)
        pairs = kpo.concurrent_pr_runs(conn, int(time.time()) - 1000)
        assert [(p["first"], p["second"]) for p in pairs] == [(a, b)]
