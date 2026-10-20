"""`hermes kanban dispatch` (one-shot) obeys kanban.dispatch_load_gate (t_689b81b7).

2026-09-29 18:37: `kanban dispatch --max 128` at load1 66 (pause_above 64)
spawned 33 workers because the one-shot verb never built the gate the gateway
loop and `kanban daemon` apply; the Studio reached load1 243. Earlier the same
day `--max 6` was read as "stop once 6 are running". Contract:

* a paused gate spawns 0 from the CLI and prints ``PAUSED load1=... (pause_above=...)``;
* ``--max N`` is additive (N more this call), ``--max-running`` is the ceiling;
* ``--ignore-load-gate`` spawns, prints ``OVERRIDE by <profile>`` and records a
  ``load_gate_override`` event on every card it spawned.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_load_gate as klg

GATE = {"enabled": True, "pause_above": 64, "resume_below": 48,
        "max_spawn_per_tick": 16}


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    monkeypatch.setattr("hermes_cli.config.load_config",
                        lambda: {"kanban": {"dispatch_load_gate": dict(GATE)}})
    monkeypatch.setattr(kb_cli, "_collect_review_awaiting_human",
                        lambda **k: [], raising=False)
    (home / "profiles" / "alpha").mkdir(parents=True, exist_ok=True)
    # Never start real workers: route the CLI's dispatch_once to a stub spawn.
    real = kbd.dispatch_once
    monkeypatch.setattr(kbd, "dispatch_once",
                        lambda conn, **kw: real(conn, spawn_fn=lambda *a, **k: 1, **kw))
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        for i in range(20):
            kb.create_task(conn, title=f"t{i}", assignee="alpha")
    return home


def _load(monkeypatch, load1, load5=None, cpu_busy=None):
    monkeypatch.setattr(klg, "sample_loadavg",
                        lambda: (load1, load1 if load5 is None else load5))
    # The live CPU sample would make these tests depend on the runner.
    monkeypatch.setattr(klg, "sample_cpu_busy",
                        lambda prev=None, block=0.0: (cpu_busy, None))


def _run(**kw) -> int:
    ns = dict(dry_run=False, max=None, max_running=None, ignore_load_gate=False,
              failure_limit=2, json=False)
    ns.update(kw)
    assert kb_cli._cmd_dispatch(argparse.Namespace(**ns)) == 0


def _running() -> int:
    with kb.connect_closing() as conn:
        return kbd.count_running_tasks(conn)


def test_paused_gate_spawns_zero_and_says_why(board, monkeypatch, capsys):
    _load(monkeypatch, 66.0)
    _run(max=128)
    out = capsys.readouterr().out
    assert _running() == 0
    assert "PAUSED load1=66.0 (pause_above=64.0)" in out
    assert "Spawned:      0" in out


def test_max_is_additive_not_a_running_ceiling(board, monkeypatch, capsys):
    _load(monkeypatch, 1.0)
    _run(max=10)
    assert _running() == 10
    capsys.readouterr()
    _run(max=3)
    out = capsys.readouterr().out
    assert _running() == 13
    assert "--max=3 reached: 3 spawned this call (additive)" in out


def test_max_running_is_the_ceiling_and_is_named(board, monkeypatch, capsys):
    _load(monkeypatch, 1.0)
    _run(max=10)
    capsys.readouterr()
    _run(max=3, max_running=10)
    out = capsys.readouterr().out
    assert _running() == 10
    assert "running ceiling --max-running fired" in out


def test_one_shot_counts_recent_spawns_as_pending_ramp(board, monkeypatch, capsys):
    # 16 spawns in the last ramp window = 32 pending load; load1 40 + 32 > 64.
    _load(monkeypatch, 1.0)
    _run(max=16)
    assert _running() == 16
    capsys.readouterr()
    _load(monkeypatch, 40.0)
    _run(max=4)
    out = capsys.readouterr().out
    assert _running() == 16
    assert "SATURATED load1=40.0" in out
    # Pending fades linearly over the 600 s ramp (t_bf26e8f1), so the
    # seconds this test takes shave a little off the 16 x 2.0 = 32.0.
    import re
    m = re.search(r"pending_ramp=([\d.]+)", out)
    assert m and 31.0 <= float(m.group(1)) <= 32.0, out


def test_ignore_load_gate_spawns_prints_and_records_event(board, monkeypatch, capsys):
    _load(monkeypatch, 66.0)
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "apollo")
    _run(max=2, ignore_load_gate=True)
    out = capsys.readouterr().out
    assert _running() == 2
    assert "load1=66.0 pause_above=64.0 OVERRIDE by apollo" in out
    with kb.connect_closing() as conn:
        rows = conn.execute(
            "SELECT task_id, payload FROM task_events WHERE kind='load_gate_override'"
        ).fetchall()
        running = {r[0] for r in conn.execute(
            "SELECT id FROM tasks WHERE status='running'").fetchall()}
    assert {r[0] for r in rows} == running and len(rows) == 2
    assert '"profile": "apollo"' in rows[0][1]


def test_disabled_gate_leaves_cli_ungated(board, monkeypatch, capsys):
    monkeypatch.setattr("hermes_cli.config.load_config",
                        lambda: {"kanban": {"dispatch_load_gate": {"enabled": False}}})
    _load(monkeypatch, 66.0)
    _run(max=5)
    assert _running() == 5


def test_utilization_cli_leases_only_local_starts(board, monkeypatch):
    """Prism 9f811a3f59ff: a run started on a pool host (worker_placed event) is not a lease on THIS host."""
    import time as _t

    from hermes_cli import kanban_ops as kops
    from hermes_cli import kanban_worker_pool as kwp

    _load(monkeypatch, 1.0, cpu_busy=0.1)
    monkeypatch.setattr(klg, "count_running_workers", lambda: 0)
    monkeypatch.setattr(klg, "sample_user_procs", lambda: (None, None))
    _run(max=2)                                           # two local starts, now
    with kb.connect_closing() as conn:
        runs = [r[0] for r in conn.execute("SELECT id FROM task_runs ORDER BY id").fetchall()]
        assert len(runs) == 2
        tid = conn.execute("SELECT task_id FROM task_runs WHERE id=?", (runs[1],)).fetchone()[0]
        conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                     (tid, runs[1], kwp.PLACED_EVENT, '{"host": "ace-ai"}', int(_t.time())))
        conn.commit()
        booked = []
        gate = klg.LoadGate(dict(GATE), 32)
        gate.admission = "utilization"
        monkeypatch.setattr(gate, "record_spawns", lambda n, now=None: booked.append(n))
        monkeypatch.setattr(gate, "admit", lambda *a, **k: (0, "stub"))
        kops._cli_utilization_gate(conn, argparse.Namespace(ignore_load_gate=False), gate,
                                   {"spawn_limit": None}, lambda m: None, 1.0)
    assert booked == [1]                                  # the placed-remote start is not booked locally


def test_utilization_cli_counts_and_leases_across_boards(board, monkeypatch):
    """Prism d88c700fe427 / 9f811a3f59ff: the one-shot gate reads EVERY board, like the gateway tick.

    Board ``default`` runs 2 local workers; board ``other`` runs 1 placed on ace-ai and 1 local. The local
    running count is 3 (all boards' total 4 minus all boards' remote 1), not 4 - 0 (this board's remote out
    of an all-board total), and ``other``'s local start inside the settle window is booked as a lease too.
    """
    import time as _t

    from hermes_cli import kanban_ops as kops
    from hermes_cli import kanban_worker_pool as kwp

    _load(monkeypatch, 1.0, cpu_busy=0.1)
    monkeypatch.setattr(klg, "sample_user_procs", lambda: (None, None))
    _run(max=2)                                           # two local starts on default
    kb.create_board(slug="other", name="Other")
    with kb.connect_closing(board="other") as oc:
        for i in range(2):
            kb.create_task(oc, title=f"o{i}", assignee="alpha")
        kbd.dispatch_once(oc, max_spawn=2)                # the fixture's stub spawn
        tid, run_id = oc.execute(
            "SELECT id, current_run_id FROM tasks WHERE status='running' ORDER BY id LIMIT 1").fetchone()
        oc.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                   (tid, run_id, kwp.PLACED_EVENT, '{"host": "ace-ai"}', int(_t.time())))
        oc.commit()
    booked, seen = [], {}
    gate = klg.LoadGate(dict(GATE), 32)
    gate.admission = "utilization"
    monkeypatch.setattr(gate, "record_spawns", lambda n, now=None: booked.append(n))

    def admit(*a, **k):
        seen.update(k)
        return 0, "stub"
    monkeypatch.setattr(gate, "admit", admit)
    with kb.connect_closing() as conn:
        kops._cli_utilization_gate(conn, argparse.Namespace(ignore_load_gate=False), gate,
                                   {"spawn_limit": None}, lambda m: None, 1.0)
    assert seen["running"] == 3                           # 2 + 2 running, 1 of them on ace-ai
    assert len(booked) == 3                               # default's 2 local starts + other's 1 local start
