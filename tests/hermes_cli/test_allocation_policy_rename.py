"""Rename wave 3 (t_7be48220, t_fc257547): hermes_cli.placement_policy -> hermes_cli.allocation_policy and
fleet/placement-policy.json -> fleet/allocation-policy.json. The old module is a counted sys.modules alias; a
fleet dir holding only the old file name still loads, counted. Both counts land in
<kanban home>/var/ci-box/deprecated-name-reads.log, the log t_74858430 reads before it removes the aliases."""
from __future__ import annotations

import importlib
import json
import sys

from hermes_cli import allocation_policy as ap
from hermes_cli import kanban_db as kb

DOC = {"schema": 1, "stale_after_s": 77, "host_preference": {"consumers": {"kanban": "on"},
                                                               "last_resort": ["mac-studio"]}}


def _log_lines():
    p = kb.kanban_home() / "var" / "ci-box" / "deprecated-name-reads.log"
    return p.read_text().splitlines() if p.exists() else []


def test_old_module_is_the_same_object_and_counted():
    sys.modules.pop("hermes_cli.placement_policy", None)
    before = len(_log_lines())
    old = importlib.import_module("hermes_cli.placement_policy")
    assert old is ap
    new_lines = _log_lines()[before:]
    assert len(new_lines) == 1 and " module:hermes_cli.placement_policy pid=" in new_lines[0]


def test_new_file_name_is_read(tmp_path):
    (tmp_path / "allocation-policy.json").write_text(json.dumps(DOC))
    before = len(_log_lines())
    pol = ap.load(tmp_path)
    assert pol.source == "file" and pol.stale_after_s == 77 and pol.is_last_resort("mac-studio")
    assert _log_lines()[before:] == []                      # the new name is never counted


def test_legacy_file_name_is_read_and_counted(tmp_path):
    (tmp_path / "placement-policy.json").write_text(json.dumps(DOC))
    before = len(_log_lines())
    pol = ap.load(tmp_path)
    assert pol.source == "file" and pol.stale_after_s == 77
    lines = _log_lines()[before:]
    assert len(lines) == 1 and f" file:{tmp_path / 'placement-policy.json'} pid=" in lines[0]


def test_new_file_name_wins_over_legacy(tmp_path):
    (tmp_path / "placement-policy.json").write_text(json.dumps({**DOC, "stale_after_s": 1}))
    (tmp_path / "allocation-policy.json").write_text(json.dumps(DOC))
    assert ap.load(tmp_path).stale_after_s == 77


def test_neither_file_is_the_prd_fallback(tmp_path):
    pol = ap.load(tmp_path)
    assert pol.source == "fallback" and pol.stale_after_s == 120


def test_fleet_placement_root_reads_allocation_home_and_counts_the_old_name(tmp_path, monkeypatch):
    """The vendored fleet_placement root: ALLOCATION_HOME wins; the old PLACEMENT_HOME is still read and counted once
    per process under HERMES_HOME (the vendored copy has no get_fleet_home)."""
    from hermes_cli import fleet_placement as fp
    from hermes_cli.fleet_placement import placement

    new, old, vh = tmp_path / "new", tmp_path / "old", tmp_path / "vh"
    vh.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(vh))
    monkeypatch.setattr(placement, "_OLD_ENV_COUNTED", set())
    monkeypatch.setenv("ALLOCATION_HOME", str(new))
    monkeypatch.setenv("PLACEMENT_HOME", str(old))
    assert fp._fleet_root() == new
    monkeypatch.delenv("ALLOCATION_HOME")
    assert fp._fleet_root() == old
    assert fp._fleet_root() == old
    log = (vh / "var" / "ci-box" / "deprecated-name-reads.log").read_text().splitlines()
    assert len(log) == 1 and " env:PLACEMENT_HOME pid=" in log[0]


def test_sandbox_root_ignores_an_exported_allocation_home(tmp_path):
    """Prism 8655eeb51c54: ALLOCATION_HOME wins in fleet_root, so the conftest sandbox must drop an inherited one the
    way it drops PLACEMENT_HOME. Runs one probe under a child pytest whose environment exports it."""
    import os
    import subprocess
    from pathlib import Path

    live = tmp_path / "live-fleet-root"
    repo = Path(__file__).resolve().parents[2]
    env = {**os.environ, "ALLOCATION_HOME": str(live), "SANDBOX_PROBE_LIVE_ROOT": str(live)}
    env.pop("PYTEST_ADDOPTS", None)
    probe = f"{Path(__file__).relative_to(repo)}::_sandbox_probe"
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "python_functions=_sandbox_probe",
                        probe], cwd=repo, env=env, capture_output=True, text=True, timeout=300)
    assert p.returncode == 0 and "1 passed" in p.stdout, p.stdout[-3000:] + p.stderr[-2000:]


def _sandbox_probe():
    import os
    from pathlib import Path

    from hermes_cli import fleet_placement as fp

    live = os.environ["SANDBOX_PROBE_LIVE_ROOT"]
    assert os.environ.get("ALLOCATION_HOME") is None       # never repr os.environ into a CI log
    assert Path(fp.placement.fleet_root()) != Path(live)
    assert Path(fp.admission_demand._root()) != Path(live)
