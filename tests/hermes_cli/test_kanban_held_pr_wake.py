"""A ready card held by active_pr behind its own hold-labelled PR wakes its home session once (t_4cface27).

2026-11-20/21: house-voice t_6721e909 sat ready 11.7 h behind pipecat-house-voice#1457 (label ``hold``,
mergeStateStatus BLOCKED). The guard said "PR mergeable, closer will land it", no closer ever would, and nothing
reached the home session. Ordinary queueing (a saturated host) must stay silent.
"""

from __future__ import annotations

import json
import time
import types
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_held_pr_wake as khpw

PR = "https://github.com/o/r/pull/1457"


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


def _pr(tid: str, labels=()) -> dict:
    return {"state": "OPEN", "mergedAt": None, "headRefName": f"daedalus-opus/{tid}-x",
            "mergeStateStatus": "BLOCKED", "statusCheckRollup": [],
            "labels": [{"name": n} for n in labels]}


def _guarded_at(conn, tid: str, at: int, hold: str, pr: str = PR) -> None:
    conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                 (tid, "respawn_guarded", json.dumps({"reason": "active_pr", "pr": pr, "hold": hold}), at))
    conn.commit()


def _wakes(conn, tid: str) -> list:
    return [e.payload for e in kb.list_events(conn, tid)
            if e.kind == "mechanical_wake" and (e.payload or {}).get("source") == khpw.WAKE_SOURCE]


def test_hold_label_says_operator_decides_not_closer_lands(kanban_home, fake_gh):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="lane work", assignee="alice")
        fake_gh[1457] = _pr(tid, labels=("hold",))
        kb.add_comment(conn, tid, "worker", PR)
        detail: dict = {}
        assert kbd.check_respawn_guard(conn, tid, detail=detail) == "active_pr"
    assert detail["hold"] == kb.RESPAWN_GUARD_HOLD_LABELLED
    assert detail["merge_state"] == "BLOCKED"


def test_t6721e909_shape_wakes_once_through_dispatch(kanban_home, fake_gh, all_assignees_spawnable):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="lane work", assignee="alice")
        fake_gh[1457] = _pr(tid, labels=("hold",))
        kb.add_comment(conn, tid, "worker", PR)
        _guarded_at(conn, tid, int(time.time()) - 31 * 60, kb.RESPAWN_GUARD_HOLD_LABELLED)
        res = kbd.dispatch_once(conn)
        assert res.respawn_guarded == [(tid, "active_pr")]
        wakes = _wakes(conn, tid)
        assert len(wakes) == 1
        assert wakes[0]["pr"] == PR and wakes[0]["guard"] == "active_pr"
        assert PR in wakes[0]["text"] and "hold" in wakes[0]["text"]
        kbd.dispatch_once(conn)
        kbd.dispatch_once(conn)
        assert len(_wakes(conn, tid)) == 1


def test_held_under_30_minutes_does_not_wake(kanban_home, fake_gh, all_assignees_spawnable):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="lane work", assignee="alice")
        fake_gh[1457] = _pr(tid, labels=("hold",))
        kb.add_comment(conn, tid, "worker", PR)
        _guarded_at(conn, tid, int(time.time()) - 10 * 60, kb.RESPAWN_GUARD_HOLD_LABELLED)
        kbd.dispatch_once(conn)
        assert _wakes(conn, tid) == []


def test_mergeable_unlabelled_pr_does_not_wake(kanban_home, fake_gh, all_assignees_spawnable):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="lane work", assignee="alice")
        fake_gh[1457] = _pr(tid)
        kb.add_comment(conn, tid, "worker", PR)
        _guarded_at(conn, tid, int(time.time()) - 2 * 3600, kb.RESPAWN_GUARD_HOLD_MERGEABLE)
        kbd.dispatch_once(conn)
        assert _wakes(conn, tid) == []


def test_card_queued_behind_saturated_host_does_not_wake(kanban_home, fake_gh, all_assignees_spawnable):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="queued work", assignee="alice")
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (int(time.time()) - 3 * 3600, tid))
        conn.commit()
        res = kbd.dispatch_once(conn, max_spawn=0)
        assert tid not in [t for (t, _a, _w) in res.spawned]
        assert kb.get_task(conn, tid).status == "ready"
        assert [e for e in kb.list_events(conn, tid) if e.kind == "mechanical_wake"] == []


def test_streak_restarts_after_requeue(kanban_home):
    now = int(time.time())
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="alice")
        _guarded_at(conn, tid, now - 3600, kb.RESPAWN_GUARD_HOLD_LABELLED)
        conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                     (tid, "requeued", "{}", now - 600))
        _guarded_at(conn, tid, now - 300, kb.RESPAWN_GUARD_HOLD_LABELLED)
        detail = {tid: {"pr": PR, "hold": kb.RESPAWN_GUARD_HOLD_LABELLED}}
        assert khpw.notify(conn, detail, now=now) == []
        assert khpw.notify(conn, detail, now=now - 300 + 31 * 60) == [tid]


@pytest.mark.parametrize("value", [False, "off"])
def test_switch_off_is_silent(kanban_home, value):
    now = int(time.time())
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="alice")
        _guarded_at(conn, tid, now - 3600, kb.RESPAWN_GUARD_HOLD_LABELLED)
        detail = {tid: {"pr": PR, "hold": kb.RESPAWN_GUARD_HOLD_LABELLED}}
        kcfg = {"respawn_guard": {"held_pr_wake": value}}
        assert khpw.notify(conn, detail, kcfg=kcfg, now=now) == []
        assert khpw.notify(conn, detail, kcfg={}, now=now) == [tid]
