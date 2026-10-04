"""LoadGate two-signal spill band (KWLB PR A, card t_010c27d3).

The PRD 2026-10-03_kanban-worker-load-balancing section 5.2.1 table, row by
row, on the Studio's calibrated band (pause_above 64 / resume_below 48) with
the v0.1 key set (0.80 / 0.70 / 0.65 / 0.80 => spill 51.2 / resume 38.4).
"""

import json
import logging
import math
from pathlib import Path

import pytest

from hermes_cli import kanban_load_gate as klg
from hermes_cli.kanban_load_gate import LoadGate

ARMED = {
    "spill_fraction": 0.80,
    "cpu_spill": 0.70,
    "cpu_spill_resume": 0.65,
    "cpu_pause": 0.80,
}
STUDIO = {"pause_above": 64, "resume_below": 48}
FIXTURE = Path(__file__).parent / "fixtures" / "kanban_load_gate_admit_sample.json"


def _gate(**over):
    return LoadGate({**STUDIO, **ARMED, **over}, ncpu=32)


class _Clock:
    def __init__(self):
        self.now = 0.0

    def tick(self, g, load1, cpu, *, load5=None, running=10, procs=None, limit=None):
        self.now += 60.0
        return g.admit(
            load1, load5=load1 if load5 is None else load5, now=self.now,
            running=running, cpu_busy=cpu, procs=procs, proc_limit=limit,
        )


def _view(g, out):
    return (g.band, out[0], g.remote_allowed, g.spill_reason)


def test_edges_derive_from_spill_fraction():
    g = _gate()
    assert g.spill_armed and g.spill_keys_error is None
    assert g.spill_above == pytest.approx(51.2)
    assert g.spill_resume == pytest.approx(38.4)


@pytest.mark.parametrize(
    "load1, cpu, expected",
    [
        # admitting: both signals under, today's allowance (cap 4).
        (30.0, 0.40, ("admitting", 4, True, None)),
        (50.0, 0.40, ("admitting", 4, True, None)),
        # load edge 51.2: spilling, local against spill_above (0 here).
        (51.2, 0.40, ("spilling", 0, True, "load")),
        # cpu edge 0.70 with load 30: spilling, local sized against cpu_pause
        # (option D): floor((0.80 - 0.70) * 32 / 1.0) = 3.
        (30.0, 0.70, ("spilling", 3, True, "cpu")),
        (30.0, 0.72, ("spilling", 2, True, "cpu")),
        (55.0, 0.72, ("spilling", 0, True, "both")),
        # CPU twin of the hard pause.
        (30.0, 0.85, ("paused", 0, True, "cpu")),
        (55.0, 0.85, ("paused", 0, True, "both")),
        # load1 over 64 with corroborating CPU: today's hard pause.
        (65.0, 0.75, ("paused", 0, True, "load")),
        (65.0, None, ("paused", 0, True, "load")),
        # cpu None: absent leg, load decides.
        (30.0, None, ("admitting", 4, True, None)),
    ],
)
def test_band_table_single_tick(load1, cpu, expected):
    g, c = _gate(), _Clock()
    assert _view(g, c.tick(g, load1, cpu)) == expected


def test_cpu_spill_allowance_uses_cpu_pause_not_cpu_spill():
    # Option D: between 70% and 80% the Studio still admits a few locally.
    g, c = _gate(), _Clock()
    allowance, _ = c.tick(g, 30.0, 0.72)
    assert g.band == "spilling"
    assert allowance == math.floor((0.80 - 0.72) * 32 / klg.DEFAULT_WORKER_CPU_COST)
    assert allowance > 0


def test_cpu_pause_edge_has_no_effect_on_allowance():
    # 0.79 vs 0.80 changes the band label, never a spawn decision: the CPU
    # term is 0 within one worker's CPU cost of cpu_pause.
    assert klg.DEFAULT_WORKER_CPU_COST > 0.01 * 32
    below, at = _gate(), _gate()
    c1, c2 = _Clock(), _Clock()
    a_below, _ = c1.tick(below, 30.0, 0.79)
    a_at, _ = c2.tick(at, 30.0, 0.80)
    assert (below.band, at.band) == ("spilling", "paused")
    assert a_below == a_at == 0


