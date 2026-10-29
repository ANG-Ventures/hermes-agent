"""Byte copies of hermes-home ``scripts/lib/{placement,admission,work_estimate,admission_demand}.py`` (t_29403e19).

The fleet has ONE placement scorer and ONE admission predicate; the fork may not import hermes-home code, so it
carries copies. ``VENDORED.json`` pins the source ref and each file's sha256, and
``tests/hermes_cli/test_fleet_placement_vendored.py`` fails when a copy's digest differs from the pin or the pin is
older than ``max_age_days``. Re-vendor by copying the files byte for byte and updating ``VENDORED.json``; never edit
the copies here. Every fork-side difference lives in this adapter.

The one difference: the state root. hermes-home resolves ``PLACEMENT_HOME`` else the checkout two levels above
``scripts/lib``; here the root is the shared kanban root (``<root>/config/fleet-hosts.json``, ``<root>/var/placement``),
resolved at call time so a test's ``HERMES_KANBAN_HOME`` binds it.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import admission, admission_demand, placement, work_estimate

__all__ = ["admission", "admission_demand", "placement", "work_estimate"]


def _fleet_root() -> Path:
    env = os.environ.get("PLACEMENT_HOME")
    if env:
        return Path(env)
    from hermes_cli import kanban_db

    return kanban_db.kanban_home()


def _demand_root(root=None) -> Path:
    return Path(root) if root is not None else placement.fleet_root()


_vendored_unified = admission_demand.unified

# Workloads whose kill switch defaults OFF here (absent = "0" = the caller's old rule). KANBAN_UNIFIED_ADMIT ships
# off until t_c466b7ee fixes the round-8 Prism keys and flips it (t_3694ab3c hard stop); set it to "1" to opt in.
_DEFAULT_OFF = frozenset({"kanban"})


def _unified(workload, env=None):
    env = os.environ if env is None else env
    key = str(workload).upper().replace("-", "_") + "_UNIFIED_ADMIT"
    if workload in _DEFAULT_OFF and key not in env:
        return False
    return _vendored_unified(workload, env)


placement.fleet_root = _fleet_root
admission_demand._root = _demand_root
admission_demand.unified = _unified
