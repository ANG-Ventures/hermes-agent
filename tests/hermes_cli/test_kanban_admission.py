"""Saturation spec §4.2 row 1: kanban admission on the TARGET host's memory +
published band, behind kanban.placement.admit_on_memory (default off, D1).

Card t_31350981. The sampler's own verdict (``band.kanban``) and
``mem_available_gb`` are fed through the real probe parser and planner; no
test opens ssh or spawns a worker.
"""

from __future__ import annotations

import json
import logging

import pytest

from gateway import kanban_gate_tick as kgt
from hermes_cli import kanban_admission as kadm
from hermes_cli import kanban_worker_pool as kwp
from hermes_cli.config_defaults import DEFAULT_CONFIG

from tests.hermes_cli.test_kanban_target_pressure import (  # noqa: F401  (fixtures)
    HOSTS, POLICY, T0, _card, _dispatch, _ssh, board, sig,
)


def _doc(host, at, *, mem=None, band="ok", reason=None):
    d = {"host": host, "at": at, "ncpu": 24, "load1": 2.4, "load_ratio": 0.1,
         "band": {"kanban": band}, "band_reason": {"kanban": reason or f"{band} reason"}}
    if mem is not None:
        d["mem_available_gb"] = mem
    return json.dumps(d)


class Fleet:
    def __init__(self):
        self.now = {h.name: T0 for h in HOSTS}
        self.load1 = {h.name: 2.0 for h in HOSTS}
        self.text = {h.name: _doc(h.name, T0, mem=100) for h in HOSTS}

    def set(self, host, **kw):
        self.now[host] += 30.0
        self.text[host] = _doc(host, self.now[host], **kw)

    def probe(self, h):
        return kwp.HostSample(self.load1[h.name], 24, self.now[h.name], self.text[h.name])


# -- the cap -----------------------------------------------------------------

def test_cap_is_floor_of_mem_over_reservation():
    assert kadm.cap(_doc("a", T0, mem=19.9), reservation=6)[0] == 3
    assert kadm.cap(_doc("a", T0, mem=5.9), reservation=6)[0] == 0
    assert kadm.cap(_doc("a", T0, mem=60), reservation=6)[0] == 10


def test_hot_band_caps_at_zero_and_both_axes_are_named():
    limit, why = kadm.cap(_doc("a", T0, mem=4, band="hot", reason="load_cores 9.6 >= 1.5"),
                          reservation=6)
    assert limit == 0
    assert "mem_available 4 GB / 6 GB reservation = 0" in why
    assert "band.kanban hot (load_cores 9.6 >= 1.5)" in why


def test_warm_band_is_not_saturated():
    assert kadm.cap(_doc("a", T0, mem=60, band="warm"), reservation=6)[0] == 10


@pytest.mark.parametrize("text", ["", "{nope", "[]", json.dumps({"at": T0})])
def test_no_field_no_cap(text):
    """Absent axes cap nothing; staleness/unreadable is the planner's UNKNOWN."""
    assert kadm.cap(text, reservation=6) == (None, "")


def test_config_defaults_are_off_and_six():
    kcfg = DEFAULT_CONFIG["kanban"]
    assert kadm.enabled(kcfg) is False
    assert kadm.reservation_gb(kcfg) == kadm.DEFAULT_RESERVATION_GB == 6.0
    assert kadm.enabled({}) is False
    assert kadm.enabled({"placement": {"admit_on_memory": True}}) is True
    assert kadm.enabled({"placement": {"admit_on_memory": "yes"}}) is False
    assert kadm.reservation_gb({"placement": {"worker_reservation_gb": 8}}) == 8.0
    assert kadm.reservation_gb({"placement": {"worker_reservation_gb": 0}}) == 6.0


# -- the planner -------------------------------------------------------------
# These arms pin today's enforcing cap, which is the KANBAN_UNIFIED_ADMIT=0 rule since t_3694ab3c (D9: under the
# unified rule admit_class owns the memory axis; tests/hermes_cli/test_kanban_unified_admit.py covers that arm).

