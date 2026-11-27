"""The board tick lock is held only while the tick decides (t_f60f502f, ``kanban.dispatch.narrow_lock``).

Measured 2026-11-08 on the Studio: one tick held ``kanban.db.dispatch.lock`` 60-300 s, almost all of it in
workspace resolution and spawn after the claim, and ``kanban admit`` was refused for minutes. The tick now
hands the lock back between a claim and the next decision (:class:`kanban_db_connect.TickLockWindow`),
records how long it held it (``DispatchResult.lock_held_s``) and where the time went (``tick_phases_s``).
"""
from __future__ import annotations

import math
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _kill_switch(home: Path) -> None:
    (home / "config.yaml").write_text("kanban:\n  dispatch:\n    narrow_lock: false\n", encoding="utf-8")


def _lock_free(db_path: Path) -> bool:
    """Can ANOTHER holder take the board's tick lock right now (a separate open file, as a second process)."""
    with kbc._dispatch_tick_lock(db_path) as held:
        return held


def test_window_hands_the_board_lock_back_and_takes_it_again(kanban_home):
    db_path = kb.kanban_db_path(board="default")
    with kbc._dispatch_tick_lock(db_path) as held:
        assert held
        window = kbc.TickLockWindow(db_path)
        assert not _lock_free(db_path)
        window.release()
        assert _lock_free(db_path)
        window.hold()
        assert not _lock_free(db_path)
    assert _lock_free(db_path)


def test_a_window_left_released_never_clears_the_next_holders_lock(kanban_home):
    """An exception between release and hold exits the tick with the lock not ours: the tick's exit must not
    unlock or un-stamp whoever holds it now."""
    db_path = kb.kanban_db_path(board="default")
    lock_path = db_path.with_name(db_path.name + ".dispatch.lock")
    with kbc._dispatch_tick_lock(db_path):
        kbc.TickLockWindow(db_path).release()
        other = lock_path.open("a+b")
        assert kbc._try_lock_nb(other)
        kbc._stamp_tick_lock(other)
    try:
        assert not _lock_free(db_path), "the tick's exit released the other holder's lock"
        assert kb._read_dispatch_lock_holder(db_path).get("pid"), "the tick's exit cleared the holder stamp"
    finally:
        kbc._unlock(other)
        other.close()


def _tick_with_slow_spawn(conn, n_cards: int, spawn_s: float):
    """One real tick over ``n_cards`` ready cards whose spawn takes ``spawn_s``; also reports whether another
    holder could take the board lock from inside each spawn."""
    free_during_spawn = []
    db_path = kb.kanban_db_path(board="default")

    def spawn(task, workspace):
        free_during_spawn.append(_lock_free(db_path))
        time.sleep(spawn_s)
        return None

    for i in range(n_cards):
        kb.create_task(conn, title=f"card {i}", assignee="alpha")
    return kbd.dispatch_once(conn, spawn_fn=spawn), free_during_spawn


def test_spawn_runs_with_the_board_lock_released(kanban_home, all_assignees_spawnable):
    with kbc.connect() as conn:
        res, free = _tick_with_slow_spawn(conn, n_cards=2, spawn_s=0.0)
    assert len(res.spawned) == 2
    assert free == [True, True], "a second dispatcher / admit was locked out while the tick spawned"
    assert res.lock_held_s is not None
    assert "spawn.unlocked" in res.tick_phases_s and "workspace.unlocked" in res.tick_phases_s
    assert "decide" in res.tick_phases_s


def test_kill_switch_holds_the_lock_through_the_spawn(kanban_home, all_assignees_spawnable):
    _kill_switch(kanban_home)
    with kbc.connect() as conn:
        res, free = _tick_with_slow_spawn(conn, n_cards=2, spawn_s=0.0)
    assert len(res.spawned) == 2
    assert free == [False, False]
    assert not any(k.endswith(".unlocked") and k != "pr_gate_prefetch.unlocked" for k in res.tick_phases_s)


def _p95(values):
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


@pytest.mark.parametrize("narrow", [True, False])
def test_held_time_p95_stays_under_two_seconds_when_spawns_are_slow(kanban_home, all_assignees_spawnable, narrow):
    """Fake board: 5 ticks x 2 cards, each spawn 0.6 s (1.2 s of spawn per tick). Narrow: p95 held time
    under 2 s and under the tick's own spawn time. Kill switch (the control): every tick holds the lock
    for at least its spawns."""
    if not narrow:
        _kill_switch(kanban_home)
    held = []
    with kbc.connect() as conn:
        kbd.dispatch_once(conn, spawn_fn=lambda t, w: None)   # warm-up: the first tick pays one-time imports/sweeps
        for _ in range(5):
            res, _free = _tick_with_slow_spawn(conn, n_cards=2, spawn_s=0.6)
            assert len(res.spawned) == 2
            held.append(res.lock_held_s)
    if narrow:
        assert _p95(held) < 2.0, held
        assert _p95(held) < 1.2, f"lock held through the spawns: {held}"
    else:
        assert min(held) >= 1.2, held


