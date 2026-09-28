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
        assert detail == {"pr": "https://github.com/o/home/pull/1271", "pr_state": "OPEN"}


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
        }
    out = kc.run_slash(f"show {tid}")
    guard = [ln for ln in out.splitlines() if ln.strip().startswith("guard:")]
    assert guard and "active_pr — https://github.com/o/home/pull/1271 (OPEN)" in guard[0]


def test_show_has_no_guard_line_once_spawned(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="alice")
        kb._append_event(conn, tid, "respawn_guarded", {"reason": "active_pr", "pr": "u"})
        kb._append_event(conn, tid, "spawned", {"pid": 1})
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
