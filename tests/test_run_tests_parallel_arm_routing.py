"""Opt-in ARM routing through the same CLI and workflow used by CI."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/run_tests_parallel.py"
ARM = '["ubuntu-24.04-arm"]'
POOL = '["self-hosted","hermes-ci"]'


def generate(monkeypatch, capsys, tmp_path, count=None, scope="full", weights=(0.1, 8, 2, 5)):
    spec = importlib.util.spec_from_file_location("arm_runner", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Deliberately non-index-ordered weights; core must stay off ARM even
    # when it is lighter than every eligible slice.
    files = [ROOT / p for p in (mod._CORE_SMOKE_TESTS[0], "tests/test_a.py", "tests/test_b.py", "tests/test_c.py")]
    monkeypatch.setattr(mod, "_discover_files", lambda roots: files)
    monkeypatch.setattr(mod, "_compute_lpt_slices", lambda *a: [[f] for f in files])
    monkeypatch.setattr(mod, "_load_durations", lambda root: {mod._format_file(f, ROOT): d for f, d in zip(files, weights)})
    args = [str(SCRIPT), "--generate-slices", "4", "--self-hosted-labels", POOL, "--self-hosted-slots", "2", "--test-scope", scope]
    if count is not None:
        args += [f"--arm-hosted-slices={count}"]
    monkeypatch.setattr(sys, "argv", args)
    assert mod.main() == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("count,indices", [("1", [3]), ("2", [3, 4]), ("99", [2, 3, 4])])
def test_lightest_non_core_slices_override_existing_routing(monkeypatch, capsys, tmp_path, count, indices):
    matrix = generate(monkeypatch, capsys, tmp_path, count)
    assert [s["index"] for s in matrix["slice"] if s["runs_on"] == ARM] == indices
    assert matrix["slice"][0]["runs_on"] == POOL


@pytest.mark.parametrize("count", [None, "", "0", "-1", "oops", "1.5"])
def test_disabled_or_invalid_arm_count_preserves_routing(monkeypatch, capsys, tmp_path, count):
    matrix = generate(monkeypatch, capsys, tmp_path, count)
    assert [s["runs_on"] for s in matrix["slice"]] == [POOL, POOL, '["ubuntu-latest"]', '["ubuntu-latest"]']


def test_real_cli_scoped_plugin_preserves_core_smoke():
    plugin = next(p.name for p in sorted((ROOT / "tests/plugins").iterdir()) if p.is_dir() and any(p.glob("test_*.py")))
    proc = subprocess.run([sys.executable, str(SCRIPT), "--generate-slices", "4", "--test-scope", f"plugin:{plugin}", "--arm-hosted-slices", "3", "--self-hosted-labels", POOL], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    assert proc.returncode == 0, proc.stderr
    slices = json.loads(proc.stdout)["slice"]
    assert [(s["name"], s["runs_on"]) for s in slices[:2]] == [(f"plugin {plugin}", ARM), ("core smoke", POOL)]
    # Optional 3rd slice: the plugin's cross-tree consumers (never core smoke).
    assert [s["name"] for s in slices[2:]] in ([], [f"plugin {plugin} dependents"])


@pytest.mark.parametrize("weights,expected", [((0.1, 1, 9, 8), 2), ((0.1, 9, 8, 1), 4), ((0.1, 2, 2, 2), 2)])
def test_selection_tracks_duration_inputs_and_ties(monkeypatch, capsys, tmp_path, weights, expected):
    slices = generate(monkeypatch, capsys, tmp_path, "1", weights=weights)["slice"]
    assert [s["index"] for s in slices if s["runs_on"] == ARM] == [expected]


def test_workflow_binds_arm_variable_and_keeps_e2e_x64():
    jobs = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())["jobs"]
    step = next(s for s in jobs["generate"]["steps"] if s.get("id") == "matrix")
    assert step["env"].get("CI_ARM_HOSTED_SLICES") == "${{ vars.CI_ARM_HOSTED_SLICES }}"
    assert '--arm-hosted-slices="$CI_ARM_HOSTED_SLICES"' in step["run"]
    assert jobs["test"]["runs-on"] == "${{ fromJSON(matrix.slice.runs_on) }}"
    assert "X64" in jobs["e2e"]["runs-on"]