def test_cpu_headroom_row_keeps_todays_allowance():
    inert, armed = LoadGate(STUDIO, ncpu=32), _gate()
    c1, c2 = _Clock(), _Clock()
    today = c1.tick(inert, 66.0, 0.40)
    now = c2.tick(armed, 66.0, 0.40)
    assert inert.state == armed.state == "cpu_headroom"
    assert now == today and now[0] > 0
    assert (armed.band, armed.spill_reason) == ("spilling", "load")


def test_spilling_hysteresis_needs_both_signals_under_to_leave():
    g, c = _gate(), _Clock()
    c.tick(g, 52.0, 0.40)
    assert g.band == "spilling" and g.spill_reason == "load"
    allowance, _ = c.tick(g, 40.0, 0.40)          # 38.4 < 40 < 51.2: stays
    assert g.band == "spilling" and g.spill_reason == "load"
    assert allowance == 4                         # floor((51.2 - 40) / 2) = 5 -> cap
    c.tick(g, 38.0, 0.66)                         # load under, cpu not
    assert g.band == "spilling"
    allowance, reason = c.tick(g, 38.0, 0.60)     # both under: admitting
    assert (g.band, allowance, reason) == ("admitting", 4, None)


def test_spilling_local_never_exceeds_todays_allowance():
    g, inert = _gate(), LoadGate(STUDIO, ncpu=32)
    c1, c2 = _Clock(), _Clock()
    for load1, cpu in [(52, 0.3), (45, 0.3), (40, 0.68), (41, 0.74), (39, 0.5)]:
        armed_out, today_out = c1.tick(g, load1, cpu), c2.tick(inert, load1, cpu)
        assert g.band == "spilling"
        assert armed_out[0] <= today_out[0]


def test_paused_entered_directly_from_admitting_resumes_into_spilling():
    g, c = _gate(), _Clock()
    c.tick(g, 30.0, 0.40)
    assert g.band == "admitting"
    c.tick(g, 70.0, 0.75, load5=70.0)             # one 60 s jump straight to paused
    assert g.band == "paused"
    c.tick(g, 45.0, 0.50, load5=50.0)             # load1 hysteresis clears
    assert g.state != "paused"
    assert g.band == "spilling"


def test_proc_paused_places_nothing_and_still_tracks_the_spill_flag():
    g, c = _gate(), _Clock()
    out = c.tick(g, 55.0, 0.40, procs=9000, limit=10666)
    assert (g.band, out[0], g.remote_allowed, g.spill_reason) == (
        "proc_paused", 0, False, None,
    )
    c.tick(g, 45.0, 0.40, procs=1000, limit=10666)
    assert g.band == "spilling" and g.remote_allowed


def test_load1_unreadable_while_enabled_is_paused_with_remote_allowed(monkeypatch):
    g = _gate()
    assert g.admit(None, now=60.0) == (None, None)
    assert (g.band, g.spill_reason, g.remote_allowed) == ("paused", "unreadable", True)
    g2 = _gate()
    monkeypatch.setattr(klg, "sample_loadavg", lambda: (None, None))
    assert g2.admit_now(running=0) == (None, None)
    assert (g2.band, g2.spill_reason, g2.remote_allowed) == ("paused", "unreadable", True)


def test_unreadable_load1_keeps_a_held_proc_pause(monkeypatch):
    # Prism P1 (PR #1728): an unreadable tick must not lift proc_paused's
    # remote ban; no process check ran to clear it.
    g, c = _gate(), _Clock()
    c.tick(g, 30.0, 0.40, procs=9000, limit=10666)
    assert g.band == "proc_paused" and not g.remote_allowed
    assert g.admit(None, now=c.now + 60) == (None, None)
    assert (g.band, g.remote_allowed) == ("proc_paused", False)
    monkeypatch.setattr(klg, "sample_loadavg", lambda: (None, None))
    assert g.admit_now(running=0) == (None, None)
    assert (g.band, g.remote_allowed) == ("proc_paused", False)
    assert g.snapshot()["pool"] == {"planned": False, "reason": "proc_paused"}
    c.now += 120
    c.tick(g, 30.0, 0.40, procs=1000, limit=10666)   # a real check clears it
    assert g.band != "proc_paused" and g.remote_allowed


def test_cpu_none_never_enters_and_always_satisfies_the_exit():
    g, c = _gate(), _Clock()
    for _ in range(3):
        c.tick(g, 30.0, None)
        assert g.band == "admitting"
    c.tick(g, 52.0, None)
    assert g.band == "spilling"
    allowance, _ = c.tick(g, 46.0, None)          # in the gap: load term only
    assert g.band == "spilling" and allowance == math.floor((51.2 - 46.0) / 2.0)
    c.tick(g, 30.0, None)                          # load under, cpu absent
    assert g.band == "admitting"


