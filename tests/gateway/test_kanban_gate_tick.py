"""The gateway dispatcher's per-tick gate contract (KWLB v0.1, PRD 5.2.3-5.2.4).

Drives the REAL watcher loop over several boards with a fake ``dispatch_once``
and asserts the tick-level invariants:

* AC-13: the Studio cost model sees LOCAL workers only. ``admit_now`` gets
  ``running_local = total - remote`` from ONE ledger pass (clamped at 0), and
  ``record_spawns`` books ``spawned - placed``.
* AC-14: ONE pool plan per gateway tick (probe calls <= enabled hosts, not
  per board), shared by every board (a slot board 1 took is gone for
  board 2), and boards tick strictly one after another in one thread (I-12).
No real ssh, spawn or load: the probe, the ledger and the gate are stubs.
"""

import asyncio
import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway import kanban_watchers as watchers
from hermes_cli import config, kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_load_gate as klg
from hermes_cli import kanban_worker_pool as kwp

from tests.gateway.test_kanban_dispatcher_standby import Clock, cancel, runner

BOARDS = ("default", "b2", "b3")


def _write_pool(fleet: Path) -> None:
    fleet.mkdir(parents=True, exist_ok=True)
    (fleet / kwp.ROLES_FILE).write_text(json.dumps({"schema": 1, "hosts": {
        h: {"roles": {"kanban-worker": {"slots": 1}}, "state": "active"}
        for h in ("ace-ai", "ace-media")}}))
    (fleet / kwp.SIDECAR_FILE).write_text(json.dumps({
        "schema": 1, "ssh_user": "kanbanw", "priority": ["ci-box", "ace-ai", "ace-media"],
        "profiles": ["alpha"],
        "hosts": {"ci-box": {"enabled": False, "absence": "optional"},
                  "ace-ai": {"enabled": True, "absence": "required"},
                  "ace-media": {"enabled": True, "absence": "optional"}}}))


@pytest.fixture
def tick(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_DISPATCH_IN_GATEWAY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = {"kanban": {"dispatch_interval_seconds": 2, "auto_decompose": False}}
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    monkeypatch.setattr(kbd, "resolve_max_in_progress", lambda value: value)
    kb.init_db()
    _write_pool(kb.kanban_home() / "fleet")

    monkeypatch.setattr(kb, "list_boards", lambda **kw: [{"slug": s} for s in BOARDS])
    monkeypatch.setattr(kb, "connect",
                        lambda board=None, **kw: SimpleNamespace(slug=board, close=lambda: None))
    monkeypatch.setattr(kb, "count_spawnable_demand", lambda conn, **kw: 0)
    monkeypatch.setattr(kbd, "has_spawnable_ready", lambda conn: True)
    monkeypatch.setattr(kbd, "reap_worker_zombies", Mock(return_value=[]))

    st = SimpleNamespace(
        ledger={s: (0, {}) for s in BOARDS}, band="paused", allowance=(0, "paused"),
        admitted=[], booked=[], probes=[], calls=[], plans=[], events=[],
        local_spawns={}, remote_takes={}, probes_at_entry=[],
    )
    monkeypatch.setattr(kb, "count_running_by_placement", lambda boards: dict(st.ledger))

    def probe(h):
        st.probes.append(h.name)
        return (0.5, 16)

    monkeypatch.setattr(kwp, "probe_host", probe)

    def admit_now(self, running=None, **kw):
        st.admitted.append(running)
        self.band, self.spill_reason, self.remote_allowed = st.band, "load", True
        return st.allowance

    monkeypatch.setattr(klg.LoadGate, "admit_now", admit_now)
    real_record = klg.LoadGate.record_spawns

    def record_spawns(self, n, now=None):
        st.booked.append(n)
        return real_record(self, n, now)

    monkeypatch.setattr(klg.LoadGate, "record_spawns", record_spawns)

    def fake_dispatch(conn, *, board=None, spawn_paused=None, spawn_limit=None,
                      spillover=None, **kw):
        st.events.append(("enter", board, threading.get_ident()))
        st.probes_at_entry.append(len(st.probes))
        res = kb.DispatchResult()
        st.calls.append((board, spawn_limit, spawn_paused))
        st.plans.append(spillover)
        for i in range(st.local_spawns.get(board, 0)):
            res.spawned.append((f"{board}-l{i}", "alpha", ""))
        for i in range(st.remote_takes.get(board, 0)):
            host = spillover.take("alpha") if spillover is not None else None
            if host is not None:
                res.spawned.append((f"{board}-r{i}", "alpha", ""))
                res.placed.append((f"{board}-r{i}", host.name))
        st.events.append(("exit", board, threading.get_ident()))
        return res

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch)
    clock = Clock()
    monkeypatch.setattr(watchers, "asyncio", SimpleNamespace(
        sleep=clock.sleep, to_thread=asyncio.to_thread, CancelledError=asyncio.CancelledError,
        create_task=asyncio.create_task, shield=asyncio.shield,
    ))
    st.clock = clock
    return st


