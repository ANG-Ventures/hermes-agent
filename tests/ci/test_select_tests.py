"""Tests for scripts/ci/select_tests.py — change-based test selection.

The safety-critical property is FAIL-OPEN: any ambiguity must return ALL, never
a narrowed set that could skip an affected test. These tests lock that in.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "ci"))
import select_tests as st  # noqa: E402


# ── fail-open triggers → ALL ────────────────────────────────────────────────
@pytest.mark.parametrize("changed", [
    ["pyproject.toml"],
    ["uv.lock"],
    ["conftest.py"],
    ["tests/conftest.py"],
    [".github/workflows/ci.yml"],
    ["tests/fixtures/shared.py"],
    ["tests/helpers/util.py"],
    ["plugins/kanban/plugin_api.py"],          # dynamic-loader dir
    ["hermes_tools/foo.py"],                    # auto-imported tool
    ["tools/cronjob_tools.py"],                 # dynamic dispatch
    ["some/registry.py"],                       # dynamic-dispatch name
    ["hermes_cli/__init__.py"],                 # package init (name hint)
])
def test_fail_open_triggers_return_all(changed, tmp_path):
    assert st.select(changed, tmp_path) == st.ALL


def test_empty_diff_returns_all(tmp_path):
    assert st.select([], tmp_path) == st.ALL


def test_oversized_changeset_returns_all(tmp_path):
    changed = [f"pkg/mod_{i}.py" for i in range(st.MAX_CHANGED_FILES + 1)]
    assert st.select(changed, tmp_path) == st.ALL


def test_non_python_changed_file_returns_all(tmp_path):
    # A stray non-.py that reached selection (e.g. a json fixture) → fail open.
    assert st.select(["config/thing.json"], tmp_path) == st.ALL


# ── a leaf source change selects its dependent tests (real graph) ───────────
def _mini_repo(tmp_path: Path) -> Path:
    """A tiny repo: leaf.py, hub.py (imports leaf), tests importing each."""
    (tmp_path / "leaf.py").write_text("def f():\n    return 1\n")
    (tmp_path / "user.py").write_text("import leaf\n\ndef g():\n    return leaf.f()\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (tests / "test_leaf.py").write_text("import leaf\n\ndef test_a():\n    assert leaf.f() == 1\n")
    (tests / "test_user.py").write_text("import user\n\ndef test_b():\n    assert user.g() == 1\n")
    (tests / "test_unrelated.py").write_text("def test_c():\n    assert True\n")
    return tmp_path


def test_leaf_change_selects_only_dependent_tests(tmp_path):
    repo = _mini_repo(tmp_path)
    # changing leaf.py should select test_leaf (imports leaf) AND test_user
    # (imports user which imports leaf) — but NOT test_unrelated.
    result = st.select(["leaf.py"], repo)
    assert result != st.ALL
    sel = set(result)
    assert "tests/test_leaf.py" in sel
    assert "tests/test_user.py" in sel
    assert "tests/test_unrelated.py" not in sel


def test_changed_test_file_runs_itself(tmp_path):
    repo = _mini_repo(tmp_path)
    result = st.select(["tests/test_unrelated.py"], repo)
    assert result != st.ALL
    assert "tests/test_unrelated.py" in set(result)


def test_unknown_new_source_module_returns_all(tmp_path):
    repo = _mini_repo(tmp_path)
    # a .py that doesn't exist in the built graph (brand-new file) → fail open
    assert st.select(["brand_new_module.py"], repo) == st.ALL


# ── the load-bearing fix: LAZY (function-level) imports must NOT couple ──────
def test_lazy_import_does_not_couple_modules(tmp_path):
    """A function-level import is lazy — it must not make the importer a
    transitive dependent. This is the fix that un-collapsed the dense graph."""
    (tmp_path / "heavy.py").write_text("def boom():\n    return 1\n")
    # cheap.py imports heavy ONLY inside a function (lazy) → not coupled at import
    (tmp_path / "cheap.py").write_text(
        "def use():\n    import heavy\n    return heavy.boom()\n"
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (tests / "test_cheap.py").write_text("import cheap\n\ndef test_x():\n    assert True\n")
    # changing heavy.py must NOT select test_cheap (cheap only lazily imports heavy)
    result = st.select(["heavy.py"], tmp_path)
    # heavy has zero top-level importers among tests → resolves to zero dependent
    # tests → fail-open ALL (belt-and-suspenders), NOT a wrong empty narrowing.
    assert result == st.ALL


def test_toplevel_import_does_couple(tmp_path):
    """Contrast: a TOP-LEVEL import DOES couple (the real dependency)."""
    (tmp_path / "heavy.py").write_text("def boom():\n    return 1\n")
    (tmp_path / "eager.py").write_text("import heavy\n\ndef use():\n    return heavy.boom()\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (tests / "test_eager.py").write_text("import eager\n\ndef test_x():\n    assert True\n")
    result = st.select(["heavy.py"], tmp_path)
    assert result != st.ALL
    assert "tests/test_eager.py" in set(result)


def test_type_checking_import_does_not_couple(tmp_path):
    """`if TYPE_CHECKING:` imports never run → must not couple."""
    (tmp_path / "heavy.py").write_text("class T:\n    pass\n")
    (tmp_path / "typed.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    import heavy\n\ndef f(x):\n    return x\n"
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (tests / "test_typed.py").write_text("import typed\n\ndef test_x():\n    assert True\n")
    result = st.select(["heavy.py"], tmp_path)
    # no real top-level importer → fail-open ALL, never a wrong narrowing
    assert result == st.ALL
