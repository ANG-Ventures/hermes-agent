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


def test_candidate_receipt_package_closure(tmp_path):
    """Private candidate experiment, not a shipped pin or a live root proof."""
    import os
    import subprocess
    import sys
    import pytest
    # CI must supply the canonical object explicitly until final merged vendoring. Skip BEFORE
    # any filesystem access: defaulting to ~/.hermes probes the real home, which the
    # autouse home_io_guard refuses.
    source_env = os.environ.get("ADMISSION_SOURCE_REPO")
    if not source_env:
        pytest.skip("candidate-only experiment needs ADMISSION_SOURCE_REPO; final vendor pin is a release gate")
    source = Path(source_env)
    ref = os.environ.get("ADMISSION_CANDIDATE_REF", "e65e27833ef171a37582507ed72bd9ac8bcd3fd1")
    if not (source / ".git").exists():
        pytest.skip(f"ADMISSION_SOURCE_REPO={source} is not a git checkout")
    package = tmp_path / "candidate" / "hermes_cli" / "fleet_placement"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").write_text("")
    (package / "__init__.py").write_bytes((PKG / "__init__.py").read_bytes())
    for name in MANIFEST["files"]:
        blob = subprocess.check_output(["git", "-C", str(source), "show", f"{ref}:scripts/lib/{name}"])
        (package / name).write_bytes(blob)
    root = tmp_path / "fleet"
    receipt = tmp_path / "receipt.json"
    env = {**os.environ, "PYTHONPATH": str(package.parents[1]), "PLACEMENT_HOME": str(root),
           "PLACEMENT_ROOT_RECEIPT": str(receipt)}
    program = r'''import os
from pathlib import Path
from hermes_cli.fleet_placement import placement, admission_demand as dm, alloc_client
root = Path(os.environ["PLACEMENT_HOME"])
receipt = Path(os.environ["PLACEMENT_ROOT_RECEIPT"])
assert dm._root() == placement.fleet_root() == root
try:
    alloc_client.mark_protected("kanban")
except (OSError, ValueError, RuntimeError):
    pass
else:
    raise AssertionError("unprovisioned root accepted")
assert not root.exists()
dm.provision_root(root, anchor=root.parent, receipt=receipt)
alloc_client.mark_protected("kanban")
assert alloc_client._protected("kanban")
rid = placement.reserve("studio", "kanban", placement.Footprint(1, 1), ttl_s=60)
assert rid
assert placement.release(rid)
import json
ready = receipt.read_text()
doc = json.loads(ready)
doc["state"] = "pending"
receipt.write_text(json.dumps(doc))
try:
    alloc_client.mark_protected("kanban")
except (OSError, ValueError, RuntimeError):
    pass
else:
    raise AssertionError("pending receipt accepted")
receipt.write_text(ready)
os.environ["PLACEMENT_ROOT_RECEIPT"] = str(receipt.with_name("missing-receipt"))
try:
    alloc_client.mark_protected("kanban")
except (OSError, ValueError, RuntimeError):
    pass
else:
    raise AssertionError("wrong receipt env accepted")
os.environ["PLACEMENT_ROOT_RECEIPT"] = str(receipt)
root.rename(root.with_name("old-fleet"))
root.mkdir()
try:
    alloc_client.mark_protected("kanban")
except (OSError, ValueError, RuntimeError):
    pass
else:
    raise AssertionError("replaced root accepted")
'''
    subprocess.run([sys.executable, "-c", program], env=env, cwd=tmp_path, check=True)


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


def test_root_receipt_missing_names_the_env_and_the_full_reload_once():
    """t_77aa2e0e: a gateway whose env lacks PLACEMENT_ROOT_RECEIPT gets ONE loud boot line naming the env and the
    full reload; a set value gets none."""
    from hermes_cli.fleet_placement import root_receipt_missing

    line = root_receipt_missing({})
    assert line and "PLACEMENT_ROOT_RECEIPT" in line and "FULL-reload" in line
    assert root_receipt_missing({"PLACEMENT_ROOT_RECEIPT": "  "}) == line
    assert root_receipt_missing({"PLACEMENT_ROOT_RECEIPT": "/x/placement-root.json"}) is None
