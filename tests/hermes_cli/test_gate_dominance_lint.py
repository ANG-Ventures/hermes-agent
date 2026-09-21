"""Lint: a gate on a multi-branch destructive function must DOMINATE every branch.

THE DEFECT CLASS (card t_63fb42f9, review round 6). ``remove_board`` has two
destructive branches -- ``archive=True`` renames the board directory away,
``archive=False`` removes it -- and both take that board's ``workspaces/t_*``
with them. The liveness refusal was written inside ``if not archive:``, so it
covered only the branch a user has to opt into (``boards rm --delete``,
dashboard ``?delete=true``). The DEFAULT branch relocated a RUNNING card's cwd
with no refusal and no audit line: the 2026-09-20 incident's literal symptom,
reproduced on the fixed code.

The shape is general and it reads as safe, which is why it survived three
review rounds:

    def retire(target, *, archive=True):
        if not archive:
            if _has_live_cards(target):   # <-- gate, nested one branch deep
                raise ValueError(...)
        if archive:
            target.rename(dest)           # <-- UNGATED destructive branch
        else:
            rmtree(target)                # <-- gated

A guard that a sibling branch can walk around is not a guard. So: within a
function, every destructive/relocating call must be preceded by a gate call
whose set of enclosing conditionals is a SUBSET of its own -- i.e. the gate
cannot sit behind a branch that the destructive call does not itself require.

Recognised gates are the kanban tree's real ones (liveness checks and the
audit choke point), plus ``# noqa: gate-dominance`` with a reason for call
sites that genuinely cannot enclose a workspace (log rotation, atomic
single-file swaps).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: Files in scope. This class is about kanban workspace/board retirement, so
#: the sweep covers the kanban tree -- the same grep the review asked for.
SCOPED_FILES = [
    "hermes_cli/kanban_db.py",
    "hermes_cli/kanban.py",
    "hermes_cli/kanban_survivor.py",
    "hermes_cli/kanban_transfer.py",
    "hermes_cli/kanban_worker_exit.py",
]

#: Calls that relocate or destroy a directory tree. A single-file ``unlink``
#: cannot enclose a workspace, so it is not in this set -- the class is about
#: operations whose target may CONTAIN a card's scratch dir.
DESTRUCTIVE = {
    "shutil.rmtree",
    "shutil.move",
    "remove_workspace_dir",
    "_remove_tree",
}

#: Attribute calls on a Path that relocate the whole node.
DESTRUCTIVE_METHODS = {"rename", "replace"}

#: The gates that make such a call legitimate. Deliberately NOT including
#: ``_assert_not_delegated_child_mutation``: that is an authority check on the
#: CALLER, not a check on the target's liveness, and counting it satisfied the
#: lint at the exact head that carried the round-6 defect (measured).
GATES = {
    "_board_has_live_cards",
    "_live_owners_of_path",
    "_task_has_live_run",
    "_audit_workspace_deletion",
    "safe_remove_workspace_dir",
}

NOQA = "noqa: gate-dominance"


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


class _Collector(ast.NodeVisitor):
    """Record every call with the chain of ``if`` tests enclosing it."""

    def __init__(self) -> None:
        self.stack: list[int] = []
        self.calls: list[tuple[str, int, tuple[int, ...], bool]] = []

    def visit_If(self, node: ast.If) -> None:
        # The test itself runs in the ENCLOSING context, not the branch it
        # guards -- `if _board_has_live_cards(d): raise` is a gate that
        # dominates both arms, so it must be recorded at the current depth.
        self.visit(node.test)
        # Body and each orelse arm are distinct branch contexts; the test's
        # own line id distinguishes them, negated for the else arm.
        self.stack.append(node.lineno)
        for stmt in node.body:
            self.visit(stmt)
        self.stack.pop()
        self.stack.append(-node.lineno)
        for stmt in node.orelse:
            self.visit(stmt)
        self.stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        name = _dotted(node.func)
        short = name.rsplit(".", 1)[-1]
        is_destructive = (
            name in DESTRUCTIVE
            or short in DESTRUCTIVE
            or (isinstance(node.func, ast.Attribute)
                and node.func.attr in DESTRUCTIVE_METHODS)
        )
        is_gate = name in GATES or short in GATES
        if is_destructive or is_gate:
            self.calls.append((short, node.lineno, tuple(self.stack), is_gate))
        self.generic_visit(node)


def _functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _violations(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    tree = ast.parse(src)
    out: list[str] = []

    for func in _functions(tree):
        collector = _Collector()
        for stmt in func.body:
            collector.visit(stmt)
        gates = [c for c in collector.calls if c[3]]
        destructive = [c for c in collector.calls if not c[3]]
        if not destructive:
            continue
        # Only functions that actually branch their destruction are in scope:
        # a single unconditional relocation has no sibling to walk around.
        branches = {d[2] for d in destructive}
        if len(branches) < 2:
            continue
        for name, lineno, ctx, _ in destructive:
            line = lines[lineno - 1] if lineno - 1 < len(lines) else ""
            if NOQA in line:
                continue
            dominating = [
                g for g in gates
                if g[1] < lineno and set(g[2]).issubset(set(ctx))
            ]
            if not dominating:
                out.append(
                    f"{path.relative_to(REPO)}:{lineno} {func.name}(): "
                    f"{name}() is reachable on a branch no gate dominates "
                    f"(gates at lines {[g[1] for g in gates]})"
                )
    return out


@pytest.mark.parametrize("relpath", SCOPED_FILES)
def test_no_branch_escapes_its_destructive_gate(relpath):
    path = REPO / relpath
    if not path.is_file():
        pytest.skip(f"{relpath} not present in this checkout")
    violations = _violations(path)
    assert not violations, (
        "a destructive/relocating call is reachable on a branch its gate does "
        "not dominate — a guard a sibling branch walks around is not a guard:\n"
        + "\n".join(violations)
    )


def test_the_lint_detects_the_round6_defect_shape(tmp_path):
    """Born-red control: reintroduce the sunk gate and the lint must fire."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import shutil\n"
        "def retire(d, *, archive=True):\n"
        "    if not archive:\n"
        "        if _board_has_live_cards(d):\n"
        "            raise ValueError('live')\n"
        "    if archive:\n"
        "        d.rename(dest)\n"
        "    else:\n"
        "        shutil.rmtree(d)\n",
        encoding="utf-8",
    )
    global REPO
    original, REPO = REPO, tmp_path
    try:
        found = _violations(sample)
    finally:
        REPO = original
    assert any("rename" in v for v in found), found


def test_the_lint_accepts_a_hoisted_gate(tmp_path):
    """ALLOW control: hoisting the gate above the branch clears the finding."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import shutil\n"
        "def retire(d, *, archive=True):\n"
        "    if _board_has_live_cards(d):\n"
        "        raise ValueError('live')\n"
        "    if archive:\n"
        "        d.rename(dest)\n"
        "    else:\n"
        "        shutil.rmtree(d)\n",
        encoding="utf-8",
    )
    global REPO
    original, REPO = REPO, tmp_path
    try:
        found = _violations(sample)
    finally:
        REPO = original
    assert not found, found
