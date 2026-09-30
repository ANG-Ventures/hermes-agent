"""active_pr respawn guard: only THIS card's PRs hold it, and the holder is visible.

2026-09-28, t_a8549f8b: a ready card sat ``respawn_guarded:active_pr`` for 9 h
and the event carried no PR, so the operator could not tell which PR held it
and blamed a sibling card's PR that merely mentioned the card id. The guard
now records the holding PR, ``kanban show`` prints it, and a PR whose head
branch names a DIFFERENT card id is a cross-card mention, not this card's work.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def fake_gh(monkeypatch):
    """Route ``gh pr view`` through the REAL ``_query_github_pr_state``."""
    prs: dict[int, dict] = {}
    monkeypatch.setattr(kb, "_PR_HEAD_REF_CACHE", {}, raising=False)
    monkeypatch.setattr(kb, "_PR_MERGE_HEALTH_CACHE", {}, raising=False)

    def run(cmd, *args, **kwargs):
        payload = prs.get(int(cmd[3]))
        if payload is None:
            return types.SimpleNamespace(returncode=1, stdout="")
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload))

    monkeypatch.setattr(kb.subprocess, "run", run)
    return prs


def _open(head: str) -> dict:
    return {"state": "OPEN", "mergedAt": None, "headRefName": head}


def test_other_cards_open_pr_mentioned_on_card_does_not_guard(kanban_home, fake_gh):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="lane work", assignee="alice")
        fake_gh[1333] = _open("daedalus-opus/t_46c8caeb-bake-test-pollution")
        kb.add_comment(
            conn, tid, "daedalus-opus",
            "D9 coordination from t_46c8caeb: my bake change "
            "https://github.com/o/home/pull/1333 touches only bake.py",
        )
        assert kb.check_respawn_guard(conn, tid) is None


def test_cards_own_open_pr_still_guards_and_names_the_pr(kanban_home, fake_gh):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="lane work", assignee="alice")
        fake_gh[1271] = _open(f"daedalus-opus/{tid}-kimi-code-lane")
        kb.add_comment(conn, tid, "worker", "PR https://github.com/o/home/pull/1271 open")
        detail: dict = {}
        assert kb.check_respawn_guard(conn, tid, detail=detail) == "active_pr"
        assert detail == {
            "pr": "https://github.com/o/home/pull/1271", "pr_state": "OPEN",
            "hold": "PR merge state unknown",
        }


@pytest.mark.parametrize(
    "head", ["fix/some-topic", None], ids=["no-card-id-in-branch", "head-unknown"],
)
def test_unattributable_open_pr_stays_guarded(kanban_home, fake_gh, head):
    """Fail-safe: without positive evidence of another owner, keep guarding."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="lane work", assignee="alice")
        fake_gh[7] = {"state": "OPEN", "mergedAt": None, **({"headRefName": head} if head else {})}
        kb.add_comment(conn, tid, "worker", "https://github.com/o/r/pull/7")
        assert kb.check_respawn_guard(conn, tid) == "active_pr"


def test_other_cards_pr_skipped_but_own_pr_in_same_card_still_guards(kanban_home, fake_gh):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="lane work", assignee="alice")
        fake_gh[1] = _open("bob/t_00000001-other")
        fake_gh[2] = _open(f"alice/{tid}-mine")
        kb.add_comment(
            conn, tid, "worker",
            "see https://github.com/o/r/pull/1 and mine https://github.com/o/r/pull/2",
        )
        detail: dict = {}
        assert kb.check_respawn_guard(conn, tid, detail=detail) == "active_pr"
        assert detail["pr"] == "https://github.com/o/r/pull/2"


def test_dispatch_event_and_show_name_the_holding_pr(
    kanban_home, fake_gh, all_assignees_spawnable,
):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="lane work", assignee="alice")
        fake_gh[1271] = _open(f"daedalus-opus/{tid}-lane")
        kb.add_comment(conn, tid, "worker", "https://github.com/o/home/pull/1271")
        res = kb.dispatch_once(conn)
        assert res.respawn_guarded == [(tid, "active_pr")]
        payloads = [e.payload for e in kb.list_events(conn, tid) if e.kind == "respawn_guarded"]
        assert payloads[-1] == {
            "reason": "active_pr", "pr": "https://github.com/o/home/pull/1271", "pr_state": "OPEN",
            "hold": "PR merge state unknown",
        }
    out = kc.run_slash(f"show {tid}")
    guard = [ln for ln in out.splitlines() if ln.strip().startswith("guard:")]
    assert guard and "active_pr — https://github.com/o/home/pull/1271 (OPEN)" in guard[0]


