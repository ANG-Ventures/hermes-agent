"""``hermes kanban diagnostics --placement`` (t_fb95fc07; Placement PRD v0.4 §6 Phase 1).

Golden: two warm hosts, the lower-load1 host carries a fresh kanban
reservation, so ``projected()`` (load1 + unrealised share) flips the rung to
the other host. Dropping the projected term picks the wrong host.
Read-only: no ssh probe, no file written.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_placement_diag as kpd
from hermes_cli import kanban_worker_pool as kwp

NOW = 1_791_200_000.0


def _write(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "root"
    _write(r / "fleet" / "fleet-roles.json", {"schema": 1, "hosts": {
        "ace-ai": {"roles": {"kanban-worker": {"slots": 4}}, "state": "active"},
        "ace-media": {"roles": {"kanban-worker": {"slots": 4}}, "state": "active"}}})
    _write(r / "fleet" / "kanban-pool.json", {
        "schema": 1, "ssh_user": "kanbanw", "capacity_pct": 0.8, "priority": ["ace-ai", "ace-media"],
        "profiles": ["alpha"], "hosts": {"ace-ai": {"enabled": True, "absence": "required"},
                                          "ace-media": {"enabled": True, "absence": "optional"}}})
    # ace-ai: two kanban units placed 150 s ago, ramp 600 s -> 0.75 unrealised each.
    _write(r / "var" / "placement" / "host-reservations.kanban.json", {
        "consumer": "kanban", "at": NOW - 10, "ttl_s": 180,
        "hosts": {"ace-ai": {"busy_units": 2, "cpu_est": 2.0, "ramp_s": 600,
                             "placed_at": [NOW - 150, NOW - 150]}}})
    _write(r / "var" / kwp.TARGET_STATE_FILE, {"hosts": {
        "ace-ai": {"at": NOW - 5, "hot": False, "hot_run": 0, "clear_run": 4, "updated": NOW - 5},
        "ace-media": {"at": NOW - 5, "hot": False, "hot_run": 1, "clear_run": 0, "updated": NOW - 5}}})
    return r


def _gate(**hosts):
    return {"updated_at": NOW - 3, "band": "spilling", "cost": 2.0,
            "pool": {"planned": True, "planned_at": NOW - 3, "hosts": hosts}}


def _row(load1, band, free=2):
    return {"slots": 4, "running": 0, "load1": load1, "ncpu": 24, "state": "active", "enabled": True,
            "reachable": True, "band": band, "hot": False, "free": free}


def _snapshot(root: Path) -> dict:
    return {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_golden_warm_rung_is_least_projected_and_read_only(root, monkeypatch):
    def no_ssh(*a, **kw):
        raise AssertionError("diagnostics --placement must not probe a host")

    monkeypatch.setattr(subprocess, "run", no_ssh)
    monkeypatch.setattr(kwp, "probe_host", no_ssh)
    before = _snapshot(root)
    gate = _gate(**{"ace-ai": _row(10.0, "warm"), "ace-media": _row(12.0, "warm")})

    rep = kpd.compute(root, kanban_cfg={}, gate_state=gate, now=NOW, assignee="alpha")

    ai, media = rep["hosts"]["ace-ai"], rep["hosts"]["ace-media"]
    assert ai["reservations"] == [{"consumer": "kanban", "busy_units": 2, "cpu_est": 2.0, "ramp_s": 600.0,
                                   "unrealised": [0.75, 0.75], "pending": 3.0}]
    assert (ai["pending"], ai["projected"]) == (3.0, 13.0)
    assert (media["pending"], media["projected"]) == (0.0, 12.0)
    assert ai["streak"]["clear_run"] == 4 and media["streak"]["hot_run"] == 1
    assert rep["rung"] == {"host": "ace-media", "assignee": "alpha",
                           "why": "warm: least projected() wins (ace-ai=13.00, ace-media=12.00)"}
    text = "\n".join(kpd.format_lines(rep))
    assert "rung for @alpha: ace-media" in text and "projected=13.00" in text
    assert _snapshot(root) == before  # nothing written: no streak advance, no ledger write


def test_ok_band_wins_in_priority_order_and_unplanned_tick_names_reason(root):
    gate = _gate(**{"ace-ai": _row(10.0, "warm"), "ace-media": _row(20.0, "ok")})
    rep = kpd.compute(root, kanban_cfg={}, gate_state=gate, now=NOW, assignee="alpha")
    assert rep["rung"]["host"] == "ace-media" and rep["rung"]["why"].startswith("band ok")

    rep = kpd.compute(root, kanban_cfg={}, gate_state={"pool": {"planned": False, "reason": "not_needed"}},
                      now=NOW, assignee="alpha")
    assert rep["rung"] == {"host": None, "assignee": "alpha",
                           "why": "no pool plan on the last tick (not_needed)"}
    assert rep["hosts"]["ace-ai"]["band"] == kwp.BAND_UNKNOWN and rep["hosts"]["ace-ai"]["projected"] is None


def test_cli_flag_is_wired(root, monkeypatch, capsys):
    from hermes_cli import kanban as kcli
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_load_gate as klg
    import argparse

    from hermes_cli.kanban_parser import build_parser

    monkeypatch.setattr(kb, "kanban_home", lambda: root)
    monkeypatch.setattr(klg, "read_state", lambda *a, **k: _gate(
        **{"ace-ai": _row(10.0, "warm"), "ace-media": _row(12.0, "warm")}))
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {}})
    monkeypatch.setattr(kpd.time, "time", lambda: NOW)
    top = argparse.ArgumentParser()
    build_parser(top.add_subparsers(dest="cmd"))
    args = top.parse_args(["kanban", "diagnostics", "--placement", "--json"])
    assert args.placement is True
    assert kcli._cmd_diagnostics(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rung"]["host"] == "ace-media"
