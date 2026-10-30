"""Residue of hermes-agent#2397 (t_509ff5f6), both in the REPORTING path of the ONE admission rule.

  b22e585970ce  placement.band() (utilization basis) named admission.admit()'s verdict while the admitter under
                KANBAN_UNIFIED_ADMIT is may_start() -> admit_class() (fleet line + queued higher-class demand), so a
                host held back for Prism published ``ok``. band == may_start's verdict for one job (hot) and two (warm).
  8397e82995e1  GateTickBuilder.publish_demand() swallowed a failed demand write at DEBUG and returned the count as if
                it had been published. A failed write is now logged (warning) and the next tick writes it again.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import kanban_gate_tick as kgt
from hermes_cli import kanban_db as kb
from hermes_cli.fleet_placement import admission as ad
from hermes_cli.fleet_placement import admission_demand as dmd
from hermes_cli.fleet_placement import placement

NOW = 1792989332.0
PRISM_UNIT = ad.Estimate(cpus=4.0, ram_gb=4.0)
PRIORITY = {"schema": 1, "order": ["main", "prism", "interactive", "kanban", "ci", "test-gate", "batch"],
            "line": 0.90, "holdback_units": 2, "demand_max_age_s": 120}


# ------------------------------------------------------------------- b22e585970ce: band == may_start's verdict

@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "root"
    (r / "fleet").mkdir(parents=True)
    (r / "fleet" / "admission-priority.json").write_text(json.dumps(PRIORITY), encoding="utf-8")
    monkeypatch.setenv("KANBAN_UNIFIED_ADMIT", "1")
    monkeypatch.delenv(placement.BAND_BASIS_ENV, raising=False)
    return r


def _table():
    return placement.HostTable(
        hosts={"mac-studio": placement.HostSpec(name="mac-studio", cores=24, threads=32, mem_total_gb=512.0,
                                                mem_reserve_gb=128.0, cores_reserve=10, ceilings={},
                                                aliases=("studio",), scorer="v2")},
        footprints={"kanban": placement.Footprint(ram_gb=3.0, threads=2.0, scratch_gb=0.0)},
        closed_when={}, path=None)


def _sample(busy):
    return placement.Sample(load1=10.0, load5=10.0, mem_available_gb=329.0, mem_total_gb=512.0, cpu_busy=busy,
                            ncpu=32, procs_running=11, os="darwin", at=NOW - 5)


def _may(table, s, root, n):
    fp = table.footprint("kanban")
    return placement.may_start("mac-studio", "kanban", {"ram_gb": fp.ram_gb * n, "threads": fp.threads * n},
                               samples={"mac-studio": s}, table=table, now=NOW, root=root, leases=[], history=[],
                               admitted=0, exclusions_path=root / "none.json", stale_after_s=60)


def _band(table, s, root):
    spec, fp = table.hosts["mac-studio"], table.footprint("kanban")
    ctx = placement.admit_context("kanban", fp, now=NOW, history=[], leases=[], table=table)
    return placement.band(spec, s, fp, consumer="kanban", closed_when=table.closed_when, mode="utilization",
                          now=NOW, ctx=ctx, root=root)


@pytest.mark.parametrize("busy,queued,one,two", [
    (0.40, 0, True, True), (0.40, 64, True, True), (0.60, 64, False, False),
    (0.53, 64, True, False), (0.80, 0, True, False), (0.95, 0, False, False)])
def test_band_is_may_starts_verdict_for_one_and_two_jobs(root, busy, queued, one, two):
    table = _table()
    if queued:
        dmd.publish("prism", queued, PRISM_UNIT, hosts=["studio"], root=root, now=NOW)
    s = _sample(busy)
    m1, m2 = _may(table, s, root, 1), _may(table, s, root, 2)
    assert (m1[0], m2[0]) == (one, two), (m1, m2)
    b, why = _band(table, s, root)
    assert b == ("hot" if not one else "warm" if not two else "ok"), (b, why, m1, m2)


def test_band_kill_switch_is_admit(root, monkeypatch):
    """The band follows the kanban ADMITTER's rule (Prism 59574d3a240b, 8a9579c42a36). Production shape: with the
    switch at its shipped default (unset = off) the admitter publishes no kanban demand file at all, so a missing or
    stale mode must read as off (admit(), no holdback); only a fresh ``unified: true`` from the admitter is the one
    rule, whatever the reader's own env says."""
    monkeypatch.delenv("KANBAN_UNIFIED_ADMIT", raising=False)
    dmd.publish("prism", 64, PRISM_UNIT, root=root, now=NOW)
    assert _band(_table(), _sample(0.60), root)[0] == "ok"                  # no kanban.json: default off
    dmd.publish("kanban", 0, PRISM_UNIT, root=root, now=NOW, env={"KANBAN_UNIFIED_ADMIT": "1"})
    assert _band(_table(), _sample(0.60), root)[0] == "hot"                 # admitter opted in
    dmd.publish("kanban", 0, PRISM_UNIT, root=root, now=NOW - 3600, env={"KANBAN_UNIFIED_ADMIT": "1"})
    assert _band(_table(), _sample(0.60), root)[0] == "ok"                  # switch flipped off: file aged out


