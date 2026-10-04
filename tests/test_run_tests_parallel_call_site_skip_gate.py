"""A registered call-site test that only skips is RED; `*_ptb.py` leaves the slices.

t_34bb9bef: CI slice 1/1 on #1741 printed "skipped every test ... surfaced, not
failed" for tests/plugins/test_telegram_intake_sentinel_wiring_ptb.py. That file
is named in the registry's `call_site_tests`, so the D2b lint was satisfied
while no CI lane executed it.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUNNER = _REPO_ROOT / "scripts" / "run_tests_parallel.py"


def _runner():
    spec = importlib.util.spec_from_file_location("_t34bb9bef_runner", _RUNNER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _repo(tmp_path: Path, call_site_tests: list[str]) -> Path:
    manifest = tmp_path / "docs" / "sync" / "fork-features.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps([
        {"feature": "f", "tests": call_site_tests, "call_site_tests": call_site_tests},
        {"feature": "no call site", "tests": ["tests/test_other.py::test_x"]},
    ]))
    (tmp_path / "tests").mkdir()
    return tmp_path


def _guard(mod, repo: Path, summaries) -> bool:
    return mod._noop_guard(
        all_summaries=summaries, total_executed=3, explicit_files=set(),
        strict_noop=True, min_tests=None, repo_root=repo,
    )


def test_skip_only_call_site_test_file_is_red(tmp_path: Path, capsys) -> None:
    mod = _runner()
    repo = _repo(tmp_path, ["tests/test_wiring_ptb.py::test_a", "tests/test_wiring_ptb.py::test_b"])
    f = repo / "tests" / "test_wiring_ptb.py"
    assert _guard(mod, repo, [(f, {"skipped": 1})]) is True
    out = capsys.readouterr().out
    assert "call_site_tests" in out and "tests/test_wiring_ptb.py" in out


def test_module_level_importorskip_call_site_file_is_red(tmp_path: Path) -> None:
    mod = _runner()
    repo = _repo(tmp_path, ["tests/test_wiring_ptb.py::test_a"])
    f = repo / "tests" / "test_wiring_ptb.py"
    assert _guard(mod, repo, [(f, {"noop_skip": True})]) is True


def test_skip_only_file_not_in_call_site_tests_stays_a_warning(tmp_path: Path) -> None:
    mod = _runner()
    repo = _repo(tmp_path, ["tests/test_wiring_ptb.py::test_a"])
    other = repo / "tests" / "test_other.py"  # in `tests`, not `call_site_tests`
    assert _guard(mod, repo, [(other, {"skipped": 2})]) is False


def test_call_site_file_that_ran_is_green(tmp_path: Path) -> None:
    mod = _runner()
    repo = _repo(tmp_path, ["tests/test_wiring_ptb.py::test_a"])
    f = repo / "tests" / "test_wiring_ptb.py"
    assert _guard(mod, repo, [(f, {"passed": 5, "skipped": 1})]) is False


def test_tree_without_manifest_keeps_skip_storm_a_warning(tmp_path: Path) -> None:
    mod = _runner()
    f = tmp_path / "tests" / "test_wiring_ptb.py"
    assert _guard(mod, tmp_path, [(f, {"skipped": 1})]) is False


def _matrix(files: str) -> tuple[dict, str]:
    proc = subprocess.run(
        [sys.executable, str(_RUNNER), "--generate-slices", "2", "--files", files],
        cwd=_REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1]), proc.stderr


def test_generate_slices_leaves_ptb_files_to_their_lane() -> None:
    ptb = "tests/plugins/test_telegram_polling_progress_ptb.py"
    keep = "tests/test_run_tests_parallel_noop_guard.py"
    matrix, err = _matrix(f"{ptb}:{keep}")
    sliced = {f for s in matrix["slice"] for f in s["files"].split(":") if f}
    assert sliced == {keep}
    assert ptb in err


def test_generate_slices_of_only_ptb_files_falls_back_to_the_full_suite() -> None:
    matrix, _err = _matrix("tests/plugins/test_telegram_polling_progress_ptb.py")
    sliced = [f for s in matrix["slice"] for f in s["files"].split(":") if f]
    assert len(sliced) > 100
    assert not any(f.endswith("_ptb.py") for f in sliced)