@pytest.mark.parametrize("kind", ["spawned", "requeued", "status", "unblocked"])
def test_show_drops_guard_line_after_spawn_or_requeue(kanban_home, kind):
    """A requeue authorizes the next spawn; show must not keep blaming the PR."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="alice")
        kb._append_event(conn, tid, "respawn_guarded", {"reason": "active_pr", "pr": "u"})
        assert "guard:" in kc.run_slash(f"show {tid}")
        kb._append_event(conn, tid, kind, {})
    assert "guard:" not in kc.run_slash(f"show {tid}")


def test_stuck_page_item_carries_pr(kanban_home):
    import time as _t

    now = int(_t.time())
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="alice")
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (tid, "respawn_guarded",
             json.dumps({"reason": "active_pr", "pr": "https://github.com/o/r/pull/9"}),
             now - 3600),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (tid, "respawn_guarded",
             json.dumps({"reason": "active_pr", "pr": "https://github.com/o/r/pull/9"}), now),
        )
        conn.commit()
        stuck = [x for x in kb.respawn_guard_stuck_tasks(conn, now=now) if x["task_id"] == tid]
    assert stuck and stuck[0]["pr"] == "https://github.com/o/r/pull/9"


@pytest.mark.parametrize(
    "head",
    ["alice/fix-t_11111111", "alice/topic_t_11111111", "t_22222222-x/fix-t_11111111"],
)
def test_card_id_outside_owner_position_stays_guarded(kanban_home, fake_gh, head):
    """t_84471ec4: an id in the branch TOPIC is a mention, not an owner."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="lane work", assignee="alice")
        fake_gh[5] = _open(head)
        kb.add_comment(conn, tid, "worker", "https://github.com/o/r/pull/5")
        assert kb.check_respawn_guard(conn, tid) == "active_pr"


@pytest.mark.parametrize("head", ["wt/t_11111111", "proj/t_11111111", "bob/t_11111111-x"])
def test_other_card_id_in_owner_position_skips(kanban_home, fake_gh, head):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="lane work", assignee="alice")
        fake_gh[6] = _open(head)
        kb.add_comment(conn, tid, "worker", "https://github.com/o/r/pull/6")
        assert kb.check_respawn_guard(conn, tid) is None


def test_show_drops_ready_hold_after_review_requested(kanban_home):
    """t_84471ec4: ready -> review handoff must not keep showing the old active_pr."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="alice")
        kb._append_event(conn, tid, "respawn_guarded", {"reason": "active_pr", "pr": "u"})
        kb._append_event(conn, tid, "review_requested", {})
    assert "guard:" not in kc.run_slash(f"show {tid}")


def test_show_hides_active_pr_hold_on_review_card(kanban_home):
    """The review lane never records active_pr; a stale one must not display."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="alice")
        kb._append_event(conn, tid, "respawn_guarded", {"reason": "active_pr", "pr": "u"})
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        conn.commit()
    assert "guard:" not in kc.run_slash(f"show {tid}")


# 2026-09-30, t_64d223f2: t_57274bc7 sat ready 12 h, held every tick by its OWN
# DIRTY PR #1871 with no live worker, while only that worker could rebase it.
# A PR only the worker can move holds the card only while the worker lives.


def _with_health(head: str, merge_state: str, rollup=None) -> dict:
    return {**_open(head), "mergeStateStatus": merge_state,
            "statusCheckRollup": rollup or []}


_RED_ROLLUP = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}]


@pytest.mark.parametrize(
    ("merge_state", "rollup", "needs"),
    [("DIRTY", None, "DIRTY"), ("BEHIND", None, "BEHIND"),
     ("UNSTABLE", None, "UNSTABLE"), ("BLOCKED", _RED_ROLLUP, "CI red")],
)
def test_workerless_card_spawns_to_fix_its_own_unmergeable_pr(
    kanban_home, fake_gh, merge_state, rollup, needs,
):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="D-perf1", assignee="alice")
        fake_gh[1871] = _with_health(f"daedalus-opus/{tid}-perf", merge_state, rollup)
        kb.add_comment(conn, tid, "worker", "https://github.com/o/r/pull/1871")
        assert kb._prior_worker_still_alive(conn, tid) is None
        assert kb.check_respawn_guard(conn, tid) is None
        assert kb._pr_needs_its_worker("o/r", 1871) == needs


