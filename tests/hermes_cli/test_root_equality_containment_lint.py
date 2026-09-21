"""Lint: a containment guard before a destructive op must reject path == root.

THE DEFECT CLASS (incident 2026-09-20, card t_63fb42f9). ``Path.relative_to``
and ``Path.is_relative_to`` SUCCEED when the two paths are EQUAL --
``p.relative_to(p)`` returns ``Path('.')`` rather than raising. So this shape:

    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return                      # "never delete outside root"
    shutil.rmtree(path)

reads like a containment guard and is one for descendants -- but it PERMITS
``path == root``, i.e. deleting the root itself and everything under it. That
is the single most destructive input the guard exists to reject.

Two live instances were found in this repo:

  * ``hermes_cli/kanban.py:_cmd_gc`` -- an archived task row whose
    ``workspace_path`` equalled the scratch root would rmtree the root,
    destroying EVERY card's workspace including running workers'.
  * ``hermes_cli/managed_uv.py:_remove_tree`` -- a ``candidate`` or
    ``generation`` that resolved to its own boundary would take the whole
    managed runtime tree.

Both are fixed; this lint is the detector so the class cannot come back. The
fix is always one of:

  * route through a helper that requires strict descendancy (the kanban side
    now uses ``kanban_db.safe_remove_workspace_dir``), or
  * add an explicit ``if resolved == root_resolved: return`` before the
    ``relative_to`` call.

Recognised as "guarded" by this lint:
  - an equality check against the root within the guard's few lines
  - ``!=`` / ``==`` comparison of the two resolved paths
  - a ``parents`` / ``.parent`` containment idiom
  - an explicit ``# noqa: root-equality`` with a reason
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Directories whose destructive ops are out of scope (vendored, generated).
SKIP_PARTS = {
    "node_modules", ".git", "venv", ".venv", "site-packages",
    "staging", "tests", "tests-js", "evals", "eval",
}

DESTRUCTIVE = re.compile(
    r"\bshutil\.rmtree\s*\(|\bshutil\.move\s*\(|\.unlink\s*\(|"
    r"\bos\.remove\s*\(|\bos\.rmdir\s*\(|\bos\.removedirs\s*\("
)
RELATIVE_TO = re.compile(r"\.(?:is_)?relative_to\s*\(")
# Any of these within the guard window means the author handled equality.
EQUALITY_HANDLED = re.compile(
    r"==|!=|\bin\s+\S*\.parents\b|\.parent\b|noqa:\s*root-equality"
)

# How many lines after the guard still count as "the same guard".
WINDOW = 12


def _candidate_files():
    for path in sorted(REPO.rglob("*.py")):
        rel = path.relative_to(REPO)
        if SKIP_PARTS & set(rel.parts):
            continue
        yield path, rel


def _find_violations(path: Path, rel: Path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if not RELATIVE_TO.search(text) or not DESTRUCTIVE.search(text):
        return []
    # Cheap syntax gate: a file we cannot parse is not our problem.
    try:
        ast.parse(text)
    except SyntaxError:
        return []

    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        if not RELATIVE_TO.search(line):
            continue
        if line.lstrip().startswith("#"):
            continue
        window = lines[i:i + WINDOW]
        joined = "\n".join(window)
        if not DESTRUCTIVE.search(joined):
            continue
        # Look a couple of lines BEFORE too -- the equality check usually
        # sits immediately above the relative_to call.
        context = "\n".join(lines[max(0, i - 4):i + WINDOW])
        if EQUALITY_HANDLED.search(context):
            continue
        out.append((f"{rel}:{i + 1}", line.strip()))
    return out


def test_no_unguarded_root_equality_before_destructive_op():
    violations = []
    for path, rel in _candidate_files():
        violations.extend(_find_violations(path, rel))

    if violations:
        detail = "\n".join(f"  {loc}\n      {src}" for loc, src in violations)
        pytest.fail(
            "Containment guard permits path == root before a destructive "
            "operation.\n\n"
            "`Path.relative_to(root)` SUCCEEDS when path == root (it returns "
            "'.'), so these guards do NOT stop the root itself from being "
            "deleted -- the worst possible input. On 2026-09-20 this exact "
            "shape in _cmd_gc wiped the entire kanban scratch root under "
            "running workers (card t_63fb42f9).\n\n"
            "Fix: add `if resolved == root_resolved: return` before the "
            "relative_to call, or route through a helper that requires "
            "strict descendancy (e.g. kanban_db.safe_remove_workspace_dir). "
            "If the equality case is genuinely safe here, annotate the line "
            "with `# noqa: root-equality  <reason>`.\n\n"
            f"{len(violations)} site(s):\n{detail}"
        )


def test_lint_detects_the_known_bad_shape():
    """The lint must be born-red against the pre-fix _cmd_gc source.

    Without this, deleting the rule table would make the suite pass.
    """
    bad = '''
import shutil
from pathlib import Path


def cleanup(path: Path, root: Path) -> None:
    try:
        path = path.resolve()
    except OSError:
        return
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return
    shutil.rmtree(path, ignore_errors=True)
'''
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "bad.py"
        p.write_text(bad, encoding="utf-8")
        found = _find_violations(p, Path("bad.py"))
    assert found, "lint failed to flag the known-bad root-equality shape"


def test_lint_accepts_the_fixed_shape():
    good = '''
import shutil
from pathlib import Path


def cleanup(path: Path, root: Path) -> None:
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
    except OSError:
        return
    if resolved == root_resolved:
        return
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return
    shutil.rmtree(resolved, ignore_errors=True)
'''
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "good.py"
        p.write_text(good, encoding="utf-8")
        found = _find_violations(p, Path("good.py"))
    assert not found, f"lint false-positived on the fixed shape: {found}"
