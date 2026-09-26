"""Measured per-file timeout budgets (t_bf25c6b2).

A fixed 300 s per-file wall killed files whose cached wall time sat near it
(13 of 14 failed merge_group runs on 2026-09-25, 0 assertion failures). The
budget is now clamp(3 x cached duration, floor, max(floor, 900)), stamped into
the slice matrix by --generate-slices and applied by the runner.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/run_tests_parallel.py"
# Real files so --files resolves; their content never runs (the runner seam
# is replaced below).
HEAVY = "tests/test_run_tests_parallel_timeout_verdict.py"
LIGHT = "tests/test_run_tests_parallel_stdio.py"


def _load():
    spec = importlib.util.spec_from_file_location("budget_runner", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "duration,floor,expected",
    [
        (None, 300.0, 300.0),   # no cache entry -> today's fixed cap
        (0.0, 300.0, 300.0),
        (-5.0, 300.0, 300.0),
        (50.0, 300.0, 300.0),   # 3x below floor -> floor
        (150.0, 300.0, 450.0),  # the edge case that used to be a coin flip
        (240.0, 300.0, 720.0),
        (400.0, 300.0, 900.0),  # capped: a real hang stays bounded
        (400.0, 1200.0, 1200.0),  # an explicit floor above the cap wins
    ],
)
def test_measured_budget(duration, floor, expected):
    mod = _load()
    assert mod._measured_file_timeout(duration, floor) == expected


def test_spec_round_trip_lists_only_raised_budgets():
    mod = _load()
    durations = {HEAVY: 240.0, LIGHT: 12.0}
    spec = mod._file_timeouts_spec([ROOT / HEAVY, ROOT / LIGHT], durations, ROOT)
    assert spec == f"{HEAVY}=720"
    assert mod._parse_file_timeouts(spec) == {HEAVY: 720.0}
    # Malformed entries fall back to the floor instead of crashing the slice.
    assert mod._parse_file_timeouts(f"garbage:{LIGHT}=x:=5:{HEAVY}=0") == {}
    assert mod._parse_file_timeouts("") == {}


def test_generate_slices_stamps_file_timeouts(monkeypatch, capsys):
    mod = _load()
    files = [ROOT / HEAVY, ROOT / LIGHT]
    monkeypatch.setattr(mod, "_discover_files", lambda roots: files)
    monkeypatch.setattr(mod, "_load_durations", lambda root: {HEAVY: 240.0, LIGHT: 12.0})
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--generate-slices", "2"])
    assert mod.main() == 0
    rows = json.loads(capsys.readouterr().out)["slice"]
    by_files = {r["files"]: r["file_timeouts"] for r in rows}
    assert by_files == {HEAVY: f"{HEAVY}=720", LIGHT: ""}


def _run_main(mod, monkeypatch, capsys, extra_args, durations=None):
    seen: dict[str, float] = {}

    def fake_run(file, pytest_args, repo_root, file_timeout, retries=0):
        seen[mod._format_file(file, repo_root)] = file_timeout
        return file, 0, "1 passed in 0.01s", {"passed": 1}, 0.01

    monkeypatch.setattr(mod, "_run_one_file", fake_run)
    monkeypatch.setattr(mod, "_load_durations", lambda root: dict(durations or {}))
    monkeypatch.setattr(mod, "_save_durations", lambda *a, **k: None)
    monkeypatch.setattr(
        sys, "argv",
        [str(SCRIPT), "--files", f"{HEAVY}:{LIGHT}", "--no-strict-noop", *extra_args],
    )
    mod.main()
    out = capsys.readouterr().out
    return seen, out


def test_runner_applies_stamped_budget_per_file(monkeypatch, capsys):
    mod = _load()
    seen, out = _run_main(mod, monkeypatch, capsys, ["--file-timeouts", f"{HEAVY}=720"])
    assert seen == {HEAVY: 720.0, LIGHT: 300.0}
    assert f"{HEAVY}=720s" in out


def test_runner_never_goes_below_floor_or_above_cap(monkeypatch, capsys):
    mod = _load()
    seen, _ = _run_main(
        mod, monkeypatch, capsys,
        ["--file-timeout", "400", "--file-timeouts", f"{HEAVY}=350:{LIGHT}=5000"],
    )
    assert seen == {HEAVY: 400.0, LIGHT: 900.0}


def test_runner_uses_local_cache_when_no_spec(monkeypatch, capsys):
    mod = _load()
    seen, _ = _run_main(mod, monkeypatch, capsys, [], durations={HEAVY: 200.0})
    assert seen == {HEAVY: 600.0, LIGHT: 300.0}


def test_budget_basis_is_p90_of_history_not_the_last_sample():
    """The measured failure: one fast sample (75 s) must not set a 300 s
    budget for a file whose slow runs take 250-300 s."""
    mod = _load()
    history = {HEAVY: [196, 192, 259, 106, 164, 128, 273, 154, 300, 270]}
    durations = {HEAVY: 75.0}
    basis = mod._budget_basis(HEAVY, durations, history)
    assert basis == 273
    assert mod._measured_file_timeout(basis, 300.0) == 819.0
    # Last sample alone (no history) is still used; nothing at all -> None.
    assert mod._budget_basis(HEAVY, durations, {}) == 75.0
    assert mod._budget_basis(LIGHT, {}, {}) is None


def test_generate_stamps_from_history(monkeypatch, capsys):
    mod = _load()
    files = [ROOT / HEAVY, ROOT / LIGHT]
    monkeypatch.setattr(mod, "_discover_files", lambda roots: files)
    monkeypatch.setattr(mod, "_load_durations", lambda root: {HEAVY: 75.0, LIGHT: 12.0})
    monkeypatch.setattr(
        mod, "_load_duration_history", lambda root: {HEAVY: [250.0, 280.0, 75.0]}
    )
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--generate-slices", "2"])
    assert mod.main() == 0
    rows = json.loads(capsys.readouterr().out)["slice"]
    assert {r["files"]: r["file_timeouts"] for r in rows}[HEAVY] == f"{HEAVY}=840"


def test_merge_duration_history_cli_appends_and_trims(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "__file__", str(tmp_path / "scripts" / "run_tests_parallel.py"))
    hist_path = tmp_path / mod._DURATION_HISTORY_FILE
    hist_path.write_text(
        json.dumps({HEAVY: list(range(1, mod._DURATION_HISTORY_KEEP + 1)), "gone.py": [5]}),
        encoding="utf-8",
    )
    new = tmp_path / "test_durations.json"
    new.write_text(json.dumps({HEAVY: 99.5, LIGHT: 3.0, "bad.py": -1}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--merge-duration-history", str(new)])
    assert mod.main() == 0
    merged = json.loads(hist_path.read_text(encoding="utf-8"))
    assert merged[HEAVY][-1] == 99.5
    assert len(merged[HEAVY]) == mod._DURATION_HISTORY_KEEP
    assert merged[HEAVY][0] == 2  # oldest sample dropped
    assert merged[LIGHT] == [3.0]
    assert merged["gone.py"] == [5]  # files not in this run keep their history
    assert "bad.py" not in merged
    # A corrupt history file must not break the merge (starts fresh).
    hist_path.write_text("{not json", encoding="utf-8")
    assert mod.main() == 0
    assert json.loads(hist_path.read_text(encoding="utf-8"))[HEAVY] == [99.5]


def test_history_cache_path_matches_between_restore_and_save():
    """actions/cache versions entries by path: generate's restore must use
    the exact path save-durations saves, or it can never hit."""
    wf = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8"))

    def history_paths(job):
        return {
            s["with"]["path"] for s in wf["jobs"][job]["steps"]
            if "actions/cache" in str(s.get("uses", ""))
            and "history" in str(s.get("with", {}).get("key", ""))
        }

    assert history_paths("generate") == {"test_durations_history.json"}
    assert history_paths("save-durations") == {"test_durations_history.json"}
    steps = [s.get("name", "") for s in wf["jobs"]["save-durations"]["steps"]]
    assert steps[0] == "Checkout runner script"
    assert steps.index("Append this run to the duration history") > steps.index(
        "Merge into single durations file"
    )


def test_workflow_passes_the_stamped_budgets_to_the_runner():
    wf = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8"))
    run = next(
        s["run"] for s in wf["jobs"]["test"]["steps"]
        if "--files" in str(s.get("run", ""))
    )
    assert "--file-timeouts '${{ matrix.slice.file_timeouts }}'" in run


def test_merge_group_runs_the_same_slice_count_as_pull_requests():
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yaml").read_text(encoding="utf-8"))
    count = ci["jobs"]["tests"]["with"]["slice_count"]
    assert "merge_group" not in str(count)
    assert int(count) == 16
