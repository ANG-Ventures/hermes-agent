"""S7 D4: the auto-decompose loop names its board; it never flips ``os.environ``.

Pre-S7 the gateway's ``_auto_decompose_tick`` closure set ``HERMES_KANBAN_BOARD`` per board
inside the service thread (M6). ``os.environ`` is process-global, so any other thread resolving
a bare board during that window got the loop's board. After S7 the body runs inside
``scoped_current_board(slug)`` (a ContextVar: this thread only) and passes ``board=slug`` to
``list_triage_ids`` / ``decompose_task`` explicitly.

Drives the REAL watcher loop for one tick. Mutations that must turn it RED: drop the
``scoped_current_board`` wrapper (the in-thread bare read gets ``default``); drop ``board=slug``
from the ``list_triage_ids`` call. Spec: hermes-home
plans/2026-10-21_sdlc-simplification/S7-boards-no-global-pointer-SPEC.md D4, §8 C2.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import hermes_cli
from agent import secret_scope as ss
from gateway import kanban_watchers as watchers
from hermes_cli import config, kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd

from tests.gateway.test_kanban_dispatcher_standby import Clock, cancel, runner


@pytest.fixture
def loop(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_DISPATCH_IN_GATEWAY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    kb.create_board("subs-ace")
    assert kb.get_current_board() == kb.DEFAULT_BOARD

    cfg = {"kanban": {"dispatch_interval_seconds": 2, "auto_decompose": True,
                      "auto_decompose_per_tick": 3}}
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    monkeypatch.setattr(kbd, "resolve_max_in_progress", lambda value: value)
    monkeypatch.setattr(kb, "list_boards", lambda **kw: [{"slug": "subs-ace"}])
    monkeypatch.setattr(kbd, "has_spawnable_ready", lambda conn: False)
    monkeypatch.setattr(kbd, "reap_worker_zombies", Mock(return_value=[]))
    monkeypatch.setattr(kbd, "dispatch_once", lambda conn, **kw: kb.DispatchResult())

    seen: dict = {}

    def list_triage_ids(*args, **kwargs):
        seen["list_kwargs"] = dict(kwargs)
        seen["in_thread"] = kb.get_current_board()
        other: dict = {}
        t = threading.Thread(target=lambda: other.update(board=kb.get_current_board()))
        t.start()
        t.join(timeout=10)
        seen["other_thread"] = other.get("board")
        return ["t_d4"]

    def decompose_task(task_id, **kwargs):
        seen["decompose_kwargs"] = dict(kwargs)
        try:
            seen["secret"] = ss.get_secret("ANTHROPIC_API_KEY")
        except Exception as exc:  # fail-closed read: record it, the secret-scope test asserts
            seen["secret"] = exc
        return SimpleNamespace(ok=False, fanout=False, child_ids=None, reason="test")

    fake = SimpleNamespace(list_triage_ids=list_triage_ids, decompose_task=decompose_task)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_decompose", fake)
    monkeypatch.setattr(hermes_cli, "kanban_decompose", fake, raising=False)

    clock = Clock()
    monkeypatch.setattr(watchers, "asyncio", SimpleNamespace(
        sleep=clock.sleep, to_thread=asyncio.to_thread, CancelledError=asyncio.CancelledError,
        create_task=asyncio.create_task, shield=asyncio.shield,
    ))
    return SimpleNamespace(seen=seen, clock=clock, home=home)


async def _one_decompose_iteration(loop) -> None:
    b = runner()
    task = asyncio.create_task(b._kanban_dispatcher_watcher())
    try:
        for _ in range(20):
            _delay, resume = await loop.clock.paused(task)
            resume.set_result(None)
            if "decompose_kwargs" in loop.seen:
                break
    finally:
        await cancel(task, b)
    assert "decompose_kwargs" in loop.seen, "the auto-decompose body never ran"


@pytest.mark.asyncio
async def test_decompose_loop_scopes_its_board_to_its_own_thread(loop):
    await _one_decompose_iteration(loop)
    seen = loop.seen
    # (i) the board is passed, not left for an ambient read to find.
    assert seen["list_kwargs"].get("board") == "subs-ace", seen
    assert seen["decompose_kwargs"].get("board") == "subs-ace", seen
    # (ii) a callee that still resolves bare inside the body sees the loop's board ...
    assert seen["in_thread"] == "subs-ace", seen
    # ... and nobody else does: another thread's bare read during the same iteration.
    assert seen["other_thread"] == kb.DEFAULT_BOARD, seen
    # The loop leaves no process-global pointer behind.
    assert "HERMES_KANBAN_BOARD" not in os.environ


@pytest.mark.kanban_bare_verb_refuse
def test_shipped_dispatcher_unit_names_its_board(tmp_path, monkeypatch):
    """Prism 56158c7d096a: under the shipped bare-verb refusal the standalone dispatcher unit's
    ExecStart must resolve a board on its own (no pin, no current-board file), else the unit
    exits 2 and crash-loops under systemd. ``daemon`` stays out of BOARD_AGNOSTIC_VERBS."""
    import argparse
    import shlex

    from hermes_cli import kanban as kanban_cli
    from hermes_cli import kanban_board_select as kbsel

    unit = (Path(hermes_cli.__file__).resolve().parent.parent / "plugins" / "kanban" / "systemd"
            / "hermes-kanban-dispatcher.service")
    exec_start = next(line.split("=", 1)[1] for line in unit.read_text(encoding="utf-8").splitlines()
                      if line.startswith("ExecStart="))
    argv = shlex.split(exec_start.replace("%t", str(tmp_path)))
    argv = argv[argv.index("kanban"):]

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_HOME"):
        monkeypatch.delenv(key, raising=False)
    assert "daemon" not in kbsel.BOARD_AGNOSTIC_VERBS
    assert kbsel.bare_verb_policy() == kbsel.BARE_VERB_REFUSE

    root = argparse.ArgumentParser(prog="hermes")
    kanban_cli.build_parser(root.add_subparsers(dest="command"))
    ns = root.parse_args(argv)
    assert ns.kanban_action == "daemon"
    board = getattr(ns, "verb_board", None) or ns.board
    assert kbsel.resolve_slug(board, verb="daemon", card_ids=kbsel.card_ids_of(ns)) == kb.DEFAULT_BOARD