async def _run_ticks(st, n=1):
    b = runner()
    task = asyncio.create_task(b._kanban_dispatcher_watcher())
    try:
        for _ in range(40):
            _delay, resume = await st.clock.paused(task)
            resume.set_result(None)
            if len(st.calls) >= n * len(BOARDS):
                break
        # Let the tick that issued the last call finish its booking.
        _delay, resume = await st.clock.paused(task)
        resume.set_result(None)
    finally:
        await cancel(task, b)


@pytest.mark.asyncio
async def test_ac13_cost_model_sees_local_workers_only(tick):
    tick.band, tick.allowance = "spilling", (1, None)
    tick.ledger = {"default": (5, {"ace-ai": 2}), "b2": (1, {}), "b3": (0, {})}
    tick.local_spawns = {"default": 1}
    tick.remote_takes = {"default": 2}
    await _run_ticks(tick)
    assert tick.admitted[0] == 4                     # running_local = 6 - 2
    # One local + two remote spawned on the first tick; only the local is booked.
    first = tick.calls[:3]
    assert [c[0] for c in first] == list(BOARDS)
    assert tick.booked[0] == 1, tick.booked


@pytest.mark.asyncio
async def test_ac13_running_local_is_clamped_at_zero(tick, caplog):
    caplog.set_level(logging.INFO)
    tick.ledger = {"default": (1, {"ace-ai": 3})}
    await _run_ticks(tick)
    assert tick.admitted[0] == 0
    assert any("running_local clamped (1, 3)" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_ac14_one_plan_per_tick_shared_by_every_board(tick):
    tick.remote_takes = {s: 1 for s in BOARDS}
    await _run_ticks(tick)
    first = tick.plans[:3]
    # ONE plan object for all boards; probes <= enabled hosts (ci-box is disabled).
    assert first[0] is not None and all(p is first[0] for p in first)
    assert sorted(tick.probes[:2]) == ["ace-ai", "ace-media"]
    # Two probes before board 1 and none added by boards 2 and 3 of that tick.
    assert tick.probes_at_entry[:3] == [2, 2, 2], tick.probes_at_entry
    # Board 1 took ace-ai, board 2 ace-media, board 3 found the pool full.
    assert first[0].budget == 0
    # Paused tick: every board gets an int local budget of 0, never None.
    assert [c[1] for c in tick.calls[:3]] == [0, 0, 0]


@pytest.mark.asyncio
async def test_ac14_boards_tick_sequentially_in_one_thread(tick):
    tick.remote_takes = {s: 1 for s in BOARDS}
    await _run_ticks(tick)
    ev = tick.events[:6]
    assert [e[0] for e in ev] == ["enter", "exit"] * 3, ev
    assert len({e[2] for e in ev}) == 1, ev


@pytest.mark.asyncio
async def test_pool_disabled_by_config_plans_nothing(tick, monkeypatch):
    cfg = {"kanban": {"dispatch_interval_seconds": 2, "auto_decompose": False,
                      "worker_pool": {"enabled": False}}}
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    await _run_ticks(tick)
    assert tick.plans[:3] == [None, None, None] and tick.probes == []


@pytest.mark.asyncio
async def test_admitting_band_without_a_pin_does_not_probe(tick):
    tick.band, tick.allowance = "admitting", (4, None)
    await _run_ticks(tick)
    assert tick.probes == [] and tick.plans[0] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("supported,limit", [(True, 0), (False, None)])
async def test_unreadable_load_fails_closed_only_where_the_sampler_exists(
        tick, monkeypatch, supported, limit):
    """A host with no getloadavg (native Windows) keeps unrestricted local
    admission; a host whose sampler failed gets a local budget of 0."""
    monkeypatch.setattr(klg, "loadavg_supported", lambda: supported)
    tick.band, tick.allowance = "admitting", (None, None)
    await _run_ticks(tick)
    assert tick.calls[0][1] == limit