@pytest.fixture(autouse=True)
def _legacy_admission_rule(monkeypatch):
    monkeypatch.setenv("KANBAN_UNIFIED_ADMIT", "0")


def _plan(fleet, sig, *, on):
    sig.admit_on_memory = on
    return kwp.plan(HOSTS, {}, probe=fleet.probe, signal=sig)


def test_flag_off_changes_no_slot_but_records_the_would_refuse(sig):
    fleet = Fleet()
    fleet.set("ace-ai", mem=10)
    p = _plan(fleet, sig, on=False)
    assert p.slots == {"ace-ai": 4, "ace-media": 4}
    assert set(p.admission) == {"ace-ai"}
    assert p.admission["ace-ai"].startswith("cap 1 < free 4: mem_available 10 GB")
    assert "pressure" not in p.detail["ace-ai"]


def test_flag_on_cuts_free_slots_to_the_cap(sig):
    fleet = Fleet()
    fleet.set("ace-ai", mem=13)
    p = _plan(fleet, sig, on=True)
    assert p.slots == {"ace-ai": 2, "ace-media": 4}
    assert "pressure" not in p.detail["ace-ai"]  # still admits; not a refusal


def test_flag_on_zero_cap_is_a_named_refusal(sig):
    fleet = Fleet()
    fleet.set("ace-ai", mem=19, band="hot", reason="psi_mem 12 >= 10")
    fleet.set("ace-media", mem=3)
    p = _plan(fleet, sig, on=True)
    assert p.slots == {"ace-ai": 0, "ace-media": 0}
    assert p.detail["ace-ai"]["pressure"] == (
        "admission: mem_available 19 GB / 6 GB reservation = 3; band.kanban hot (psi_mem 12 >= 10)")
    assert p.full_reason().startswith("pool_unavailable (ace-ai: admission:")
    assert "ace-media: admission: mem_available 3 GB" in p.full_reason()


def test_plenty_of_memory_and_ok_band_records_nothing(sig):
    p = _plan(Fleet(), sig, on=True)
    assert p.slots == {"ace-ai": 4, "ace-media": 4} and p.admission == {}


def test_running_workers_reduce_free_before_the_cap(sig):
    """A cap at or above what is free is not a refusal (no noise line)."""
    fleet = Fleet()
    fleet.set("ace-ai", mem=13)  # cap 2
    sig.admit_on_memory = True
    p = kwp.plan(HOSTS, {"ace-ai": 2}, probe=fleet.probe, signal=sig)
    assert p.slots["ace-ai"] == 2 and "ace-ai" not in p.admission


# -- gate-seam E2E: real builder, real dispatcher ----------------------------

class _SshFleet(Fleet):
    pass


def _builder(home, fleet, cfg):
    gate = type("G", (), {"band": "spilling", "spill_reason": "load", "remote_allowed": True,
                          "pool": None, "cost": 2.0})()
    return kgt.GateTickBuilder(
        gate, fleet_dir=lambda: home / "fleet", kanban_cfg=lambda: cfg,
        ledger=lambda boards: {}, connect=lambda board=None: None,
        probe=lambda h: kwp.probe_host(h, runner=_ssh(fleet), pressure_path=POLICY.pressure_path))


@pytest.mark.parametrize("on, want_host, verb", [
    (False, "ace-ai", "would-refuse 1"),
    (True, "ace-media", "refused 1"),
])
def test_e2e_low_memory_host(board, caplog, on, want_host, verb):
    caplog.set_level(logging.INFO)
    fleet = _SshFleet()
    fleet.set("ace-ai", mem=4)
    fleet.set("ace-media", mem=100)
    cfg = {"placement": {"admit_on_memory": on}}
    plan = _builder(board, fleet, cfg).plan_pool([], {})
    tid = _card()
    _res, spawned = _dispatch(plan)
    assert spawned == [(tid, want_host)]
    lines = [m for m in caplog.messages if m.startswith("kanban admission")]
    mode = "enforce" if on else "dry"
    assert lines == [f"kanban admission ({mode}, reservation 6 GB): {verb} "
                     f"(ace-ai: cap 0 < free 4: mem_available 4 GB / 6 GB reservation = 0)"]
