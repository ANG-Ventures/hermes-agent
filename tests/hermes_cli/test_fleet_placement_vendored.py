"""The vendored fleet placement/admission copies are pinned and do not rot (t_29403e19).

hermes_cli/fleet_placement/ holds byte copies of hermes-home scripts/lib/{placement,admission,work_estimate,
admission_demand}.py. The fork's copies had drifted 215/331 diff lines behind home main because workers hand-ported
hunks into them; the pin makes any edit to a copy red, and the age bound makes a stale pin red (prism-router's
test_vendored_pin_is_not_rotting shape, t_b22747df).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import hermes_cli.fleet_placement as fp

PKG = Path(fp.__file__).resolve().parent
MANIFEST = json.loads((PKG / "VENDORED.json").read_text(encoding="utf-8"))


def test_vendored_copies_match_their_pinned_digest():
    """Red when a copy is edited in the fork (or re-vendored without updating the pin)."""
    assert MANIFEST["source_repo"] == "ANG-Ventures/hermes-home"
    assert len(MANIFEST["source_ref"]) == 40
    drifted = {name: hashlib.sha256((PKG / name).read_bytes()).hexdigest()
               for name, entry in MANIFEST["files"].items()
               if hashlib.sha256((PKG / name).read_bytes()).hexdigest() != entry["sha256"]}
    assert not drifted, f"vendored copies differ from VENDORED.json (re-vendor byte-equal, never edit): {drifted}"


def test_every_vendored_module_is_pinned():
    """A module added under fleet_placement/ is either the adapter or a pinned copy."""
    modules = {p.name for p in PKG.glob("*.py")} - {"__init__.py"}
    assert modules == set(MANIFEST["files"])


def test_vendored_pin_is_not_rotting():
    """Red once vendored_at is older than max_age_days: re-vendor from hermes-home main."""
    age = (dt.date.today() - dt.date.fromisoformat(MANIFEST["vendored_at"])).days
    assert age <= MANIFEST["max_age_days"], (
        f"vendored placement/admission is {age} days old (max {MANIFEST['max_age_days']}): "
        "re-vendor from hermes-home main and update VENDORED.json")


def test_adapter_binds_the_state_root_to_the_kanban_root(tmp_path, monkeypatch):
    """The one fork-side difference: no PLACEMENT_HOME -> the shared kanban root, for placement AND demand."""
    from hermes_cli import kanban_db

    monkeypatch.delenv("PLACEMENT_HOME", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    root = kanban_db.kanban_home()
    assert root != Path(fp.placement.__file__).resolve().parents[2]   # not hermes-home's checkout default
    assert fp.placement.fleet_root() == root
    assert fp.admission_demand._root() == root
    monkeypatch.setenv("PLACEMENT_HOME", str(tmp_path / "p"))
    assert fp.admission_demand._root() == tmp_path / "p"
