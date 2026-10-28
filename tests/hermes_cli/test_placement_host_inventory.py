"""Vendored placement: discovered host inventory (FWA R2a, card t_e3f34b3e; Ace 2026-10-18 09:48).

``load_hosts()`` takes each host's cores / threads / clock from ``var/placement/host-inventory.json`` (beside the
table: ``<root>/config/fleet-hosts.json`` -> ``<root>/var/placement/host-inventory.json``) first and the table second,
printing a disagreement once. A table with the hand-typed values deleted loads. Mirrors hermes-home
scripts/tests/test_placement_host_inventory.py on the vendored bytes (load_hosts/host_facts sha256 identical).
"""
from __future__ import annotations

import json

from hermes_cli.fleet_placement import placement

DOC = {
    "schema": 1, "default_first": "ci-box", "saturate_to": 1.0,
    "footprints": {"kanban": {"ram_gb": 3, "threads": 1, "scratch_gb": 0, "gpu_mb": 0}},
    "hosts": {
        "ci-box": {"mem_total_gb": 220, "mem_reserve_gb": 26, "cores_reserve": 0, "consumers": {"kanban": {"slots": 24}}},
        "mac-studio": {"mem_total_gb": 512, "mem_reserve_gb": 128, "cores_reserve": 10, "count_cores": "performance",
                       "consumers": {"kanban": {"slots": 2}}},
    },
}
INV = {"ci-box": {"physical_cores": 128, "threads": 256, "max_mhz": 3207},
       "mac-studio": {"physical_cores": 32, "threads": 32, "p_cores": 24, "e_cores": 8}}


def _root(tmp_path, doc=DOC, inv=INV):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "fleet-hosts.json").write_text(json.dumps(doc), encoding="utf-8")
    (tmp_path / "var" / "placement").mkdir(parents=True)
    (tmp_path / "var" / "placement" / "host-inventory.json").write_text(json.dumps({"hosts": inv}), encoding="utf-8")
    return tmp_path / "config" / "fleet-hosts.json"


def test_cores_come_from_the_inventory_beside_the_table(tmp_path, monkeypatch):
    monkeypatch.delenv("PLACEMENT_INVENTORY", raising=False)
    t = placement.load_hosts(_root(tmp_path))
    assert (t.hosts["ci-box"].cores, t.hosts["ci-box"].threads, t.hosts["ci-box"].clock_mhz) == (128, 256, 3207.0)
    assert (t.hosts["mac-studio"].cores, t.hosts["mac-studio"].threads) == (24, 32)


def test_discovered_beats_hand_typed_and_prints_once(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PLACEMENT_INVENTORY", raising=False)
    placement._PRINTED_ONCE.clear()
    doc = json.loads(json.dumps(DOC))
    doc["hosts"]["ci-box"].update(cores=64, threads=128)
    p = _root(tmp_path, doc)
    assert placement.load_hosts(p).hosts["ci-box"].cores == 128
    assert placement.load_hosts(p).hosts["ci-box"].cores == 128
    assert capsys.readouterr().err.count("ci-box.cores discovered 128 != table 64; discovered wins") == 1


def test_host_with_no_discovered_or_typed_cores_is_left_out(tmp_path, monkeypatch):
    monkeypatch.delenv("PLACEMENT_INVENTORY", raising=False)
    t = placement.load_hosts(_root(tmp_path, inv={"ci-box": INV["ci-box"]}))
    assert "mac-studio" not in t.hosts and t.hosts["ci-box"].cores == 128
