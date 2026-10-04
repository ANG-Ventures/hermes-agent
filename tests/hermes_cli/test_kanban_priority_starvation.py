"""Dispatcher admission order + priority-starvation guard (t_24d20798).

2026-10-02: t_2f8b03ac (p62) sat READY 10:05 -> 15:40 while lower-priority
cards were admitted. The ready loop already orders ``priority DESC,
created_at ASC``; the card was HELD by ``respawn_guarded: active_pr`` (247
events) and nothing surfaced it. These tests pin the order under the load
gate's per-tick allowance and prove the guard names a starved card once.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_budget as kbud
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd  # the dispatcher's guard seam

THREE_HOURS = 3 * 3600


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(kb, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


@pytest.fixture
def pages(monkeypatch):
    sent: list[str] = []

    def _notify(argv):
        sent.append(argv[-1])
        return True

    monkeypatch.setattr(kbud, "_notify_script_path", lambda home=None: "/x/notify.py")
    monkeypatch.setattr(kbud, "_run_notify", _notify)
    return sent


def _tick(conn, limit=4):
    order: list[str] = []

    def _spawn(task, workspace, **_):
        order.append(task.id)
        return None

    res = kb.dispatch_once(conn, spawn_fn=_spawn, max_spawn=64, spawn_limit=limit)
    return order, res


def _backdate(conn, task_id, seconds):
    conn.execute("UPDATE tasks SET created_at = created_at - ? WHERE id = ?", (seconds, task_id))
    conn.execute(
        "UPDATE task_status_audit SET changed_at = changed_at - ? WHERE task_id = ?",
        (seconds, task_id),
    )
    conn.commit()


def test_admission_is_priority_desc_then_age_within_tick_allowance(board, pages):
    with kb.connect_closing() as conn:
        ids = {}
        # Created low-to-high and young-to-old so insertion order is wrong.
        for name, prio in (("p58", 58), ("p60", 60), ("p62-new", 62), ("p10", 10), ("p0", 0)):
            ids[name] = kb.create_task(conn, title=name, assignee="daedalus", priority=prio)
        ids["p62-old"] = kb.create_task(conn, title="p62-old", assignee="daedalus-opus", priority=62)
        _backdate(conn, ids["p62-old"], 600)
        order, _ = _tick(conn, limit=4)
    assert order == [ids["p62-old"], ids["p62-new"], ids["p60"], ids["p58"]]


def test_starved_card_is_paged_once_naming_card_and_hold(board, pages, monkeypatch):
    with kb.connect_closing() as conn:
        held = kb.create_task(conn, title="held", assignee="daedalus", priority=62)
        _backdate(conn, held, THREE_HOURS)
        low = kb.create_task(conn, title="low", assignee="daedalus-opus", priority=58)

        real_guard = kbd.check_respawn_guard

        def _guard(c, task_id, *a, **k):
            if task_id == held:
                return "active_pr"
            return real_guard(c, task_id, *a, **k)

        monkeypatch.setattr(kbd, "check_respawn_guard", _guard)
        order, res = _tick(conn)
        assert order == [low]
        assert [t for t, _, _ in res.priority_starved] == [held]
        assert len(pages) == 1
        line = pages[0]
        assert held in line and "p62" in line and "active_pr" in line and low in line
        kinds = [e.kind for e in kb.list_events(conn, held)]
        assert kinds.count("priority_starved") == 1

        # Next tick: still starved, same episode -> no second page.
        _tick(conn)
        assert len(pages) == 1


def test_no_page_under_threshold_or_without_lower_priority_admission(board, pages, monkeypatch):
    with kb.connect_closing() as conn:
        young = kb.create_task(conn, title="young", assignee="daedalus", priority=62)
        _backdate(conn, young, 3600)  # 1 h < 2 h threshold
        old_top = kb.create_task(conn, title="old-top", assignee="daedalus", priority=90)
        _backdate(conn, old_top, THREE_HOURS)
        monkeypatch.setattr(kbd, "check_respawn_guard",
                            lambda c, tid, *a, **k: "active_pr" if tid in (young, old_top) else None)
        kb.create_task(conn, title="low", assignee="daedalus", priority=58)
        _tick(conn)
    # young: held, but ready only 1 h (< 2 h). old_top: held 3 h while the
    # p58 card was admitted -> starved.
    assert len(pages) == 1 and old_top in pages[0] and young not in pages[0]


def test_no_page_when_nothing_lower_was_admitted(board, pages, monkeypatch):
    with kb.connect_closing() as conn:
        held = kb.create_task(conn, title="held", assignee="daedalus", priority=62)
        _backdate(conn, held, THREE_HOURS)
        higher = kb.create_task(conn, title="higher", assignee="daedalus", priority=80)
        monkeypatch.setattr(kbd, "check_respawn_guard",
                            lambda c, tid, *a, **k: "active_pr" if tid == held else None)
        order, res = _tick(conn)
        assert order == [higher]
        assert res.priority_starved == [] and pages == []


def test_failed_page_is_retried_next_tick(board, monkeypatch):
    results = iter([False, True])
    sent: list[str] = []

    def _notify(argv):
        sent.append(argv[-1])
        return next(results)

    monkeypatch.setattr(kbud, "_notify_script_path", lambda home=None: "/x/notify.py")
    monkeypatch.setattr(kbud, "_run_notify", _notify)
    with kb.connect_closing() as conn:
        held = kb.create_task(conn, title="held", assignee="daedalus", priority=62)
        _backdate(conn, held, THREE_HOURS)
        kb.create_task(conn, title="low", assignee="daedalus", priority=1)
        monkeypatch.setattr(kbd, "check_respawn_guard",
                            lambda c, tid, *a, **k: "active_pr" if tid == held else None)
        _tick(conn)
        _tick(conn)
        _tick(conn)
        kinds = [e.kind for e in kb.list_events(conn, held)]
    assert len(sent) == 2 and kinds.count("priority_starved") == 1