def _status(conn, task_id: str) -> str:
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]


def _claim_in_window(db_path: Path, title: str, assignee: str = "alpha") -> str:
    """What a concurrent `kanban admit` does while the tick has the lock handed back: take the board lock
    (a separate open file) and claim a card."""
    with kbc._dispatch_tick_lock(db_path) as held:
        assert held, "the tick kept the lock through its spawn"
        with kbc.connect() as other:
            tid = kb.create_task(other, title=title, assignee=assignee)
            assert kb.claim_task(other, tid) is not None
    return tid


def test_a_claim_made_while_the_lock_is_handed_back_counts_against_max_in_progress(
        kanban_home, all_assignees_spawnable, monkeypatch):
    """Prism 3c47847c9979: max_in_progress=2, nothing running, A and B ready. The tick claims A and hands the
    lock back for A's spawn; an admit claims C in that window. On re-taking the lock the tick must see 2
    running and leave B ready (the budget computed at tick start said 2 more)."""
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda *a, **k: "ok")
    db_path = kb.kanban_db_path(board="default")
    claimed_in_window = []

    def spawn(task, workspace):
        if not claimed_in_window:
            claimed_in_window.append(_claim_in_window(db_path, "C"))
        return None

    with kbc.connect() as conn:
        a = kb.create_task(conn, title="A", assignee="alpha")
        b = kb.create_task(conn, title="B", assignee="alpha")
        res = kbd.dispatch_once(conn, spawn_fn=spawn, max_in_progress=2)
        assert [s[0] for s in res.spawned] == [a]
        assert _status(conn, b) == "ready"
        assert kbd.count_running_tasks(conn) == 2


def test_a_claim_made_while_the_lock_is_handed_back_counts_against_the_per_profile_cap(
        kanban_home, all_assignees_spawnable, monkeypatch):
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda *a, **k: "ok")
    db_path = kb.kanban_db_path(board="default")
    claimed_in_window = []

    def spawn(task, workspace):
        if not claimed_in_window:
            claimed_in_window.append(_claim_in_window(db_path, "C"))
        return None

    with kbc.connect() as conn:
        a = kb.create_task(conn, title="A", assignee="alpha")
        b = kb.create_task(conn, title="B", assignee="alpha")
        res = kbd.dispatch_once(conn, spawn_fn=spawn, max_in_progress_per_profile=2)
        assert [s[0] for s in res.spawned] == [a]
        assert _status(conn, b) == "ready"
        assert b in [s[0] for s in res.skipped_per_profile_capped]


def test_memory_pressure_rising_while_the_lock_is_handed_back_stops_the_tick(
        kanban_home, all_assignees_spawnable, monkeypatch):
    level = {"now": "ok"}
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda *a, **k: level["now"])

    def spawn(task, workspace):
        level["now"] = "critical"
        return None

    with kbc.connect() as conn:
        a = kb.create_task(conn, title="A", assignee="alpha")
        b = kb.create_task(conn, title="B", assignee="alpha")
        res = kbd.dispatch_once(conn, spawn_fn=spawn)
        assert [s[0] for s in res.spawned] == [a]
        assert _status(conn, b) == "ready"
        assert res.memory_pressure == "critical"


def _successor_in_window(db_path: Path, task_id: str) -> int:
    """While the tick has the lock handed back, another actor ends the tick's run and starts a successor run on
    the SAME card (block + unblock + an admit's claim). Returns the successor's run id."""
    with kbc._dispatch_tick_lock(db_path) as held:
        assert held, "the tick kept the lock through its launch"
        with kbc.connect() as other:
            assert kb.block_task(other, task_id, reason="retry", kind="transient")
            assert kb.unblock_task(other, task_id)
            if _status(other, task_id) != "ready":
                other.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
            succ = kb.claim_task(other, task_id)
            assert succ is not None
            return int(succ.current_run_id)