def test_unmergeable_pr_holds_while_its_worker_is_alive(kanban_home, fake_gh, monkeypatch):
    monkeypatch.setattr(
        kb, "_prior_worker_still_alive", lambda conn, tid: {"prev_pid": 4242},
    )
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="D-perf1", assignee="alice")
        fake_gh[1871] = _with_health(f"daedalus-opus/{tid}-perf", "DIRTY")
        kb.add_comment(conn, tid, "worker", "https://github.com/o/r/pull/1871")
        detail: dict = {}
        assert kb.check_respawn_guard(conn, tid, detail=detail) == "active_pr"
    assert detail == {
        "pr": "https://github.com/o/r/pull/1871", "pr_state": "OPEN",
        "merge_state": "DIRTY", "pr_needs": "DIRTY", "hold": "worker alive",
    }


@pytest.mark.parametrize("merge_state", ["CLEAN", "HAS_HOOKS", "BLOCKED"])
def test_mergeable_pr_still_holds_and_says_the_closer_lands_it(
    kanban_home, fake_gh, merge_state,
):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="lane work", assignee="alice")
        fake_gh[9] = _with_health(f"alice/{tid}-x", merge_state)
        kb.add_comment(conn, tid, "worker", "https://github.com/o/r/pull/9")
        detail: dict = {}
        assert kb.check_respawn_guard(conn, tid, detail=detail) == "active_pr"
    assert detail["hold"] == "PR mergeable, closer will land it"
    assert detail["merge_state"] == merge_state


def test_dispatch_spawns_workerless_card_behind_planted_dirty_pr(
    kanban_home, fake_gh, all_assignees_spawnable,
):
    """Tick-level: a planted DIRTY own-PR no longer defers the card."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="D-perf1", assignee="alice")
        fake_gh[1871] = _with_health(f"daedalus-opus/{tid}-perf", "DIRTY")
        kb.add_comment(conn, tid, "worker", "https://github.com/o/r/pull/1871")
        res = kb.dispatch_once(conn, dry_run=True)
        assert (tid, "active_pr") not in res.respawn_guarded
        assert tid in [t for (t, _a, _w) in res.spawned]


def test_deferred_tick_line_names_pr_and_hold_reason():
    line = kc._fmt_respawn_guard_detail({
        "pr": "https://github.com/o/r/pull/9", "pr_state": "OPEN",
        "merge_state": "CLEAN", "hold": "PR mergeable, closer will land it",
    })
    assert line == (
        " — https://github.com/o/r/pull/9 (OPEN/CLEAN), PR mergeable, closer will land it"
    )


@pytest.mark.parametrize("state", ["MERGED", "CLOSED", "OPEN"])
def test_state_survives_when_check_rollup_is_not_readable(monkeypatch, state):
    """Prism P1 (#1538): a PR-only token cannot read checks; the enriched query
    fails, the state-only retry must still resolve the authoritative state and
    leave merge health unknown (a stale DIRTY reading must not release)."""
    calls: list[str] = []

    def run(cmd, *args, **kwargs):
        fields = cmd[cmd.index("--json") + 1]
        calls.append(fields)
        if "statusCheckRollup" in fields:
            return types.SimpleNamespace(returncode=1, stdout="")
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({
            "state": "CLOSED" if state == "MERGED" else state,
            "mergedAt": "2026-09-30T00:00:00Z" if state == "MERGED" else None,
            "headRefName": "alice/t_00000001-x",
        }))

    monkeypatch.setattr(kb, "_PR_MERGE_HEALTH_CACHE",
                        {("o/r", 5): {"merge_state": "DIRTY", "ci_red": False}})
    monkeypatch.setattr(kb.subprocess, "run", run)
    assert kb._query_github_pr_state("o/r", 5) == state
    assert len(calls) == 2 and "statusCheckRollup" not in calls[1]
    assert kb._pr_needs_its_worker("o/r", 5) is None
