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


# ── arm-first with the CI_X64_HOSTED_MIN floor (spec L5) ─────────────────
HOSTED = '["ubuntu-latest"]'
# core smoke first (lightest), then 7 non-core slices with distinct weights.
WEIGHTS8 = (0.1, 40, 10, 30, 5, 20, 1, 15)


def generate_floor(monkeypatch, capsys, slots, arm="3", x64_min="2", weights=WEIGHTS8):
    spec = importlib.util.spec_from_file_location("arm_runner_floor", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    files = [ROOT / mod._CORE_SMOKE_TESTS[0]] + [ROOT / f"tests/test_f{i}.py" for i in range(1, len(weights))]
    monkeypatch.setattr(mod, "_discover_files", lambda roots: files)
    monkeypatch.setattr(mod, "_compute_lpt_slices", lambda *a: [[f] for f in files])
    monkeypatch.setattr(mod, "_load_durations", lambda root: {mod._format_file(f, ROOT): d for f, d in zip(files, weights)})
    args = [str(SCRIPT), "--generate-slices", str(len(files)), "--self-hosted-labels", POOL,
            "--self-hosted-slots", str(slots), f"--arm-hosted-slices={arm}"]
    if x64_min is not None:
        args.append(f"--x64-hosted-min={x64_min}")
    monkeypatch.setattr(sys, "argv", args)
    assert mod.main() == 0
    return [s["runs_on"] for s in json.loads(capsys.readouterr().out)["slice"]]


@pytest.mark.parametrize("slots", range(0, 9))
def test_x64_floor_survives_every_self_hosted_cap(monkeypatch, capsys, slots):
    runs_on = generate_floor(monkeypatch, capsys, slots)
    hosted_total = len(runs_on) - slots
    # The controller-owned pool head is never moved.
    assert runs_on[:slots] == [POOL] * slots
    # Hosted x64 canaries = min(floor, hosted slices); everything else hosted is ARM.
    assert runs_on.count(HOSTED) == min(2, hosted_total)
    assert runs_on.count(ARM) == max(0, hosted_total - 2)
    assert set(runs_on) <= {POOL, HOSTED, ARM}


def test_heaviest_hosted_go_arm_lightest_stay_x64(monkeypatch, capsys):
    # slots=2: pool = core(0.1) + 40; hosted = 10,30,5,20,1,15 -> x64 keeps 1 and 5.
    runs_on = generate_floor(monkeypatch, capsys, 2)
    assert runs_on == [POOL, POOL, ARM, ARM, HOSTED, ARM, HOSTED, ARM]


def test_hosted_core_slice_counts_as_x64_canary_and_never_goes_arm(monkeypatch, capsys):
    # slots=0: core is hosted, stays x64 and fills one floor seat; lightest non-core (1) fills the other.
    runs_on = generate_floor(monkeypatch, capsys, 0)
    assert runs_on == [HOSTED, ARM, ARM, ARM, ARM, ARM, HOSTED, ARM]


def test_floor_of_zero_sends_every_hosted_non_core_slice_to_arm(monkeypatch, capsys):
    runs_on = generate_floor(monkeypatch, capsys, 0, x64_min="0")
    assert runs_on == [HOSTED] + [ARM] * 7


@pytest.mark.parametrize("arm", ["0", "", "-1", "oops"])
def test_arm_kill_switch_wins_over_the_floor(monkeypatch, capsys, arm):
    runs_on = generate_floor(monkeypatch, capsys, 2, arm=arm)
    assert runs_on == [POOL, POOL] + [HOSTED] * 6


@pytest.mark.parametrize("x64_min", ["off", "-1", ""])
def test_invalid_floor_falls_back_to_the_legacy_trial(monkeypatch, capsys, x64_min):
    legacy = generate_floor(monkeypatch, capsys, 2, x64_min=None)
    assert generate_floor(monkeypatch, capsys, 2, x64_min=x64_min) == legacy
    # Legacy = the 3 lightest non-core slices from either venue (1, 5, 10).
    assert legacy == [POOL, POOL, ARM, HOSTED, ARM, HOSTED, ARM, HOSTED]


def test_workflow_binds_x64_floor_with_default_two():
    jobs = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())["jobs"]
    step = next(s for s in jobs["generate"]["steps"] if s.get("id") == "matrix")
    assert step["env"].get("CI_X64_HOSTED_MIN") == "${{ vars.CI_X64_HOSTED_MIN || '2' }}"
    assert '--x64-hosted-min="$CI_X64_HOSTED_MIN"' in step["run"]
