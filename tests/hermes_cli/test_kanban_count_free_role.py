"""kanban-worker is a count-free role (t_c834e0d8, bug class A: admission is measured
use, never a count).

fleet-roles.json ``"kanban-worker": {}`` parses to ``PoolHost.slots = None``. The
planner then never bounds that host by a count: running workers are not subtracted,
and measured admission (allocd per take, the band and the memory cap per plan, the
v0.1 load line) alone refuses. A legacy ``{"slots": N}`` is still a ceiling.
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_headroom as kh
from hermes_cli import kanban_worker_pool as kwp

from tests.hermes_cli.test_kanban_admission import Fleet as MemFleet
from tests.hermes_cli.test_kanban_target_pressure import HOSTS, T0, sig  # noqa: F401  (fixture)

AI = HOSTS[0]
FREE_AI = dataclasses.replace(AI, slots=None)
OLD_COUNT = AI.slots  # 4


def _roles(spec):
    return {"schema": 1, "hosts": {"a": {"roles": {"kanban-worker": spec}, "state": "active"}}}


# -- parse / read ------------------------------------------------------------

def test_kanban_worker_empty_spec_parses_to_none_and_legacy_count_is_kept():
    assert kwp.parse_roles(_roles({})) == {"a": ({"kanban-worker": None}, "active")}
    assert kwp.parse_roles(_roles({"slots": 3})) == {"a": ({"kanban-worker": 3}, "active")}
    with pytest.raises(kwp.PoolError, match=r'kanban-worker must be \{\} or \{"slots": N\}'):
        kwp.parse_roles(_roles({"slots": 3, "x": 1}))
    with pytest.raises(kwp.PoolError, match="slots must be 1..64"):
        kwp.parse_roles(_roles({"slots": 0}))


def test_read_pool_carries_a_count_free_host(tmp_path):
    (tmp_path / kwp.ROLES_FILE).write_text(json.dumps({"schema": 1, "hosts": {
        "ace-ai": {"roles": {"kanban-worker": {}, "ci-runner": {}}, "state": "active"},
        "ace-media": {"roles": {"kanban-worker": {"slots": 4}}, "state": "active"},
    }}), encoding="utf-8")
    (tmp_path / kwp.SIDECAR_FILE).write_text(json.dumps({
        "schema": 1, "ssh_user": "kanbanw", "priority": ["ace-ai", "ace-media"], "profiles": ["alpha"],
        "hosts": {"ace-ai": {"enabled": True, "absence": "required"},
                  "ace-media": {"enabled": True, "absence": "optional"}},
    }), encoding="utf-8")
    cfg = kwp.read_pool(tmp_path, kanban_cfg={})
    assert cfg.refused is None and cfg.warnings == ()
    assert {h.name: h.slots for h in cfg.pool_hosts} == {"ace-ai": None, "ace-media": 4}


# -- v0.1 planner: the load line decides --------------------------------------

def test_v01_count_free_host_admits_past_the_old_count_until_the_load_line():
    running = {"ace-ai": OLD_COUNT + 6}
    legacy = kwp.plan([AI], running, probe=lambda h: (1.0, 24))
    assert legacy.budget == 0  # the count refuses
    free = kwp.plan([FREE_AI], running, probe=lambda h: (1.0, 24))
    taken = [free.take("alpha") for _ in range(OLD_COUNT + 2)]
    assert all(h is not None and h.name == "ace-ai" for h in taken)
    hot = kwp.plan([FREE_AI], running, probe=lambda h: (24 * 0.8, 24))
    assert hot.budget == 0 and hot.take("alpha") is None and hot.refusal == "pool_full"


# -- signal planner: band + memory cap decide ----------------------------------

def test_memory_cap_alone_bounds_a_count_free_host(sig):
    fleet = MemFleet()
    fleet.set("ace-ai", mem=60)  # cap floor(60 / 6) = 10 > the old count 4
    sig.admit_on_memory = True
    running = {"ace-ai": OLD_COUNT}
    assert kwp.plan([AI], running, probe=fleet.probe, signal=sig).budget == 0  # count refuses
    p = kwp.plan([FREE_AI], running, probe=fleet.probe, signal=sig)
    assert p.slots == {"ace-ai": 10} and p.detail["ace-ai"]["slots"] is None
    got = [p.take("alpha") for _ in range(10)]
    assert all(h is not None for h in got)
    assert p.take("alpha") is None and p.refusal == "pool_full"


def test_hot_band_refuses_a_count_free_host(sig):
    fleet = MemFleet()
    fleet.set("ace-ai", mem=100, band="hot", reason="psi_mem 12 >= 10")
    sig.admit_on_memory = True
    p = kwp.plan([FREE_AI], {}, probe=fleet.probe, signal=sig)
    assert p.slots == {"ace-ai": 0}
    assert p.take("alpha") is None and p.refusal.startswith("pool_unavailable (ace-ai: admission:")


# -- allocd on: allocd alone decides -------------------------------------------

def _allocd_plan(sig, host, running):
    sig.placement_table = object()
    sig.allocd = kh.ALLOCD_ON
    detail = {host.name: {"slots": host.slots, "running": running, "load1": 1.0, "ncpu": 24}}
    return kwp._finish_plan([host], {host.name: kwp.count_free(host, running)}, detail, (), T0, None, sig)


def test_allocd_admits_a_count_free_host_past_the_old_count_until_it_refuses(sig, monkeypatch):
    admitted = []

    def fake_admit(plan, cands, job, job_id):
        if len(admitted) >= OLD_COUNT + 3:
            raise RuntimeError("no_headroom: mem_available")
        admitted.append(cands[0])
        return SimpleNamespace(host=cands[0], reservation_id=f"r{len(admitted)}", reason=None, rule="admit")

    monkeypatch.setattr(kh, "allocd_admit", fake_admit)
    monkeypatch.setattr(kh, "pool_key", lambda table, cands, h: h if h in cands else None)
    running = OLD_COUNT + 2  # past the old count already
    legacy = _allocd_plan(sig, AI, running)
    assert kh.structural_free(legacy, "ace-ai") == 0
    assert legacy.take("alpha") is None and legacy.refusal == "pool_full" and admitted == []

    p = _allocd_plan(sig, FREE_AI, running)
    assert kh.structural_free(p, "ace-ai") == kwp.UNCOUNTED_TICK_FREE
    got = [p.take("alpha") for _ in range(OLD_COUNT + 3)]
    assert [h.name for h in got] == ["ace-ai"] * (OLD_COUNT + 3)
    assert p.take("alpha") is None
    assert p.refusal.startswith("pool_unavailable (allocd RuntimeError: no_headroom")
    assert kh.structural_free(p, "ace-ai") == kwp.UNCOUNTED_TICK_FREE - (OLD_COUNT + 3)


def test_count_free_and_legacy_free_counts():
    assert kwp.count_free(FREE_AI, 1000) == kwp.UNCOUNTED_TICK_FREE
    assert kwp.count_free(AI, 1) == OLD_COUNT - 1
    assert kwp.count_free(AI, OLD_COUNT + 5) == 0
