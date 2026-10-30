"""kanban dispatch_load_gate CPU defaults come from the fleet CPU-line registry (Ace 2026-10-26 04:40)."""
import hashlib
import importlib
import json

import pytest

from hermes_cli import load_lines

KEYS = ("cpu_busy_pause", "cpu_spill", "spill_fraction", "cpu_spill_resume", "cpu_pause")


def _defaults(monkeypatch, policy):
    monkeypatch.setenv("LOAD_LINES_POLICY", str(policy))
    monkeypatch.setenv("LOAD_LINES_HOST", "hx")
    monkeypatch.setenv("HERMES_HOME", str(policy.parent / "nohome"))
    # The host-synced copy (/usr/local/lib/placement/..., present on ci-box since 10-28) must not leak in.
    monkeypatch.setattr(load_lines, "HOST_POLICY", str(policy.parent / "no-host-policy.json"))
    load_lines._cache.clear()
    from hermes_cli import config_defaults as cd
    return cd._kanban_cpu_lines()


def test_registry_present_sets_the_gate_keys(tmp_path, monkeypatch):
    p = tmp_path / "placement-policy.json"
    p.write_text(json.dumps({"cpu_lines": {"default": {"admit": 0.9, "spill": 0.9, "resume": 0.85, "hot": 0.9},
                                           "hosts": {"hx": {"lanes": {"kanban": {"admit": 0.88, "hot": 0.95}}}}}}))
    got = _defaults(monkeypatch, p)
    assert got == {"cpu_busy_pause": 0.88, "cpu_spill": 0.9, "spill_fraction": 0.9, "cpu_spill_resume": 0.85,
                   "cpu_pause": 0.95}
    from hermes_cli.kanban_load_gate import LoadGate
    g = LoadGate(got, 32)
    assert (g.cpu_busy_pause, g.cpu_spill, g.cpu_spill_resume) == (0.88, 0.9, 0.85) and not g.spill_keys_error


def test_registry_absent_keeps_spill_unarmed_and_admit_at_90(tmp_path, monkeypatch, capsys):
    got = _defaults(monkeypatch, tmp_path / "missing.json")
    assert got == {"cpu_busy_pause": 0.90}
    assert capsys.readouterr().err == ""  # imported by every CLI call: silent


@pytest.mark.parametrize("unified", ["0", "1"])
@pytest.mark.parametrize("registry", ["absent", "unreadable"])
def test_no_registry_same_numbers_unified_on_or_off(tmp_path, monkeypatch, capsys, unified, registry):
    """t_3694ab3c (operator 10-29 03:5x): an absent OR unreadable cpu-lines registry yields today's numbers (spill
    unarmed, admit 0.90) whether KANBAN_UNIFIED_ADMIT is on or off."""
    monkeypatch.setenv("KANBAN_UNIFIED_ADMIT", unified)
    p = tmp_path / "placement-policy.json"
    if registry == "unreadable":
        p.write_text("{not json")
    got = _defaults(monkeypatch, p)
    assert got == {"cpu_busy_pause": 0.90}
    from hermes_cli.kanban_load_gate import LoadGate, unified_admit
    from hermes_cli.fleet_placement import admission_demand

    assert unified_admit() is (unified == "1")
    g = LoadGate(got, 32)
    assert g.cpu_busy_pause == 0.90 and not g.spill_armed and not g.spill_keys_error
    assert capsys.readouterr().err == ""  # config import + gate build stay silent on both arms
    # the unified rule's line with no priority file either: the same 90 % (admission.HOST_LINE); admission_demand
    # says once that it fell back, by design (vendored byte-equal from hermes-home)
    assert admission_demand.load_priority(root=tmp_path / "nohome").line == 0.90


def test_fallback_equals_registry_default():
    assert load_lines.FALLBACK == {"admit": 0.90, "spill": 0.90, "resume": 0.85, "hot": 0.90}
