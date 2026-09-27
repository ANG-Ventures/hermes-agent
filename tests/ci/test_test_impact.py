"""Tests for scripts/ci/test_impact.py, the pull_request test-impact pre-filter.

The load-bearing property: a change to a SHARED module selects every test that
reaches it transitively, not just the test that imports it directly. Break the
reverse-closure walk and ``test_shared_module_selects_transitive_dependents``
goes red.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_PATH = _REPO / "scripts" / "ci" / "test_impact.py"
_spec = importlib.util.spec_from_file_location("test_impact", _PATH)
assert _spec is not None and _spec.loader is not None
ti = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ti)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    # pkg.core <- pkg.mid <- pkg.leaf (module-scope imports), plus a lazy
    # function-level import that must NOT create an edge.
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/core.py", "VALUE = 1\n")
    _write(tmp_path, "pkg/mid.py", "from pkg import core\n")
    _write(tmp_path, "pkg/leaf.py", "import pkg.mid\n")
    _write(tmp_path, "pkg/lazy.py", "def f():\n    import pkg.core\n")
    _write(tmp_path, "pkg/other.py", "X = 2\n")
    _write(tmp_path, "pkg/orphan.py", "Y = 3\n")
    _write(tmp_path, "scripts/tool.py", "Z = 4\n")
    _write(tmp_path, "tests/__init__.py", "")
    _write(tmp_path, "tests/test_core.py", "from pkg.core import VALUE\n")
    _write(tmp_path, "tests/test_leaf.py", "import pkg.leaf\n")
    _write(tmp_path, "tests/test_lazy.py", "from pkg import lazy\n")
    _write(tmp_path, "tests/test_other.py", "import pkg.other\n")
    _write(
        tmp_path,
        "tests/test_patch.py",
        "def test_x(monkeypatch):\n    monkeypatch.setattr('pkg.other.X', 3)\n",
    )
    _write(
        tmp_path,
        "tests/test_tool.py",
        "P = 'scripts/tool.py'\n",
    )
    _write(tmp_path, "tests/sub/conftest.py", "import pkg.other\n")
    _write(tmp_path, "tests/sub/test_in_sub.py", "def test_a():\n    pass\n")
    return tmp_path


def _select(repo: Path, *changed: str) -> list[str] | None:
    files, _reason = ti.select(list(changed), repo, {})
    return files


def test_shared_module_selects_transitive_dependents(repo: Path) -> None:
    selected = _select(repo, "pkg/core.py")
    assert selected is not None
    # direct importer AND the test reaching core through mid -> leaf
    assert "tests/test_core.py" in selected
    assert "tests/test_leaf.py" in selected
    assert "tests/test_other.py" not in selected


def test_function_level_import_is_not_an_edge(repo: Path) -> None:
    selected = _select(repo, "pkg/core.py")
    assert selected is not None
    assert "tests/test_lazy.py" not in selected


def test_dotted_string_and_conftest_imports_select(repo: Path) -> None:
    selected = _select(repo, "pkg/other.py")
    assert selected is not None
    assert {"tests/test_other.py", "tests/test_patch.py", "tests/sub/test_in_sub.py"} <= set(
        selected
    )


def test_path_mention_selects_script_consumer(repo: Path) -> None:
    assert _select(repo, "scripts/tool.py") == ["tests/test_tool.py"]


def test_changed_test_file_selects_itself(repo: Path) -> None:
    assert _select(repo, "tests/test_other.py") == ["tests/test_other.py"]


def test_nested_conftest_selects_its_directory(repo: Path) -> None:
    assert _select(repo, "tests/sub/conftest.py") == ["tests/sub/test_in_sub.py"]


@pytest.mark.parametrize(
    "changed",
    [
        ["tests/conftest.py"],
        ["pyproject.toml"],
        ["uv.lock"],
        [".github/workflows/tests.yml"],
        ["scripts/run_tests_parallel.py"],
        ["scripts/ci/test_impact.py"],
        ["pkg/orphan.py"],  # no test reaches it: unseen linkage -> full
        ["pkg/core.py", "pkg/orphan.py"],
        ["docs/readme.md"],  # nothing selected -> full
        ["../escape.py"],
        [],
    ],
)
def test_ambiguity_fails_open_to_full(repo: Path, changed: list[str]) -> None:
    assert _select(repo, *changed) is None


def test_selection_above_share_cap_runs_full(repo: Path) -> None:
    durations = {"tests/test_core.py": 1000.0}
    files, reason = ti.select(["pkg/core.py"], repo, durations)
    assert files is None and "suite duration" in reason


def _cli(repo: Path, event: str, changed: str, out: Path) -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            str(_PATH),
            "--repo-root",
            str(repo),
            "--event",
            event,
            "--full-slices",
            "16",
            "--files-out",
            str(out),
            changed,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


@pytest.mark.parametrize("event", ["merge_group", "push", "workflow_dispatch"])
def test_cli_never_narrows_the_gate(repo: Path, tmp_path: Path, event: str) -> None:
    out = tmp_path / "files.txt"
    result = _cli(repo, event, "pkg/core.py", out)
    assert result["slices"] == 0
    assert not out.exists()


def test_cli_pull_request_writes_selection(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "files.txt"
    result = _cli(repo, "pull_request", "pkg/core.py", out)
    assert result["slices"] >= 1
    assert "tests/test_leaf.py" in out.read_text(encoding="utf-8").split()


def test_runner_generates_matrix_from_files_from(tmp_path: Path) -> None:
    listed = ["tests/ci/test_evaluate_needs.py", "tests/ci/test_classify_changes.py"]
    listing = tmp_path / "files.txt"
    listing.write_text("\n".join(listed) + "\n", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(_REPO / "scripts" / "run_tests_parallel.py"),
            "--generate-slices",
            "2",
            "--files-from",
            str(listing),
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=_REPO,
    )
    matrix = json.loads(proc.stdout)
    got = sorted(
        f for s in matrix["slice"] for f in str(s["files"]).split(":") if f
    )
    assert got == sorted(listed)
