"""Vendored placement: ci-box ``cpu_saturable`` (card t_bfb88c23, Ace 2026-10-18 09:04).

A ``cpu_saturable`` host is never refused on negative cpu headroom: binding ``cpu-saturated``, score = cpu fraction
clamped at -1 (a penalty: it ranks below any host with cpu room). A host without the key keeps ``no_headroom:cpu``.
Mirrors hermes-home scripts/tests/test_placement_cpu_saturable.py on the vendored bytes.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli.fleet_placement import placement

NOW = 10_000.0
DOC = {
    "schema": 1, "default_first": "ci-box", "saturate_to": 1.0,
    "footprints": {"kanban": {"ram_gb": 3, "threads": 1, "scratch_gb": 0, "gpu_mb": 0}},
    "hosts": {
        "ci-box": {"cores": 128, "threads": 256, "mem_total_gb": 220, "mem_reserve_gb": 26, "cores_reserve": 0,
                   "cpu_saturable": True, "consumers": {"kanban": {"slots": 24}}},
        "ace-media": {"cores": 24, "threads": 32, "mem_total_gb": 94, "mem_reserve_gb": 28, "cores_reserve": 4,
                      "consumers": {"kanban": {"slots": 4}}},
    },
}


def _sample(load, mem):
    return placement.Sample(mem_available_gb=mem, load1=load, load5=load, psi_cpu_some10=0.0, psi_mem_some10=0.0,
                            system_pct=1.0, at=NOW)


@pytest.fixture
def table(tmp_path):
    p = tmp_path / "fleet-hosts.json"
    p.write_text(json.dumps(DOC), encoding="utf-8")
    return placement.load_hosts(p)


def test_saturated_ci_box_admits_with_a_penalty(table, tmp_path):
    d = placement.choose(table, "kanban", {"ci-box": _sample(169.0, 150.0)}, candidates=["ci-box"], now=NOW,
                         exclusions_path=tmp_path / "none.json")
    assert d.host == "ci-box"
    assert d.rows[0].binding == "cpu-saturated" and d.rows[0].score == pytest.approx((128 - 169.0 - 1) / 128)


def test_saturated_ci_box_loses_to_a_host_with_room(table, tmp_path):
    d = placement.choose(table, "kanban", {"ci-box": _sample(169.0, 150.0), "ace-media": _sample(6.0, 60.0)},
                         now=NOW, exclusions_path=tmp_path / "none.json")
    assert d.host == "ace-media"


def test_a_host_without_the_key_is_still_refused_on_cpu(table, tmp_path):
    d = placement.choose(table, "kanban", {"ace-media": _sample(30.0, 60.0)}, candidates=["ace-media"], now=NOW,
                         exclusions_path=tmp_path / "none.json")
    assert d.host is None and d.rows[0].reason.startswith("no_headroom:cpu")
