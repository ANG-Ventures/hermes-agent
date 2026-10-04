"""Worker-host spillover (kanban.worker_hosts, t_5981ff03).

Contract: with no worker host configured a gate-paused tick spawns nothing
(unchanged); with one, eligible cards (scratch + allowlisted assignee) spawn
with a placement, up to the host's free slots, and every placement is a
``worker_placed`` event on the run. The worker process re-asserts the
placement over its profile's ``terminal.backend``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_hosts as kwh


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    for p in ("alpha", "beta"):
        (home / "profiles" / p).mkdir(parents=True, exist_ok=True)
    with kb.connect_closing():
        kb.create_board(slug="default", name="Test")
    return home


HOST_CFG = [{
    "name": "ace-ai", "ssh_host": "ace-ai", "ssh_user": "kanbanw",
    "max_workers": 2, "pause_above": 18, "profiles": ["alpha"],
}]


def _plan(load1=5.0, cfg=HOST_CFG):
    hosts = kwh.parse_worker_hosts(cfg)
    return lambda conn: kwh.plan_spillover(conn, hosts, probe=lambda h: load1)


def _tick(spillover_fn, spawned_with):
    def spawn(task, workspace, *, board=None, placement=None):
        spawned_with.append((task.id, placement.name if placement else None))
        return 4242
    with kb.connect_closing() as conn:
        return kbd.dispatch_once(
            conn, spawn_fn=spawn, max_spawn=64,
            spawn_paused="load1=150.0 > pause_above=64.0", spawn_limit=0,
            spillover_fn=spillover_fn,
        )


def _make(n, assignee="alpha", body="host:any"):
    with kb.connect_closing() as conn:
        return [kb.create_task(conn, title=f"t{i}", assignee=assignee, body=body)
                for i in range(n)]


def test_parse_drops_incomplete_and_unallowlisted_entries():
    assert kwh.parse_worker_hosts(None) == []
    assert kwh.parse_worker_hosts([{"name": "x", "ssh_host": "h", "ssh_user": "u",
                                    "max_workers": 2, "pause_above": 9}]) == []
    assert kwh.parse_worker_hosts(HOST_CFG + [dict(HOST_CFG[0], enabled=False)])[0].name == "ace-ai"
    assert len(kwh.parse_worker_hosts(HOST_CFG + HOST_CFG)) == 1


def test_paused_tick_without_worker_hosts_spawns_nothing(kanban_home):
    _make(3)
    spawned = []
    res = _tick(lambda conn: None, spawned)
    assert spawned == [] and res.spawned == [] and res.spawn_paused


def test_paused_tick_spills_eligible_cards_up_to_free_slots(kanban_home):
    ids = _make(3)
    other = _make(1, assignee="beta")
    spawned = []
    res = _tick(_plan(), spawned)
    assert [h for _, h in spawned] == ["ace-ai", "ace-ai"]
    assert set(res.placements) <= set(ids) and len(res.placements) == 2
    assert other[0] not in res.placements
    with kb.connect_closing() as conn:
        rows = conn.execute(
            "SELECT e.task_id, e.payload FROM task_events e JOIN tasks t ON t.id=e.task_id "
            "WHERE e.kind=? AND e.run_id=t.current_run_id", (kwh.PLACED_EVENT,),
        ).fetchall()
        assert {r[0] for r in rows} == set(res.placements)
        assert all(json.loads(r[1])["host"] == "ace-ai" for r in rows)
        # Slots are counted from running placements: the host is now full.
        assert kwh.running_by_host(conn) == {"ace-ai": 2}
    spawned2 = []
    res2 = _tick(_plan(), spawned2)
    assert spawned2 == [] and "running=2/2" in (res2.spillover or "")


def test_hot_or_unreachable_worker_host_takes_nothing(kanban_home):
    _make(2)
    for load1 in (19.0, None):
        spawned = []
        res = _tick(_plan(load1=load1), spawned)
        assert spawned == [] and "slots=0" in (res.spillover or "")


def test_non_scratch_cards_never_spill(kanban_home):
    (tid,) = _make(1)
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET workspace_kind='worktree' WHERE id=?", (tid,))
        conn.commit()
    spawned = []
    _tick(_plan(), spawned)
    assert spawned == []


def test_cards_without_host_any_opt_in_never_spill(kanban_home):
    """Allowlist + scratch is not enough: the card must opt in (t_7c0ad8db)."""
    _make(1, body=None)
    _make(1, body="Fix the thing.\nUse `host:any` only if it is portable.")
    (opted,) = _make(1, body="Fix the thing.\n  HOST: any  \n")
    spawned = []
    res = _tick(_plan(), spawned)
    assert spawned == [(opted, "ace-ai")]
    assert list(res.placements) == [opted]


def test_opted_in_marker_is_line_anchored():
    assert kwh.opted_in("host:any")
    assert kwh.opted_in("body\nhost: any\nmore")
    for body in (None, "", "hostany", "host:anything", "see host:any below", "`host:any`"):
        assert not kwh.opted_in(body), body


def test_placement_env_survives_profile_terminal_backend(monkeypatch):
    host = kwh.parse_worker_hosts(HOST_CFG)[0]
    env: dict = {}
    kwh.apply_placement(env, host, "/Volumes/fleet-scratch/workspaces/default/t_x")
    # The profile's config.yaml bridge has pulled the backend back to local.
    env.update({"TERMINAL_ENV": "local", "TERMINAL_CWD": "."})
    assert kwh.reapply_placement_env(env) == "ace-ai"
    assert env["TERMINAL_ENV"] == "ssh"
    assert env["TERMINAL_SSH_USER"] == "kanbanw"
    assert env["TERMINAL_CWD"] == "/Volumes/fleet-scratch/workspaces/default/t_x"


def test_terminal_tool_config_bridge_keeps_placement(monkeypatch):
    import tools.terminal_tool as tt
    host = kwh.parse_worker_hosts(HOST_CFG)[0]
    env: dict = {}
    kwh.apply_placement(env, host, "/tmp/ws")
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    def fake_bridge(env=None, override=False):
        import os
        os.environ["TERMINAL_ENV"] = "local"

    monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", False)
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {"terminal": {"backend": "local"}})
    monkeypatch.setattr("hermes_cli.config.apply_terminal_config_to_env", fake_bridge)
    cfg = tt._get_env_config()
    assert cfg["env_type"] == "ssh"
    assert cfg["ssh_user"] == "kanbanw"



def test_cards_no_host_can_take_are_never_claimed(kanban_home):
    """Budget is summed across hosts; per-assignee capacity is not.

    Host A takes alpha (2 slots), host B takes beta (2 slots): 4 ready alpha
    cards must spawn 2 and leave 2 ready, unclaimed, with no spawn failure.
    """
    cfg = [dict(HOST_CFG[0]), dict(HOST_CFG[0], name="host-b", ssh_host="b", profiles=["beta"])]
    ids = _make(4)
    spawned = []
    res = _tick(_plan(cfg=cfg), spawned)
    assert [h for _, h in spawned] == ["ace-ai", "ace-ai"]
    assert res.spawn_failed == [] and res.auto_blocked == []
    marks = ",".join("?" * len(ids))
    with kb.connect_closing() as conn:
        statuses = sorted(r[0] for r in conn.execute(
            f"SELECT status FROM tasks WHERE id IN ({marks})", ids,
        ).fetchall())
        assert statuses == ["ready", "ready", "running", "running"]
        claimed = conn.execute(
            f"SELECT COUNT(DISTINCT task_id) FROM task_events WHERE kind = 'claimed' "
            f"AND task_id IN ({marks})", ids,
        ).fetchone()[0]
        assert claimed == 2


def test_parent_cards_and_prefilled_workspaces_stay_local(kanban_home, tmp_path):
    parent, prefilled = _make(2)
    (child,) = _make(1, assignee="beta")
    ws = tmp_path / "legacy-ws"
    ws.mkdir()
    (ws / "handoff.md").write_text("x")
    with kb.connect_closing() as conn:
        conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)", (parent, child))
        conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(ws), prefilled))
        conn.commit()
    spawned = []
    _tick(_plan(), spawned)
    assert spawned == []



def test_linked_children_stay_local_too(kanban_home):
    (parent,) = _make(1, assignee="beta")
    (child,) = _make(1)
    with kb.connect_closing() as conn:
        conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)", (parent, child))
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        conn.commit()
    spawned = []
    _tick(_plan(), spawned)
    assert spawned == []


def test_unreadable_local_workspace_fails_closed(tmp_path, monkeypatch):
    assert kwh.local_workspace_has_content(None) is False
    assert kwh.local_workspace_has_content(str(tmp_path / "missing")) is False

    def boom(path):
        raise PermissionError(13, "denied", path)

    monkeypatch.setattr(kwh.os, "scandir", boom)
    assert kwh.local_workspace_has_content(str(tmp_path)) is True
