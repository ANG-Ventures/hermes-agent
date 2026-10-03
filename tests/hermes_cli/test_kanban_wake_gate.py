"""Wake -> notify downgrade under measured contention (t_74bf5296).

Ace 13:40: "Wake is really nice as well, except if we have resource
contention. In that case, we want to turn Wake off." The band is the
dispatcher's (pause_above 64 / resume_below 48 on the Studio), with
hysteresis, and it applies per EVENT, never to the subscription.
"""
from __future__ import annotations

from hermes_cli.kanban_wake_gate import WakeGate, read_state

CFG = {"kanban": {"dispatch_load_gate": {"pause_above": 64, "resume_below": 48}}}


class Load:
    def __init__(self, v):
        self.v = v

    def __call__(self):
        return (self.v, self.v, self.v)


def _gate(load, tmp_path, cfg=CFG, lane=lambda p: None):
    return WakeGate(cfg, ncpu=32, loadavg=load, lane_probe=lane,
                    state_file=tmp_path / "wake_gate.json")


def test_inherits_the_dispatch_band(tmp_path):
    g = _gate(Load(1), tmp_path)
    assert (g.pause_above, g.resume_below) == (64, 48)


def test_downgrade_fires_above_the_gate_and_not_below(tmp_path):
    load = Load(71)
    g = _gate(load, tmp_path)
    assert g.downgrade_reason("apollo", now=0) == "host load 71"
    assert read_state(tmp_path / "wake_gate.json")["mode"] == "notify"
    load.v = 30
    assert g.downgrade_reason("apollo", now=100) is None
    assert read_state(tmp_path / "wake_gate.json")["mode"] == "wake"


def test_below_the_gate_never_downgrades(tmp_path):
    g = _gate(Load(60), tmp_path)
    assert g.downgrade_reason("apollo", now=0) is None


def test_hysteresis_holds_between_resume_and_pause(tmp_path):
    load = Load(70)
    g = _gate(load, tmp_path)
    assert g.downgrade_reason(None, now=0)
    load.v = 55  # under pause_above, over resume_below: still contended
    assert g.downgrade_reason(None, now=20) == "host load 55"
    load.v = 47
    assert g.downgrade_reason(None, now=40) is None


def test_samples_at_most_once_per_window(tmp_path):
    load = Load(70)
    g = _gate(load, tmp_path)
    assert g.downgrade_reason(None, now=0)
    load.v = 1
    assert g.downgrade_reason(None, now=5), "cached sample inside sample_seconds"
    assert g.downgrade_reason(None, now=16) is None


def test_lane_headroom_downgrades_that_profile_only(tmp_path):
    lane = lambda p: "lane claude-apr capped" if p == "athena" else None
    g = _gate(Load(1), tmp_path, lane=lane)
    assert g.downgrade_reason("athena", now=0) == "lane claude-apr capped"
    assert g.downgrade_reason("apollo", now=0) is None


def test_lane_probe_error_fails_open_toward_wake(tmp_path):
    def boom(p):
        raise RuntimeError("relay down")
    g = _gate(Load(1), tmp_path, lane=boom)
    assert g.downgrade_reason("athena", now=0) is None


def test_disabled_gate_never_downgrades(tmp_path):
    cfg = {"kanban": {"dispatch_load_gate": {"pause_above": 64, "resume_below": 48},
                      "wake_load_gate": {"enabled": False}}}
    g = _gate(Load(200), tmp_path, cfg=cfg, lane=lambda p: "capped")
    assert g.downgrade_reason("athena", now=0) is None
