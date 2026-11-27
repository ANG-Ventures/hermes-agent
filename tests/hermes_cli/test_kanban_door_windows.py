"""Native-Windows door fault state (P4, t_0c5ef02e). Kept in its own module: the OS jobs select whole files by
their platforms() marker, and the round-5 module imports the vendored admission_demand (fcntl/pwd at module top) at
collection time. Nothing here may import admission_demand at module level (Prism 3e3d55eb04a0)."""
import ast
from pathlib import Path

import pytest

from tests.hermes_cli.test_kanban_door_dispatch import fleet  # noqa: F401  (fixture)
from hermes_cli import kanban_priority_faults as faults


@pytest.mark.platforms("windows")
def test_fault_state_operations_work_on_native_windows(fleet):
    """Prism df51bfc576d9. RED on 605a9c86 on Windows: the lazy fcntl import only moved the failure, so latched(),
    hold_reason() and check_registry() with a missing registry raised ModuleNotFoundError inside _state()."""
    fleet.table()
    fleet.token()
    assert faults.check_registry() == "missing"
    assert len(fleet.pages) == 1
    faults.latch("probe")
    assert faults.latched() == "probe"
    assert faults.hold_reason() == "door latched: probe"



def test_this_module_and_the_fault_path_never_import_admission_demand_at_top_level():
    """Static pin: the Windows-selected module and kanban_priority_faults carry no module-level admission_demand import."""
    for rel in ("tests/hermes_cli/test_kanban_door_windows.py", "hermes_cli/kanban_priority_faults.py"):
        tree = ast.parse(Path(rel).read_text(encoding="utf-8"))
        for node in tree.body:
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [f"{node.module}.{a.name}" for a in node.names]
            assert not any("admission_demand" in n for n in names), (rel, names)


@pytest.mark.platforms("posix")
def test_registry_path_matches_the_vendored_library(fleet, monkeypatch):
    """The local copy of the registry location must equal the vendored library's, with and without PLACEMENT_HOME."""
    from hermes_cli.fleet_placement import admission_demand
    assert faults._registry_path() == Path(admission_demand._root()) / admission_demand.PRIORITY_REL
    monkeypatch.delenv("PLACEMENT_HOME", raising=False)
    monkeypatch.delenv("ALLOCATION_HOME", raising=False)
    assert faults._registry_path() == Path(admission_demand._root()) / admission_demand.PRIORITY_REL
