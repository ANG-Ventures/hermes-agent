"""A registered call-site nodeid that did not execute is RED; `*_ptb.py` leaves the slices.

t_34bb9bef: CI slice 1/1 on #1741 printed "skipped every test ... surfaced, not
failed" for tests/plugins/test_telegram_intake_sentinel_wiring_ptb.py. Its
nodeids are in the registry's `call_site_tests`, so the D2b lint was satisfied
while no CI lane executed them. The check is per nodeid (Prism P1 on #1743):
a passing sibling test in the same file must not hide a skipped registered one.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUNNER = _REPO_ROOT / "scripts" / "run_tests_parallel.py"
_REL = "tests/test_wiring.py"
_BODY = """\
import pytest

def test_a():
    pass

def test_b():
    pytest.skip("optional dep missing")
"""


def _runner():
    spec = importlib.util.spec_from_file_location("_t34bb9bef_runner", _RUNNER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _repo(tmp_path: Path, call_site_tests: list[str] | None) -> Path:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / _REL).write_text(_BODY)
    if call_site_tests is not None:
        manifest = repo / "docs" / "sync" / "fork-features.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps([
            {"feature": "f", "tests": call_site_tests, "call_site_tests": call_site_tests},
            {"feature": "no call site", "tests": [f"{_REL}::test_a"]},
        ]))
    return repo


def _junit(mod, repo: Path, junit_dir: Path) -> None:
    """Run pytest on the file the way the runner does, to get a real xunit1 junit."""
    junit_dir.mkdir(exist_ok=True)
    out = junit_dir / mod.junit_name(_REL)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", _REL,
         f"--junitxml={out}", "-o", "junit_family=xunit1"],
        cwd=repo, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out.is_file()


def _guard(mod, repo: Path, summary: dict) -> bool:
    return mod._noop_guard(
        all_summaries=[(repo / _REL, summary)], total_executed=1, explicit_files=set(),
        strict_noop=True, min_tests=None, repo_root=repo,
    )


def test_skipped_registered_nodeid_is_red_even_when_a_sibling_passed(tmp_path, monkeypatch, capsys):
    mod = _runner()
    repo = _repo(tmp_path, [f"{_REL}::test_b"])
    monkeypatch.setattr(mod, "_JUNIT_DIR", tmp_path / "junit")
    _junit(mod, repo, tmp_path / "junit")
    assert _guard(mod, repo, {"passed": 1, "skipped": 1}) is True
    assert f"{_REL}::test_b" in capsys.readouterr().out


def test_registered_nodeid_that_ran_is_green(tmp_path, monkeypatch):
    mod = _runner()
    repo = _repo(tmp_path, [f"{_REL}::test_a"])
    monkeypatch.setattr(mod, "_JUNIT_DIR", tmp_path / "junit")
    _junit(mod, repo, tmp_path / "junit")
    assert _guard(mod, repo, {"passed": 1, "skipped": 1}) is False


def test_registered_nodeid_not_collected_is_red(tmp_path, monkeypatch, capsys):
    mod = _runner()
    repo = _repo(tmp_path, [f"{_REL}::test_a", f"{_REL}::test_renamed_away"])
    monkeypatch.setattr(mod, "_JUNIT_DIR", tmp_path / "junit")
    _junit(mod, repo, tmp_path / "junit")
    assert _guard(mod, repo, {"passed": 1, "skipped": 1}) is True
    out = capsys.readouterr().out
    assert f"{_REL}::test_renamed_away" in out and f"{_REL}::test_a\n" not in out


def test_missing_junit_for_a_registered_file_is_red(tmp_path, monkeypatch):
    mod = _runner()
    repo = _repo(tmp_path, [f"{_REL}::test_a"])
    monkeypatch.setattr(mod, "_JUNIT_DIR", tmp_path / "junit")  # never written
    assert _guard(mod, repo, {"passed": 1}) is True


def test_without_junit_dir_a_skip_only_registered_file_is_red(tmp_path, monkeypatch):
    mod = _runner()
    repo = _repo(tmp_path, [f"{_REL}::test_b"])
    monkeypatch.setattr(mod, "_JUNIT_DIR", None)
    assert _guard(mod, repo, {"skipped": 2}) is True
    assert _guard(mod, repo, {"noop_skip": True}) is True


def test_skip_only_file_not_in_call_site_tests_stays_a_warning(tmp_path, monkeypatch):
    mod = _runner()
    repo = _repo(tmp_path, [f"{_REL.replace('wiring', 'other')}::test_x"])
    monkeypatch.setattr(mod, "_JUNIT_DIR", None)
    assert _guard(mod, repo, {"skipped": 2}) is False


def test_tree_without_manifest_keeps_skip_storm_a_warning(tmp_path, monkeypatch):
    mod = _runner()
    repo = _repo(tmp_path, None)
    monkeypatch.setattr(mod, "_JUNIT_DIR", None)
    assert _guard(mod, repo, {"skipped": 2}) is False


def _matrix(*args: str) -> tuple[dict, str]:
    proc = subprocess.run(
        [sys.executable, str(_RUNNER), *args],
        cwd=_REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1]), proc.stderr


_PTB = "tests/plugins/test_telegram_polling_progress_ptb.py"


def test_generate_slices_leaves_ptb_files_to_their_lane() -> None:
    keep = "tests/test_run_tests_parallel_noop_guard.py"
    matrix, err = _matrix("--generate-slices", "2", "--files", f"{_PTB}:{keep}")
    sliced = {f for s in matrix["slice"] for f in s["files"].split(":") if f}
    assert sliced == {keep}
    assert _PTB in err


def test_ptb_only_impact_set_slices_the_full_suite_at_the_full_slice_count() -> None:
    # The impact selector sized --generate-slices for one small file (1); the
    # fallback slices the whole suite, so it must use the full count instead.
    matrix, _err = _matrix("--generate-slices", "1", "--full-slices", "3", "--files", _PTB)
    assert len(matrix["slice"]) == 3
    sliced = [f for s in matrix["slice"] for f in s["files"].split(":") if f]
    assert len(sliced) > 100
    assert not any(f.endswith("_ptb.py") for f in sliced)
