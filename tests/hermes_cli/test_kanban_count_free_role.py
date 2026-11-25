"""kanban-worker is a count-free role (t_c834e0d8, bug class A: admission is measured
use, never a count).

fleet-roles.json ``"kanban-worker": {}`` parses to ``PoolHost.slots = None``. The
planner then never bounds that host by a count: running workers are not subtracted,
and measured admission (allocd per take, the band per plan, the v0.1 load line) alone
refuses. A legacy ``{"slots": N}`` is still a ceiling. The per-tick stand-in is the
fleet runaway stop, 1000 (t_b20850d2): it never binds and the plan budget is never a slot sum.
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_headroom as kh
from hermes_cli import kanban_worker_pool as kwp

from tests.hermes_cli.test_kanban_target_pressure import HOSTS, T0, sig  # noqa: F401  (fixture)

AI = HOSTS[0]
FREE_AI = dataclasses.replace(AI, slots=None)
OLD_COUNT = AI.slots  # 4


def _roles(spec):
    return {"schema": 1, "hosts": {"a": {"roles": {"kanban-worker": spec}, "state": "active"}}}


# -- parse / read ------------------------------------------------------------

def test_kanban_worker_count_is_never_read_as_capacity():
    """t_b20850d2 (FWA-H, red on base: a legacy {"slots": 12} parsed to 12 and capped the plan at the slot sum).
    The dispatcher reads kanban-worker as membership only, ``{}`` or a legacy count alike; the shape is still
    validated so a malformed file is refused, and a non-kanban role keeps its count."""
    assert kwp.parse_roles(_roles({})) == {"a": ({"kanban-worker": None}, "active")}
    assert kwp.parse_roles(_roles({"slots": 3})) == {"a": ({"kanban-worker": None}, "active")}
    with pytest.raises(kwp.PoolError, match=r'kanban-worker must be \{\} or \{"slots": N\}'):
        kwp.parse_roles(_roles({"slots": 3, "x": 1}))
    with pytest.raises(kwp.PoolError, match="slots must be 1..64"):
        kwp.parse_roles(_roles({"slots": 0}))
    other = {"schema": 1, "hosts": {"a": {"roles": {"review-worker": {"slots": 5}}, "state": "active"}}}
    assert kwp.parse_roles(other) == {"a": ({"review-worker": 5}, "active")}


def test_count_free_review_worker_row_does_not_refuse_the_pool(tmp_path):
    """hermes-home #9338 wrote ``"review-worker": {}`` on every Prism member; the
    parser refused the whole pool (``refused:roles``), so nothing spilled (t_e85e77b7)."""
    (tmp_path / kwp.ROLES_FILE).write_text(json.dumps({"schema": 1, "hosts": {
        "ace-media": {"roles": {"review-worker": {}, "kanban-worker": {"slots": 12}, "ci-runner": {}},
                      "state": "active"},
        "nas-fleet": {"roles": {"review-worker": {}}, "state": "active"},
    }}), encoding="utf-8")
    (tmp_path / kwp.SIDECAR_FILE).write_text(json.dumps({
        "schema": 1, "ssh_user": "ace", "ssh_identity": "/k/worker", "priority": ["ace-media"], "profiles": ["alpha"],
        "hosts": {"ace-media": {"enabled": True, "absence": "optional"}},
    }), encoding="utf-8")
    cfg = kwp.read_pool(tmp_path, kanban_cfg={})
    assert cfg.refused is None, cfg.warnings
    assert [h.name for h in cfg.pool_hosts] == ["ace-media"]


def test_live_slot_counts_do_not_shape_the_pool_budget(tmp_path):
    """The live fleet-roles shape (kanban-worker 12 / 12 / 24): every host plans the runaway stop, never 12."""
    (tmp_path / kwp.ROLES_FILE).write_text(json.dumps({"schema": 1, "hosts": {
        "ace-ai": {"roles": {"kanban-worker": {"slots": 12}, "ci-runner": {}}, "state": "active"},
        "ace-media": {"roles": {"kanban-worker": {"slots": 12}}, "state": "active"},
    }}), encoding="utf-8")
    (tmp_path / kwp.SIDECAR_FILE).write_text(json.dumps({
        "schema": 1, "ssh_user": "ace", "ssh_identity": "/k/worker",
        "priority": ["ace-ai", "ace-media"], "profiles": ["alpha"],
        "hosts": {"ace-ai": {"enabled": True, "absence": "required"},
                  "ace-media": {"enabled": True, "absence": "optional"}},
    }), encoding="utf-8")
    cfg = kwp.read_pool(tmp_path, kanban_cfg={})
    assert {h.name: h.slots for h in cfg.pool_hosts} == {"ace-ai": None, "ace-media": None}
    p = kwp.plan(list(cfg.pool_hosts), {"ace-ai": 5, "ace-media": 0}, probe=lambda h: (1.0, 24))
    assert p.budget == 2 * kwp.UNCOUNTED_TICK_FREE and p.budget != (12 - 5) + 12


def test_read_pool_carries_count_free_hosts(tmp_path):
    (tmp_path / kwp.ROLES_FILE).write_text(json.dumps({"schema": 1, "hosts": {
        "ace-ai": {"roles": {"kanban-worker": {}, "ci-runner": {}}, "state": "active"},
        "ace-media": {"roles": {"kanban-worker": {"slots": 4}}, "state": "active"},
    }}), encoding="utf-8")
    (tmp_path / kwp.SIDECAR_FILE).write_text(json.dumps({
        "schema": 1, "ssh_user": "ace", "ssh_identity": "/k/worker",
        "priority": ["ace-ai", "ace-media"], "profiles": ["alpha"],
        "hosts": {"ace-ai": {"enabled": True, "absence": "required"},
                  "ace-media": {"enabled": True, "absence": "optional"}},
    }), encoding="utf-8")
    cfg = kwp.read_pool(tmp_path, kanban_cfg={})
    assert cfg.refused is None and cfg.warnings == ()
    assert {h.name: h.slots for h in cfg.pool_hosts} == {"ace-ai": None, "ace-media": None}


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


# -- signal planner: no count and no memory cap bound the plan ------------------

def _pressure(at, *, mem=4.0, band="ok"):
    return json.dumps({"host": "ace-ai", "at": at, "ncpu": 24, "load1": 2.4, "load_ratio": 0.1,
                       "mem_available_gb": mem, "band": {"kanban": band}, "band_reason": {"kanban": band}})


def test_signal_plan_budget_is_the_stand_in_not_a_slot_sum_or_memory_cap(sig):
    """t_b20850d2 (red on base: the 6 GB/worker cap and the 64 stand-in shaped the plan budget). A count-free
    host with 4 GB available plans the runaway stop; the measured rule (allocd / unified_filter) decides per take."""
    probe = lambda h: kwp.HostSample(2.0, 24, T0, _pressure(T0))  # noqa: E731
    p = kwp.plan([FREE_AI], {"ace-ai": 30}, probe=probe, signal=sig)
    assert p.slots == {"ace-ai": kwp.UNCOUNTED_TICK_FREE} and p.budget == 1000
    assert "admission" not in p.detail["ace-ai"]


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
    # t_2158f095 (one guard per start): an allocd take is not capped by a legacy count either; the plan's own
    # slots stay the real count for every take that does not ask allocd (test_kanban_fwa_no_slot_cap.py).
    assert legacy.slots["ace-ai"] == 0
    assert kh.structural_free(legacy, "ace-ai") == kwp.UNCOUNTED_TICK_FREE

    p = _allocd_plan(sig, FREE_AI, running)
    assert kh.structural_free(p, "ace-ai") == kwp.UNCOUNTED_TICK_FREE
    got = [p.take("alpha") for _ in range(OLD_COUNT + 3)]
    assert [h.name for h in got] == ["ace-ai"] * (OLD_COUNT + 3)
    assert p.take("alpha") is None
    assert p.refusal.startswith("pool_unavailable (allocd RuntimeError: no_headroom")
    assert kh.structural_free(p, "ace-ai") == kwp.UNCOUNTED_TICK_FREE - (OLD_COUNT + 3)


def test_count_free_and_legacy_free_counts():
    assert kwp.UNCOUNTED_TICK_FREE == 1000        # the fleet's runaway stop (no new counts: a survivor is 1000)
    assert kwp.count_free(FREE_AI, 1000) == kwp.UNCOUNTED_TICK_FREE
    assert kwp.count_free(AI, 1) == OLD_COUNT - 1
    assert kwp.count_free(AI, OLD_COUNT + 5) == 0
