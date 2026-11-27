"""Upstream small-VM memory guards stand down under fleet admission (t_2158f095).

Apollo ruling t_2158f095-round4-integration 2 (Ace msg 1569308234441949345, "only fwa is the guard/gate"): the
derived ``max_in_progress`` default (MemTotal / 512 MB clamped 2..8) and ``_memory_pressure_level`` (critical = spawn
nothing, elevated = at most one) stay in the code for non-fleet installs, and are inert whenever
``kanban.admission: utilization`` (the fleet admission path) is set in config.yaml. With it unset/legacy, upstream
behaviour is unchanged. The config is read through the real loader from a temp HERMES_HOME.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

GIB = 1024 * 1024  # KiB per GiB
# A 1 GiB Linux VM at critical pressure: the derived cap is 2, the pressure guard spawns nothing.
LOW = {"mem_available_kib": 32 * 1024, "mem_total_kib": 1 * GIB}


def _home(tmp_path, monkeypatch, admission, profile_admission=None, gate_enabled=None):
    """``admission`` goes in the FLEET-ROOT config.yaml (PLACEMENT_HOME, what placement.admission_mode reads);
    ``profile_admission`` (default: the same) in the profile's HERMES_HOME config.yaml. ``gate_enabled`` sets
    ``kanban.dispatch_load_gate.enabled`` in the profile config (None = key absent, the gate's default: on)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    root = tmp_path / "fleet-root"
    root.mkdir()
    profile_admission = admission if profile_admission is None else profile_admission
    if admission is not None:
        (root / "config.yaml").write_text(f"kanban:\n  admission: {admission}\n")
    lines = []
    if profile_admission is not None:
        lines.append(f"  admission: {profile_admission}")
    if gate_enabled is not None:
        lines.append(f"  dispatch_load_gate:\n    enabled: {'true' if gate_enabled else 'false'}")
    if lines:
        (home / "config.yaml").write_text("kanban:\n" + "\n".join(lines) + "\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("ALLOCATION_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: dict(LOW))
    kb.init_db()
    return home


def _dispatch_three():
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kbc.connect() as conn:
        for title in ("a", "b", "c"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn,
                                max_in_progress=kbd.resolve_max_in_progress(kbd.configured_max_in_progress()))
    return spawns, res


@pytest.mark.real_memory_guard
def test_fleet_admission_neither_caps_nor_holds_on_a_low_memory_linux_sample(
    tmp_path, monkeypatch, all_assignees_spawnable,
):
    _home(tmp_path, monkeypatch, "utilization")
    assert kbd.resolve_max_in_progress(None) is None
    assert kbd._memory_pressure_level() == "unknown"
    spawns, res = _dispatch_three()
    assert len(spawns) == 3, res
    assert res.memory_pressure is None


@pytest.mark.real_memory_guard
@pytest.mark.parametrize("admission", [None, "legacy"])
def test_without_fleet_admission_upstream_guards_unchanged(
    tmp_path, monkeypatch, all_assignees_spawnable, admission,
):
    _home(tmp_path, monkeypatch, admission)
    assert kbd.resolve_max_in_progress(None) == 2
    assert kbd._memory_pressure_level() == "critical"
    spawns, res = _dispatch_three()
    assert spawns == []
    assert res.memory_pressure == "critical"


@pytest.mark.real_memory_guard
def test_explicit_max_in_progress_still_wins_under_fleet_admission(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, "utilization")
    assert kbd.resolve_max_in_progress(5) == 5


@pytest.mark.real_memory_guard
@pytest.mark.parametrize("fleet,profile", [("legacy", "utilization"), ("utilization", "legacy")])
def test_one_reader_the_fleet_root_mode_decides(tmp_path, monkeypatch, fleet, profile):
    """Round 6 item 11: fleet_admission_active() answers exactly what placement.admission_mode() answers (the
    fleet-root config.yaml that turns the fleet admission path on), whatever the profile config says."""
    from hermes_cli.fleet_placement import placement

    _home(tmp_path, monkeypatch, fleet, profile_admission=profile)
    assert placement.admission_mode() == fleet
    assert kbd.fleet_admission_active() is (fleet == "utilization")


@pytest.mark.real_memory_guard
def test_fleet_admission_with_the_load_gate_off_keeps_the_memory_guards(
    tmp_path, monkeypatch, all_assignees_spawnable,
):
    """Prism 9c506516b019 (Apollo ruling: one guard per start, never zero). The load gate is what replaces the
    memory guards for a local start; with ``kanban.dispatch_load_gate.enabled: false`` it admits everything
    (allowance None), so the memory guards must stay on: no max_in_progress, critical memory = nothing spawns."""
    _home(tmp_path, monkeypatch, "utilization", gate_enabled=False)
    spawns, res = _dispatch_three()
    assert spawns == []
    assert res.memory_pressure == "critical"
    assert kbd.fleet_admission_active() is False
    assert kbd.resolve_max_in_progress(None) == 2


@pytest.mark.real_memory_guard
def test_fleet_admission_with_the_load_gate_on_stands_the_memory_guards_down(
    tmp_path, monkeypatch, all_assignees_spawnable,
):
    """The other half: utilization + gate explicitly enabled = the load gate is the one guard; the memory guards
    stand down (same outcome as the key absent, whose default is on)."""
    _home(tmp_path, monkeypatch, "utilization", gate_enabled=True)
    assert kbd.fleet_admission_active() is True
    assert kbd.resolve_max_in_progress(None) is None
    assert kbd._memory_pressure_level() == "unknown"
    spawns, res = _dispatch_three()
    assert len(spawns) == 3, res
    assert res.memory_pressure is None
