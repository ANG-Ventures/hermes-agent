"""The gateway dispatcher splits ONE load-gate allowance across boards (t_f78d1938).

2026-09-28: the allowance was consumed board-by-board in a fixed order, default
first. With 17 ready cards on default and allowance 4, the subs-ace board got
ZERO spawns for 93 minutes with 7 ready P1 cards. This drives the real watcher
loop over two boards and asserts the second board gets a spawn every tick.
"""

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway import kanban_watchers as watchers
from hermes_cli import config, kanban_db as kb
from hermes_cli import kanban_load_gate as klg

from tests.gateway.test_kanban_dispatcher_standby import Clock, cancel, runner


@pytest.fixture
def boards(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_DISPATCH_IN_GATEWAY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = {"kanban": {"dispatch_interval_seconds": 2, "auto_decompose": False}}
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    monkeypatch.setattr(kb, "resolve_max_in_progress", lambda value: value)
    kb.init_db()

    # "default" first in board order, exactly like the incident.
    ready = {"default": 10, "subs-ace": 3}
    calls = []
    monkeypatch.setattr(
        kb, "list_boards",
        lambda **kw: [{"slug": "default"}, {"slug": "subs-ace"}],
    )
    monkeypatch.setattr(
        kb, "connect",
        lambda board=None, **kw: SimpleNamespace(slug=board, close=lambda: None),
    )
    monkeypatch.setattr(
        kb, "count_spawnable_demand",
        lambda conn, **kw: ready[conn.slug],
    )
    monkeypatch.setattr(kb, "has_spawnable_ready", lambda conn: True)
    monkeypatch.setattr(kb, "reap_worker_zombies", Mock(return_value=[]))

    def fake_dispatch(conn, *, board=None, spawn_paused=None, spawn_limit=None, **kw):
        res = kb.DispatchResult()
        n = 0 if spawn_paused else min(ready[board], spawn_limit if spawn_limit is not None else 99)
        res.spawned = [(f"{board}-{ready[board] - i}", "alpha", None) for i in range(n)]
        ready[board] -= n
        calls.append((board, spawn_limit, spawn_paused, n))
        return res

    monkeypatch.setattr(kb, "dispatch_once", fake_dispatch)
    # Host has room for exactly 4 new workers every tick.
    monkeypatch.setattr(klg.LoadGate, "admit_now", lambda self: (4, None))
    clock = Clock()
    monkeypatch.setattr(watchers, "asyncio", SimpleNamespace(
        sleep=clock.sleep, to_thread=asyncio.to_thread, CancelledError=asyncio.CancelledError,
        create_task=asyncio.create_task, shield=asyncio.shield,
    ))
    return SimpleNamespace(ready=ready, calls=calls, clock=clock, home=home)


@pytest.mark.asyncio
async def test_second_board_gets_a_spawn_every_tick(boards, caplog):
    caplog.set_level(logging.INFO)
    b = runner()
    task = asyncio.create_task(b._kanban_dispatcher_watcher())
    per_tick = []
    try:
        # Initial 5 s wiring delay, then tick sleeps; resume until 3 ticks ran.
        for _ in range(40):
            _delay, resume = await boards.clock.paused(task)
            resume.set_result(None)
            ticks = len(boards.calls) // 2
            if ticks >= 3:
                break
        per_tick = [
            dict((c[0], c[3]) for c in boards.calls[i:i + 2])
            for i in range(0, 6, 2)
        ]
    finally:
        await cancel(task, b)
    assert len(per_tick) == 3, boards.calls
    # subs-ace had 3 ready and gets >= 1 on every tick while it has work.
    assert [t["subs-ace"] for t in per_tick] == [2, 1, 0], per_tick
    assert all(sum(t.values()) == 4 for t in per_tick), per_tick
    # One [board] line per board with ready cards, naming starvation.
    lines = [r.getMessage() for r in caplog.records
             if "ready=" in r.getMessage() and "starved=" in r.getMessage()]
    assert any(l.startswith("kanban dispatcher [subs-ace]: ready=3 quota=2 spawned=2 starved=0")
               for l in lines), lines


@pytest.mark.asyncio
async def test_quota_a_board_cannot_use_passes_to_the_next_board(boards, monkeypatch):
    """Allowance 1; default gets the quota but its concurrency cap lets it
    spawn nothing. The unused quota must reach subs-ace this same tick."""
    monkeypatch.setattr(klg.LoadGate, "admit_now", lambda self: (1, None))
    inner = kb.dispatch_once

    def capped(conn, *, board=None, spawn_paused=None, spawn_limit=None, **kw):
        if board == "default":
            boards.calls.append((board, spawn_limit, spawn_paused, 0))
            return kb.DispatchResult()
        return inner(conn, board=board, spawn_paused=spawn_paused,
                     spawn_limit=spawn_limit, **kw)

    monkeypatch.setattr(kb, "dispatch_once", capped)
    b = runner()
    task = asyncio.create_task(b._kanban_dispatcher_watcher())
    try:
        for _ in range(40):
            _delay, resume = await boards.clock.paused(task)
            resume.set_result(None)
            if len(boards.calls) >= 2:
                break
    finally:
        await cancel(task, b)
    first_tick = dict((c[0], c) for c in boards.calls[:2])
    # default was offered the quota first (tick 0 rotation) and spawned 0.
    assert first_tick["default"][1] == 1, boards.calls
    assert first_tick["default"][3] == 0, boards.calls
    # subs-ace is not paused and spawns the unused quota.
    assert first_tick["subs-ace"][2] is None, boards.calls
    assert first_tick["subs-ace"][1] == 1, boards.calls
    assert first_tick["subs-ace"][3] == 1, boards.calls
