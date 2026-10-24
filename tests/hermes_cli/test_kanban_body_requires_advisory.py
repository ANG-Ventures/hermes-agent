"""A body-sniffed ``requires:`` pin never holds a card silently (t_640a4ea0).

2026-10-23: three ready cards sat 9.5 h at ``placement_wait
requires_unsatisfied:docker host=none`` because the body said "docker run"
(``matched_by: body``); quota was free and nothing spawned them.

(a) a body pin that no host may serve is advisory: the card is placed by its
    other pins and records ``requires_body_ignored:<cap>``;
(b) an explicit ``requires:`` header still waits, and a hold that outlives one
    tick gets ONE comment naming the term, the line and both overrides;
(c) the gateway tick summary counts ``held_by_requires``.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_host_capabilities as khc

from tests.hermes_cli.test_kanban_hard_classes import _card, _tick, home  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _no_real_spawn(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("real spawn in gate test")
    monkeypatch.setattr(kbd, "_default_spawn", boom)


def _mode(monkeypatch, body_mode=None):
    pl = {"capabilities": "enforce"}
    if body_mode:
        pl["body_requires_mode"] = body_mode
    monkeypatch.setattr(kbd, "_root_kanban_cfg", lambda: {"placement": pl})


def _events(conn, tid, kind):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind))]


def _held_comments(conn, tid):
    return [r[0] for r in conn.execute("SELECT body FROM task_comments WHERE task_id=?", (tid,))
            if r[0].startswith(khc.HELD_PREFIX)]


BODY = "Repro the leak: docker run --rm -it alpine sh, then count fds."


def test_body_docker_run_is_placed_with_an_ignored_event(home, monkeypatch):  # noqa: F811
    _mode(monkeypatch)
    with kb.connect_closing() as conn:
        tid = _card(conn, BODY)
        res, spawned = _tick(conn, spawn_limit=0)   # no local slot: the pool ladder decides
        assert tid not in res.placement_waits, res.placement_waits
        assert spawned == [(tid, "ace-ai")]          # the ladder's first free pool host
        (ev,) = _events(conn, tid, khc.BODY_IGNORED_EVENT)
        assert ev["reason"] == "requires_body_ignored:docker" and ev["term"] == "docker run"


def test_kill_switch_enforce_restores_the_hold(home, monkeypatch):  # noqa: F811
    _mode(monkeypatch, "enforce")
    with kb.connect_closing() as conn:
        tid = _card(conn, BODY)
        res, spawned = _tick(conn)
        assert spawned == [] and res.placement_waits[tid] == "requires_unsatisfied:docker"
        assert _events(conn, tid, khc.BODY_IGNORED_EVENT) == []


def test_declared_requires_waits_and_comments_once_on_tick_two(home, monkeypatch):  # noqa: F811
    _mode(monkeypatch)
    with kb.connect_closing() as conn:
        tid = _card(conn, "requires: docker\nBring the stack up and run the e2e.")
        per_tick = []
        for _ in range(3):
            res, spawned = _tick(conn)
            assert spawned == [] and res.placement_waits[tid] == "requires_unsatisfied:docker"
            per_tick.append(len(_held_comments(conn, tid)))
        assert per_tick == [0, 1, 1]
        (c,) = _held_comments(conn, tid)
        assert "docker" in c and "`requires: docker`" in c
        assert "`host: studio`" in c and "`requires: none`" in c
        assert len(_events(conn, tid, khc.HELD_EVENT)) == 1


def test_tick_summary_counts_held_by_requires(caplog):
    from gateway import kanban_watchers as w
    res = SimpleNamespace(
        placement_affinity={"t_a": {"host": "none"}, "t_b": {"host": "none"}, "t_c": {"host": "ace-ai"}},
        placement_waits={"t_a": "requires_unsatisfied:docker", "t_b": "local_full",
                         "t_c": "requires_unsatisfied:docker"})
    assert w._held_by_requires(res) == 1
    assert w._held_by_requires(SimpleNamespace()) == 0
