"""Deferred-wake contract, end to end (t_7a6f4611, W12-3).

``block --kind deferred --until T`` must park without paging, wake to ``ready``
exactly once at T, survive a dispatcher process restart between park and wake
(the wake is DB state, not process state), wake a past ``until`` on the next
tick, and refuse an unparseable ``until`` before touching the card.

The restart case runs each dispatcher tick in a fresh interpreter so no
in-process state can carry the wake across the "restart".
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def blocked_hooks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    fired: list[str] = []
    real = kb._fire_kanban_lifecycle_hook

    def spy(name, task_id, *a, **k):
        if name == "kanban_task_blocked":
            fired.append(task_id)
        return real(name, task_id, *a, **k)

    monkeypatch.setattr(kb, "_fire_kanban_lifecycle_hook", spy)
    return fired


def _ready_card(conn, title: str) -> str:
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    return tid


def _block_cli(tid: str, until: str) -> int:
    return kanban_cli._cmd_block(argparse.Namespace(
        task_id=tid, ids=None, reason=["time", "wait"], kind="deferred", until=until))


def _counts(conn, tid: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in kb.list_events(conn, tid):
        out[e.kind] = out.get(e.kind, 0) + 1
    return out


def _assert_parked_once_woken_once(conn, tid: str) -> None:
    c = _counts(conn, tid)
    assert (c.get("scheduled"), c.get("schedule_elapsed"), c.get("unblocked")) == (1, 1, 1), c
    assert c.get("blocked", 0) == 0 and c.get("dependency_wait", 0) == 0, c
    assert kb.get_task(conn, tid).status == "ready"


def _no_page_candidates(conn) -> bool:
    return kb.needs_input_page_candidates(conn, include_dependency=True, min_priority=0) == []


def test_deferred_wakes_once_at_until_without_paging(kanban_home: Path, blocked_hooks) -> None:
    with kb.connect_closing() as conn:
        tid = _ready_card(conn, "a")
        assert _block_cli(tid, "+3m") == 0
        until = kb.get_task(conn, tid).next_eligible_at
        assert abs(until - (int(time.time()) + 180)) <= 5
        assert _no_page_candidates(conn)
        assert kb.wake_due_scheduled(conn, now=until - 1) == []
        assert kb.wake_due_scheduled(conn, now=until) == [tid]
        assert kb.wake_due_scheduled(conn, now=until + 600) == []
        _assert_parked_once_woken_once(conn, tid)
        assert _no_page_candidates(conn)
    assert blocked_hooks == []


def _tick_in_new_process(home: Path, now: int | None) -> dict:
    """One dispatcher pass in a fresh interpreter: the timed wake at *now*
    (or a full ``dispatch_once`` on the wall clock when *now* is None)."""
    code = (
        "import json, sys\n"
        "from hermes_cli import kanban_db as kb, kanban_db_dispatch as kbd\n"
        "now = json.loads(sys.argv[1])\n"
        "with kb.connect_closing() as conn:\n"
        "    if now is None:\n"
        "        woken = list(kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None).woken_scheduled)\n"
        "    else:\n"
        "        woken = kb.wake_due_scheduled(conn, now=now)\n"
        "    print(json.dumps({'woken': woken, 'db': str(kb.kanban_db_path())}))\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_KANBAN")}
    env.update(HERMES_HOME=str(home), HERMES_KANBAN_SANDBOX="1", HOME=str(home.parent),
               PYTHONPATH=str(REPO))
    proc = subprocess.run([sys.executable, "-c", code, json.dumps(now)], env=env, cwd=str(REPO),
                          capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["db"].startswith(str(home)), out
    return out


def test_deferred_wake_survives_dispatcher_restart_exactly_once(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _ready_card(conn, "b")
        assert _block_cli(tid, "+3m") == 0
        until = kb.get_task(conn, tid).next_eligible_at
    # dispatcher #1 ticks before the wake, then dies; #2 and #3 are new processes.
    assert _tick_in_new_process(kanban_home, None)["woken"] == []
    assert _tick_in_new_process(kanban_home, until)["woken"] == [tid]
    assert _tick_in_new_process(kanban_home, None)["woken"] == []
    with kb.connect_closing() as conn:
        _assert_parked_once_woken_once(conn, tid)
        assert _no_page_candidates(conn)


def test_deferred_until_in_the_past_wakes_on_next_tick(kanban_home: Path) -> None:
    # "worker" is no installed profile here, so the tick wakes the card and leaves it ready.
    with kb.connect_closing() as conn:
        tid = _ready_card(conn, "c")
        past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        assert _block_cli(tid, past) == 0
        assert kb.get_task(conn, tid).status == "scheduled"
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None)
        assert res.woken_scheduled == [tid]
        _assert_parked_once_woken_once(conn, tid)


def test_deferred_unparseable_until_refused_before_any_write(
    kanban_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    with kb.connect_closing() as conn:
        tid = _ready_card(conn, "d")
        before = _counts(conn, tid)
        assert _block_cli(tid, "next tuesday-ish") == 1
        assert "invalid --until 'next tuesday-ish'" in capsys.readouterr().err
        assert _counts(conn, tid) == before
        assert kb.get_task(conn, tid).status == "ready"