def _assert_successor_untouched(conn, task_id: str, succ_run: int) -> None:
    row = conn.execute("SELECT status, current_run_id, claim_lock FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert (row[0], row[1]) == ("running", succ_run), "the old launch reset the successor run"
    assert row[2] is not None, "the old launch cleared the successor's claim"
    run = conn.execute("SELECT ended_at FROM task_runs WHERE id=?", (succ_run,)).fetchone()
    assert run[0] is None, "the old launch closed the successor run"


def test_a_failing_spawn_never_ends_a_successor_run_started_while_the_lock_was_handed_back(
        kanban_home, all_assignees_spawnable):
    """Prism d4e9bf031486 / 09fab0fa405f: the spawn raises after a successor run claimed the card in the
    released window. The failure is fenced to the run that launched; the successor keeps its claim and pid."""
    db_path = kb.kanban_db_path(board="default")
    succ = {}

    def spawn(task, workspace):
        succ["run"] = _successor_in_window(db_path, task.id)
        raise RuntimeError("spawn blew up after the window")

    with kbc.connect() as conn:
        a = kb.create_task(conn, title="A", assignee="alpha")
        kbd.dispatch_once(conn, spawn_fn=spawn)
        _assert_successor_untouched(conn, a, succ["run"])


@pytest.mark.parametrize("failure", ["error", "unavailable", "resolves"])
def test_workspace_resolution_never_touches_a_successor_run(
        kanban_home, all_assignees_spawnable, monkeypatch, failure):
    """Same window, workspace phase: a resolve that fails (any error, WorkspaceUnavailable) or succeeds after a
    successor claimed the card never resets the successor, never writes the card's workspace, never spawns."""
    from hermes_cli import kanban_db_workspace as kbw
    from hermes_cli.kanban_workspace_policy import WorkspaceUnavailable
    db_path = kb.kanban_db_path(board="default")
    succ = {}
    spawned = []
    real_resolve = kbw.resolve_workspace

    def resolve(task, board=None):
        real_resolve(task, board=board)
        succ["run"] = _successor_in_window(db_path, task.id)
        if failure == "error":
            raise RuntimeError("clone failed")
        if failure == "unavailable":
            raise WorkspaceUnavailable("workspaces_root_unmounted: test")
        stale = kanban_home / "stale-launch-ws"
        stale.mkdir(exist_ok=True)
        return stale

    monkeypatch.setattr(kbw, "resolve_workspace", resolve)
    with kbc.connect() as conn:
        a = kb.create_task(conn, title="A", assignee="alpha")
        kbd.dispatch_once(conn, spawn_fn=lambda t, w: spawned.append(t.id))
        _assert_successor_untouched(conn, a, succ["run"])
        assert spawned == []
        ws = conn.execute("SELECT workspace_path FROM tasks WHERE id=?", (a,)).fetchone()[0]
        assert ws != str(kanban_home / "stale-launch-ws"), "the old launch wrote the successor's workspace"


def test_baseline_write_after_the_fenced_update_never_lands_on_a_successor_run(
        kanban_home, all_assignees_spawnable, monkeypatch):
    """Prism c08a3c9d1616 (t_89e3e083): R1's fenced workspace UPDATE commits, R2 claims the card, then R1's
    survivor baseline write runs. It is fenced to R1 too: no baseline row for R2's card, R2 untouched, the
    launch is recorded spawn_aborted and nothing spawns."""
    from hermes_cli import kanban_db_workspace as kbw
    db_path = kb.kanban_db_path(board="default")
    succ = {}
    spawned = []
    real_set = kbw._set_task_column

    def set_then_lose_claim(conn, task_id, column, value, run_id=None):
        real_set(conn, task_id, column, value, run_id)
        if column == "workspace_path" and run_id is not None and "run" not in succ:
            succ["r1"] = run_id
            succ["run"] = _successor_in_window(db_path, task_id)

    monkeypatch.setattr(kbw, "_set_task_column", set_then_lose_claim)
    with kbc.connect() as conn:
        a = kb.create_task(conn, title="A", assignee="alpha")
        kbd.dispatch_once(conn, spawn_fn=lambda t, w: spawned.append(t.id))
        _assert_successor_untouched(conn, a, succ["run"])
        assert spawned == []
        row = conn.execute("SELECT bases FROM task_workspace_survivors WHERE task_id=?", (a,)).fetchone()
        assert row is None, f"the old launch recorded its baseline on the successor's card: {row[0]}"
        aborted = conn.execute("SELECT run_id FROM task_events WHERE task_id=? AND kind='spawn_aborted'",
                               (a,)).fetchall()
        assert [r[0] for r in aborted] == [succ["r1"]]


def test_narrow_lock_config_reader():
    assert kbd.narrow_lock_enabled({}) is True
    assert kbd.narrow_lock_enabled({"dispatch": {"narrow_lock": True}}) is True
    assert kbd.narrow_lock_enabled({"dispatch": {"narrow_lock": False}}) is False
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    assert kbd.narrow_lock_enabled(DEFAULT_CONFIG["kanban"]) is True
