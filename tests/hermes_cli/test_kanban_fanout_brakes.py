"""Kanban fan-out brakes: worker-created cards park, boards obey a USD ceiling.

Two independent brakes, one incident (2026-09-22): ~200 human-carded items
became ~730 worked cards / ~$13K in two days because dispatched workers created
310 child cards via ``kanban_create`` and every one of them auto-dispatched.

A. Worker-created cards land in ``kanban.worker_created_status`` (default
   ``triage``) instead of ``ready``, so a human promotes them. The marker for
   "I am a dispatched worker" is the env var ``HERMES_KANBAN_TASK`` — the
   worker's own card id, injected by the dispatcher — NOT the profile name.

B. A board whose 24h worker spend has crossed ``kanban.budget.usd_per_24h``
   stops spawning, writes a pause marker, and pages ONCE per episode.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_spawn_factory(spawns: list):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242
    return fake_spawn


# ---------------------------------------------------------------------------
# SPEC A — worker-created cards park
# ---------------------------------------------------------------------------


def test_worker_env_marker_is_the_dispatched_worker_signal(monkeypatch):
    from hermes_cli import kanban_worker_policy as kwp

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert kwp.is_dispatched_worker() is False
    # A profile name alone must NOT count — Apollo runs as a profile too.
    monkeypatch.setenv("HERMES_PROFILE", "daedalus-opus")
    assert kwp.is_dispatched_worker() is False
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_deadbeef")
    assert kwp.is_dispatched_worker() is True
    # Blank/whitespace is not a marker.
    monkeypatch.setenv("HERMES_KANBAN_TASK", "   ")
    assert kwp.is_dispatched_worker() is False


def test_configured_worker_created_status_defaults_to_triage(monkeypatch):
    from hermes_cli import kanban_worker_policy as kwp

    monkeypatch.setattr(kwp, "_load_config", lambda: {})
    assert kwp.configured_worker_created_status() == "triage"
    monkeypatch.setattr(
        kwp, "_load_config", lambda: {"kanban": {"worker_created_status": "ready"}}
    )
    assert kwp.configured_worker_created_status() == "ready"
    # A bogus value must not brick creation — fall back to the safe default.
    monkeypatch.setattr(
        kwp, "_load_config", lambda: {"kanban": {"worker_created_status": "nope"}}
    )
    assert kwp.configured_worker_created_status() == "triage"


def test_resolve_park_status_only_parks_worker_default_creates(monkeypatch):
    from hermes_cli import kanban_worker_policy as kwp

    monkeypatch.setattr(kwp, "_load_config", lambda: {})

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert kwp.resolve_park_status(initial_status="running", triage=False) is None

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc12345")
    assert kwp.resolve_park_status(initial_status="running", triage=False) == "triage"
    assert kwp.resolve_park_status(initial_status=None, triage=False) == "triage"
    # An explicit human-ops hold is untouched.
    assert kwp.resolve_park_status(initial_status="blocked", triage=False) is None
    # Already asking for triage — nothing to override.
    assert kwp.resolve_park_status(initial_status="running", triage=True) is None
    # Legacy restore.
    monkeypatch.setattr(
        kwp, "_load_config", lambda: {"kanban": {"worker_created_status": "ready"}}
    )
    assert kwp.resolve_park_status(initial_status="running", triage=False) is None


def test_create_task_forced_status_parks_and_records_event(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="parked child",
            assignee="argus",
            forced_status="triage",
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "triage"
        events = kb.list_events(conn, tid)
        parked = [e for e in events if e.kind == "parked_by_policy"]
        assert parked, [e.kind for e in events]
        assert parked[0].payload["status"] == "triage"
        assert parked[0].payload["knob"] == "kanban.worker_created_status"


def test_kanban_create_tool_parks_worker_created_card(kanban_home, monkeypatch):
    from tools import kanban_tools

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_parent01")
    monkeypatch.setattr(
        "hermes_cli.kanban_worker_policy._load_config", lambda: {}
    )
    out = json.loads(
        kanban_tools._handle_create({"title": "child from worker", "assignee": "argus"})
    )
    assert out.get("ok") is True, out
    assert out["status"] == "triage", out
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, out["task_id"]).status == "triage"


def test_kanban_create_tool_leaves_non_worker_card_ready(kanban_home, monkeypatch):
    from tools import kanban_tools

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(
        "hermes_cli.kanban_worker_policy._load_config", lambda: {}
    )
    out = json.loads(
        kanban_tools._handle_create({"title": "apollo card", "assignee": "argus"})
    )
    assert out.get("ok") is True, out
    assert out["status"] == "ready", out


def test_cli_create_parks_worker_created_card(kanban_home, monkeypatch):
    from hermes_cli import kanban as kcli

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_parent02")
    monkeypatch.setattr(
        "hermes_cli.kanban_worker_policy._load_config", lambda: {}
    )
    payload = json.loads(
        kcli.run_slash("create 'cli child' --assignee argus --json")
    )
    assert payload["status"] == "triage", payload


def test_cli_create_non_worker_stays_ready(kanban_home, monkeypatch):
    from hermes_cli import kanban as kcli

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(
        "hermes_cli.kanban_worker_policy._load_config", lambda: {}
    )
    payload = json.loads(
        kcli.run_slash("create 'human card' --assignee argus --json")
    )
    assert payload["status"] == "ready", payload


def test_dispatcher_never_spawns_a_triage_card(kanban_home, all_assignees_spawnable):
    """The park is only a brake if the dispatcher refuses triage cards."""
    spawns: list[str] = []
    with kb.connect_closing() as conn:
        parked = kb.create_task(
            conn, title="parked", assignee="argus", forced_status="triage",
        )
        live = kb.create_task(conn, title="live", assignee="argus")
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))
    assert parked not in spawns
    assert live in spawns


# ---------------------------------------------------------------------------
# SPEC B — per-board 24h USD ceiling
# ---------------------------------------------------------------------------


def _install_notify(home: Path) -> Path:
    """A notify.py in the sandbox home so the paging path is exercised.

    Without this the page is a deliberate no-op (see
    kanban_budget._notify_script_path): a sandboxed board must never fire a
    real alert about cards that do not exist on the real board.
    """
    scripts = home / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    path = scripts / "notify.py"
    path.write_text("raise SystemExit(0)\n", encoding="utf-8")
    return path


def _write_ledger(home: Path, profile: str, rows) -> Path:
    d = home / "profiles" / profile / "blackbox"
    d.mkdir(parents=True, exist_ok=True)
    db = d / "turns.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS turns "
        "(ts_start INTEGER, cost_usd REAL, user_text TEXT, model TEXT, platform TEXT)"
    )
    conn.executemany(
        "INSERT INTO turns (ts_start, cost_usd, user_text, model, platform) "
        "VALUES (?, ?, ?, ?, ?)",
        [(ts, cost, text, "m", "cli") for (ts, cost, text) in rows],
    )
    conn.commit()
    conn.close()
    return db


def test_board_spend_sums_only_in_window_cards_of_this_board(kanban_home):
    from hermes_cli import kanban_budget

    now = int(time.time())
    with kb.connect_closing() as conn:
        mine = kb.create_task(conn, title="a", assignee="argus")
        mine2 = kb.create_task(conn, title="b", assignee="argus")
    _write_ledger(
        kanban_home,
        "daedalus-opus",
        [
            (now - 60, 3.0, f"work kanban task {mine}"),
            (now - 120, 2.0, f"work kanban task {mine2}"),
            # Out of window.
            (now - 90000, 100.0, f"work kanban task {mine}"),
            # Another board's card id — not ours.
            (now - 60, 50.0, "work kanban task t_ffffffff"),
            # Not a worker turn at all.
            (now - 60, 7.0, "hello there"),
        ],
    )
    _write_ledger(kanban_home, "argus", [(now - 30, 1.5, f"work kanban task {mine}")])

    spend = kanban_budget.board_spend_usd(
        kb.kanban_db_path(), window_hours=24, home=kanban_home,
    )
    assert spend == pytest.approx(6.5)


def test_board_spend_tolerates_missing_and_broken_ledgers(kanban_home):
    from hermes_cli import kanban_budget

    # No profiles dir at all.
    assert kanban_budget.board_spend_usd(
        kb.kanban_db_path(), window_hours=24, home=kanban_home,
    ) == 0.0
    # A file that is not a database.
    d = kanban_home / "profiles" / "broken" / "blackbox"
    d.mkdir(parents=True)
    (d / "turns.db").write_text("not a database", encoding="utf-8")
    assert kanban_budget.board_spend_usd(
        kb.kanban_db_path(), window_hours=24, home=kanban_home,
    ) == 0.0


def test_board_spend_cache_is_computed_once_per_tick(kanban_home, monkeypatch):
    from hermes_cli import kanban_budget

    now = int(time.time())
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="cached", assignee="argus")
    _write_ledger(
        kanban_home, "daedalus-opus", [(now - 60, 2.0, f"work kanban task {tid}")],
    )
    calls: list[int] = []
    real = kanban_budget._sum_ledgers

    def counting(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(kanban_budget, "_sum_ledgers", counting)
    cache: dict = {}
    path = kb.kanban_db_path()
    first = kanban_budget.board_spend_usd(
        path, window_hours=24, home=kanban_home, cache=cache,
    )
    second = kanban_budget.board_spend_usd(
        path, window_hours=24, home=kanban_home, cache=cache,
    )
    assert first == second == pytest.approx(2.0)
    assert len(calls) == 1


def test_dispatch_skips_spawn_over_ceiling_and_pages_once(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    from hermes_cli import kanban_budget

    now = int(time.time())
    spawns: list[str] = []
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="expensive", assignee="argus")
    _write_ledger(
        kanban_home, "daedalus-opus", [(now - 60, 25.0, f"work kanban task {tid}")],
    )
    monkeypatch.setattr(
        kanban_budget, "_load_config",
        lambda: {"kanban": {"budget": {"usd_per_24h": 10.0}}},
    )
    _install_notify(kanban_home)
    sent: list[list[str]] = []
    monkeypatch.setattr(kanban_budget, "_run_notify", lambda argv: sent.append(argv))

    with kb.connect_closing() as conn:
        res1 = kb.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))
        res2 = kb.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))

    assert spawns == []
    assert res1.budget_paused is True
    assert res2.budget_paused is True
    marker = kb.board_dir(kb.DEFAULT_BOARD) / ".budget_paused.json"
    assert marker.exists()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["ceiling"] == 10.0
    assert data["spend"] == pytest.approx(25.0)
    assert "since" in data
    # Exactly ONE page for the whole pause episode, not one per tick.
    assert len(sent) == 1, sent
    assert any("expensive" not in a for a in sent[0])


def test_dispatch_spawns_under_ceiling(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    from hermes_cli import kanban_budget

    now = int(time.time())
    spawns: list[str] = []
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="cheap", assignee="argus")
    _write_ledger(
        kanban_home, "daedalus-opus", [(now - 60, 1.0, f"work kanban task {tid}")],
    )
    monkeypatch.setattr(
        kanban_budget, "_load_config",
        lambda: {"kanban": {"budget": {"usd_per_24h": 10.0}}},
    )
    monkeypatch.setattr(kanban_budget, "_run_notify", lambda argv: None)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))
    assert res.budget_paused is False
    assert spawns == [tid]


def test_ceiling_unset_is_off(kanban_home, monkeypatch, all_assignees_spawnable):
    from hermes_cli import kanban_budget

    now = int(time.time())
    spawns: list[str] = []
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="unbounded", assignee="argus")
    _write_ledger(
        kanban_home, "daedalus-opus", [(now - 60, 9999.0, f"work kanban task {tid}")],
    )
    monkeypatch.setattr(kanban_budget, "_load_config", lambda: {})
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))
    assert res.budget_paused is False
    assert spawns == [tid]


def test_recovery_clears_marker_and_reports_once(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    from hermes_cli import kanban_budget

    marker = kb.board_dir(kb.DEFAULT_BOARD) / ".budget_paused.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"since": 1, "spend": 25.0, "ceiling": 10.0}), encoding="utf-8",
    )
    spawns: list[str] = []
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="recovered", assignee="argus")
    monkeypatch.setattr(
        kanban_budget, "_load_config",
        lambda: {"kanban": {"budget": {"usd_per_24h": 10.0}}},
    )
    _install_notify(kanban_home)
    sent: list[list[str]] = []
    monkeypatch.setattr(kanban_budget, "_run_notify", lambda argv: sent.append(argv))
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))
    assert res.budget_paused is False
    assert not marker.exists()
    assert spawns == [tid]
    assert len(sent) == 1, sent
    assert "--target" in sent[0]


def test_dry_run_never_writes_a_marker_or_pages(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """``--dry-run`` is the documented SAFE probe: observe, never mutate.

    A dry run that wrote the pause marker and fired a real page would make the
    one command an operator reaches for during an incident itself an actor.
    """
    from hermes_cli import kanban_budget

    now = int(time.time())
    spawns: list[str] = []
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="probe", assignee="argus")
    _write_ledger(
        kanban_home, "daedalus-opus", [(now - 60, 25.0, f"work kanban task {tid}")],
    )
    monkeypatch.setattr(
        kanban_budget, "_load_config",
        lambda: {"kanban": {"budget": {"usd_per_24h": 10.0}}},
    )
    _install_notify(kanban_home)
    sent: list[list[str]] = []
    monkeypatch.setattr(kanban_budget, "_run_notify", lambda argv: sent.append(argv))
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), dry_run=True,
        )
    assert res.budget_paused is False
    assert sent == []
    assert not (kb.board_dir(kb.DEFAULT_BOARD) / ".budget_paused.json").exists()


def test_pause_without_a_notify_script_is_a_silent_noop(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """A sandbox home has no notify.py — the pause must still hold, silently.

    The board must stop spawning either way; what must NOT happen is a real
    alert about cards that only exist in a test home.
    """
    from hermes_cli import kanban_budget

    now = int(time.time())
    spawns: list[str] = []
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="sandboxed", assignee="argus")
    _write_ledger(
        kanban_home, "daedalus-opus", [(now - 60, 25.0, f"work kanban task {tid}")],
    )
    monkeypatch.setattr(
        kanban_budget, "_load_config",
        lambda: {"kanban": {"budget": {"usd_per_24h": 10.0}}},
    )
    sent: list[list[str]] = []
    monkeypatch.setattr(kanban_budget, "_run_notify", lambda argv: sent.append(argv))
    assert not (kanban_home / "scripts" / "notify.py").exists()
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))
    assert res.budget_paused is True
    assert spawns == []
    assert sent == []


def test_cli_budget_subcommand_is_read_only(kanban_home, monkeypatch):
    from hermes_cli import kanban as kcli
    from hermes_cli import kanban_budget

    now = int(time.time())
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="argus")
    _write_ledger(
        kanban_home, "daedalus-opus", [(now - 60, 4.25, f"work kanban task {tid}")],
    )
    monkeypatch.setattr(
        kanban_budget, "_load_config",
        lambda: {"kanban": {"budget": {"usd_per_24h": 10.0}}},
    )
    out = kcli.run_slash("budget")
    assert "4.25" in out, out
    assert "10.00" in out, out
    # Read-only: no pause marker written by a report.
    assert not (kb.board_dir(kb.DEFAULT_BOARD) / ".budget_paused.json").exists()