# ------------------------------------------------------------- 8397e82995e1: a failed publish is seen, then retried

@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("PLACEMENT_HOME", raising=False)
    monkeypatch.setenv("KANBAN_UNIFIED_ADMIT", "1")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    (home / "fleet").mkdir()
    with kb.connect_closing() as conn:
        for i in range(2):
            kb.create_task(conn, title=f"c{i}", assignee="alpha")
    return home


def _builder(home):
    gate = SimpleNamespace(band="admitting", spill_reason=None, remote_allowed=True, pool=None, cost=2.0)
    return kgt.GateTickBuilder(gate, fleet_dir=lambda: home / "fleet", kanban_cfg=lambda: {},
                               ledger=lambda boards: {}, connect=lambda board=None: kb.connect())


def test_failed_demand_publish_is_logged_and_retried_next_tick(board, monkeypatch, caplog):
    real = dmd.publish
    calls = []

    def flaky(*a, **kw):
        calls.append(a)
        if len(calls) == 1:
            raise OSError(28, "No space left on device")
        return real(*a, **kw)

    monkeypatch.setattr(dmd, "publish", flaky)
    b = _builder(board)
    path = board / "var" / "placement" / "demand" / "kanban.json"
    with caplog.at_level(logging.WARNING, logger="gateway.kanban_watchers"):
        assert b.publish_demand([{"slug": "default"}]) is None        # not reported as published
    assert not path.exists()
    msgs = [r.getMessage() for r in caplog.records]
    assert any("demand" in m and "failed" in m and "No space left" in m for m in msgs), msgs
    assert b.publish_demand([{"slug": "default"}]) == 2              # next tick: written
    assert json.loads(path.read_text(encoding="utf-8"))["queued"] == 2
    assert len(calls) == 2


def test_unreadable_board_is_logged_not_silently_dropped(board, caplog):
    def connect(board=None):
        if board == "broken":
            raise RuntimeError("database disk image is malformed")
        return kb.connect()

    gate = SimpleNamespace(band="admitting", spill_reason=None, remote_allowed=True, pool=None, cost=2.0)
    b = kgt.GateTickBuilder(gate, fleet_dir=lambda: board / "fleet", kanban_cfg=lambda: {},
                            ledger=lambda boards: {}, connect=connect)
    with caplog.at_level(logging.WARNING, logger="gateway.kanban_watchers"):
        assert b.publish_demand([{"slug": "default"}, {"slug": "broken"}]) == 2
    assert any("broken" in r.getMessage() for r in caplog.records)


def test_quarantined_or_unopenable_board_is_zero_demand_and_not_reopened(board):
    """Corrupt-board guard (operator send-back 17:5x): the demand read never re-opens a board the dispatcher has
    quarantined, or one whose open already raised this tick; with no board readable nothing is published."""
    opened = []

    def connect(board=None):
        opened.append(board)
        if board == "broken":
            raise RuntimeError("file is not a database")
        return kb.connect()

    gate = SimpleNamespace(band="admitting", spill_reason=None, remote_allowed=True, pool=None, cost=2.0)
    b = kgt.GateTickBuilder(gate, fleet_dir=lambda: board / "fleet", kanban_cfg=lambda: {},
                            ledger=lambda boards: {}, connect=connect,
                            skip_board=lambda slug: slug == "quarantined")
    assert b.publish_demand([{"slug": "default"}, {"slug": "quarantined"}]) == 2
    assert "quarantined" not in opened
    opened.clear()
    with pytest.raises(RuntimeError):
        b._tick_connect(board="broken")                                # an earlier probe this tick hit it
    assert b.publish_demand([{"slug": "default"}, {"slug": "broken"}]) == 2
    assert opened == ["broken", "default"]                             # not re-opened by the demand read
    path = board / "var" / "placement" / "demand" / "kanban.json"
    path.unlink()
    assert b.publish_demand([{"slug": "quarantined"}]) is None         # nothing readable: nothing published
    assert not path.exists()