def test_inert_keys_never_spill():
    g, c = LoadGate(STUDIO, ncpu=32), _Clock()
    assert not g.spill_armed and g.spill_keys_error is None
    for load1, cpu in [(55, 0.75), (66, 0.40), (30, 0.95), (30, 1.0)]:
        c.tick(g, load1, cpu)
        assert g.band in ("admitting", "paused") and not g.spilling


@pytest.mark.parametrize(
    "keys, rule",
    [
        ({"cpu_pause": 0.80}, "cpu_spill_resume < cpu_spill"),            # partial edit
        ({**ARMED, "cpu_spill": 0.60, "cpu_spill_resume": 0.55}, "cpu_busy_pause"),
        ({**ARMED, "cpu_spill_resume": 0.75}, "cpu_spill_resume < cpu_spill"),
        ({**ARMED, "cpu_pause": 0.65}, "cpu_spill <= cpu_pause"),
        ({**ARMED, "spill_fraction": 1.5}, "outside (0, 1]"),
        ({**ARMED, "spill_fraction": 0}, "outside (0, 1]"),
        ({**ARMED, "cpu_spill": "high"}, "not a number"),
    ],
)
def test_key_set_refused_as_a_whole(keys, rule, caplog):
    with caplog.at_level(logging.WARNING, logger=klg.__name__):
        g = LoadGate({**STUDIO, **keys}, ncpu=32)
    assert rule in (g.spill_keys_error or "")
    assert (g.spill_fraction, g.cpu_spill, g.cpu_spill_resume, g.cpu_pause) == (1.0,) * 4
    assert not g.spill_armed
    assert any("refused as a set" in r.getMessage() for r in caplog.records)
    c = _Clock()
    c.tick(g, 30.0, 0.75)                          # would spill under a valid set
    assert g.band == "admitting"


def test_refused_keys_warn_again_every_five_minutes():
    g = LoadGate({**STUDIO, "cpu_pause": 0.8}, ncpu=32)
    msgs = []

    class _L:
        def warning(self, fmt, *a):
            msgs.append(fmt % a)

        info = error = warning

    for now in (0.0, 60.0, 299.0, 300.0):
        g.admit(20.0, now=now)
        g.log_tick(_L(), now=now)
    assert sum("refused as a set" in m for m in msgs) == 2


def test_state_file_carries_band_fields_and_a_fresh_pool_block(tmp_path):
    g, c = _gate(), _Clock()
    c.tick(g, 52.0, 0.40)
    g.pool = {"planned": True, "planned_at": 1.0, "hosts": {}}
    c.tick(g, 52.0, 0.40)                          # next tick: never republished
    path = tmp_path / "load_gate.json"
    g.write_state(path)
    state = klg.read_state(path)
    assert state["band"] == "spilling" and state["spill_reason"] == "load"
    assert state["spill_above"] == pytest.approx(51.2)
    assert state["spill_resume"] == pytest.approx(38.4)
    assert (state["cpu_spill"], state["cpu_pause"]) == (0.70, 0.80)
    assert state["spill_keys_error"] is None
    assert state["pool"] == {"planned": False, "reason": "not_needed"}
    c.tick(g, 30.0, 0.40, procs=9000, limit=10666)
    assert g.snapshot()["pool"] == {"planned": False, "reason": "proc_paused"}
    assert "band=spilling/load" in klg.format_state_line(state)


def test_keys_absent_admit_outputs_byte_equal_to_recorded_sample():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    n = 0
    for case in data["cases"]:
        g = LoadGate(case["cfg"], case["ncpu"])
        for step in case["steps"]:
            i = step["in"]
            allowance, reason = g.admit(
                i["load1"], load5=i["load5"], now=i["now"], running=i["running"],
                cpu_busy=i["cpu_busy"], procs=i["procs"], proc_limit=i["proc_limit"],
            )
            assert [allowance, reason, g.state] == step["out"], (case["cfg"], i)
            g.record_spawns(step["spawned"], now=i["now"])
            n += 1
    assert n >= 500
    states = {s["out"][2] for c in data["cases"] for s in c["steps"]}
    assert {"admitting", "saturated", "paused", "cpu_headroom", "proc_paused"} <= states
