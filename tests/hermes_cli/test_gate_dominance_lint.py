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

Recognised gates are the kanban tree's real liveness checks and the safe-removal
choke point, plus ``# noqa: gate-dominance`` with a reason for call sites that
genuinely cannot enclose a workspace (log rotation, atomic single-file swaps).
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

#: Attribute calls on a Path that relocate the whole node. Matched only at
#: arity 1: ``Path.rename(target)`` / ``Path.replace(target)`` take exactly
#: one argument, while ``str.replace(old, new)`` always takes two or more --
#: without that discriminator a string normalisation loop reads as a
#: directory relocation (measured on ``_normalize_dispatch_file_path``).
DESTRUCTIVE_METHODS = {"rename", "replace"}

#: The gates that make such a call legitimate. Deliberately NOT including
#: ``_assert_not_delegated_child_mutation``: that is an authority check on the
#: CALLER, not a check on the target's liveness, and counting it satisfied the
#: lint at the exact head that carried the round-6 defect (measured).
#:
#: ``_audit_workspace_deletion`` is deliberately NOT here either. An audit call
#: RECORDS an event; it never refuses an operation, so a regression that keeps
#: the logging while dropping the refusal would pass a lint that counted it --
#: measured on PR #785 head 9b2d17ba, where an audit-then-``rename``/``rmtree``
#: function linted clean. The audit is a separate invariant, asserted
#: independently by the deletion-audit tests.
GATES = {
    "_board_has_live_cards",
    "_live_owners_of_path",
    "_task_has_live_run",
    "safe_remove_workspace_dir",
}

#: Gates that refuse INTERNALLY: routing the destruction through one of these
#: *is* the gate, so no separate refusal is owed at the call site.
SELF_GATING = {"safe_remove_workspace_dir"}

NOQA = "noqa: gate-dominance"


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _names_in(node: ast.AST) -> set[str]:
    """Every bare identifier appearing anywhere under *node*."""
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _block_exits(stmts: list[ast.stmt]) -> bool:
    """Whether executing *stmts* must leave the current control-flow path."""
    for stmt in stmts:
        if isinstance(stmt, (ast.Raise, ast.Return, ast.Break, ast.Continue)):
            return True
        if isinstance(stmt, ast.If) and stmt.orelse:
            if _block_exits(stmt.body) and _block_exits(stmt.orelse):
                return True
    return False


class _Collector(ast.NodeVisitor):
    """Record every call with the chain of ``if`` tests enclosing it.

    Beyond the enclosing-branch chain, two more facts are recorded per gate,
    because "a gate call appears earlier" is not the property that keeps a
    workspace alive (measured on PR #785 head 9b2d17ba -- three distinct
    shapes satisfied the round-6 lint while a live card's directory could
    still be destroyed):

    * ``checked`` -- the identifiers the gate was asked ABOUT, so a gate on
      some other object cannot bless the destruction of this one; and
    * ``refusals`` -- terminating conditional branches driven by the gate,
      recorded with position so logging-only and post-deletion checks cannot
      certify a destructive call.
    """

    def __init__(self) -> None:
        self.stack: list[int] = []
        # (short_name, lineno, ctx, is_gate, checked_names, bound_to)
        self.calls: list[tuple] = []
        #: Conditional checks that terminate one branch before execution can
        #: continue: (lineno, enclosing context, tested names, gate-call lines).
        self.refusals: list[tuple[int, tuple[int, ...], frozenset[str], frozenset[int]]] = []
        #: ``name -> names it was derived from`` for single-target assignments,
        #: so ``d = board_dir(slug)`` links ``d`` back to ``slug``.
        self.provenance: dict[str, set[str]] = {}
        #: Name the current assignment binds to, if any.
        self._assign_target: str | None = None

    # -- structure ---------------------------------------------------------

    def visit_If(self, node: ast.If) -> None:
        # A conditional is a refusal only when one branch terminates before
        # control can reach a later deletion. Merely logging in the branch is
        # observation, not enforcement.
        if _block_exits(node.body) or _block_exits(node.orelse):
            gate_lines = {
                child.lineno
                for child in ast.walk(node.test)
                if isinstance(child, ast.Call)
                and _dotted(child.func).rsplit(".", 1)[-1] in GATES
            }
            self.refusals.append((
                node.lineno,
                tuple(self.stack),
                frozenset(_names_in(node.test)),
                frozenset(gate_lines),
            ))
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

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self.stack.append(node.lineno)
        for stmt in node.body:
            self.visit(stmt)
        self.stack.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        derived = _names_in(node.value)
        for t in targets:
            self.provenance.setdefault(t, set()).update({t, *derived})
        prev, self._assign_target = self._assign_target, (
            targets[0] if len(targets) == 1 else None
        )
        self.visit(node.value)
        self._assign_target = prev

    # -- calls -------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        name = _dotted(node.func)
        short = name.rsplit(".", 1)[-1]
        is_destructive = (
            name in DESTRUCTIVE
            or short in DESTRUCTIVE
            or (isinstance(node.func, ast.Attribute)
                and node.func.attr in DESTRUCTIVE_METHODS
                and len(node.args) == 1 and not node.keywords)
        )
        is_gate = name in GATES or short in GATES
        if is_destructive or is_gate:
            checked: set[str] = set()
            for arg in node.args:
                checked |= _names_in(arg)
            for kw in node.keywords:
                checked |= _names_in(kw.value)
            if isinstance(node.func, ast.Attribute):
                # `d.rename(dest)` destroys `d`, which is the receiver.
                checked |= _names_in(node.func.value)
            self.calls.append((
                short, node.lineno, tuple(self.stack), is_gate,
                frozenset(checked), self._assign_target,
            ))
        prev, self._assign_target = self._assign_target, None
        self.generic_visit(node)
        self._assign_target = prev


def _functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _related(names: frozenset, provenance: dict[str, set[str]]) -> set[str]:
    """Close *names* over simple assignment provenance.

    ``d = board_dir(slug)`` means a gate asked about ``slug`` is a gate about
    ``d``; without this, the real ``remove_board`` shape would read as a gate
    on a different target.
    """
    out = set(names)
    changed = True
    while changed:
        changed = False
        for name in list(out):
            for src in provenance.get(name, ()):  # d -> {d, slug}
                if src not in out:
                    out.add(src)
                    changed = True
        for tgt, srcs in provenance.items():     # slug -> d, the other way
            if tgt not in out and srcs & out:
                out.add(tgt)
                changed = True
    return out


def _violations(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    tree = ast.parse(src)
    out: list[str] = []

    for func in _functions(tree):
        collector = _Collector()
        for stmt in func.body:
            collector.visit(stmt)
        prov = collector.provenance
        gates = [c for c in collector.calls if c[3]]
        destructive = [c for c in collector.calls if not c[3]]
        if not destructive:
            continue
        # Only functions that actually branch their destruction are in scope:
        # a single unconditional relocation has no sibling to walk around.
        branches = {d[2] for d in destructive}
        if len(branches) < 2:
            continue
        for name, lineno, ctx, _is_gate, target_names, _b in destructive:
            line = lines[lineno - 1] if lineno - 1 < len(lines) else ""
            if NOQA in line:
                continue
            if name in SELF_GATING:
                continue
            targets = _related(target_names, prov)
            dominating = []
            for g in gates:
                g_name, g_line, g_ctx, _, g_checked, g_bound = g
                if g_line >= lineno or not set(g_ctx).issubset(set(ctx)):
                    continue
                # A gate must drive a terminating conditional before this
                # destructive call. Logging in a branch, or raising only after
                # destruction, does not protect the target.
                refusal_dominates = any(
                    r_line < lineno
                    and set(r_ctx).issubset(set(ctx))
                    and (g_line in r_gate_lines or (g_bound and g_bound in r_names))
                    for r_line, r_ctx, r_names, r_gate_lines in collector.refusals
                )
                if g_name not in SELF_GATING and not refusal_dominates:
                    continue
                # ...and it has to be a gate about THIS target: asking whether
                # some OTHER card is live is the cross-owner data-loss shape.
                if g_name in SELF_GATING or (_related(g_checked, prov) & targets):
                    dominating.append(g)
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


def _lint(tmp_path, src: str) -> list[str]:
    """Run the lint over an inline sample with ``REPO`` pointed at *tmp_path*."""
    sample = tmp_path / "sample.py"
    sample.write_text(src, encoding="utf-8")
    global REPO
    original, REPO = REPO, tmp_path
    try:
        return _violations(sample)
    finally:
        REPO = original


# ---------------------------------------------------------------------------
# The three fake-green shapes FleetReview measured on PR #785 (findings
# "Audit logging is incorrectly treated as a deletion gate" and "Dominance is
# accepted without verifying the gate protects the deletion target"). Each one
# passed the round-6 lint while a live card's workspace could still be
# destroyed -- a lint that blesses these is worse than no lint, because it
# certifies the class it exists to catch.
# ---------------------------------------------------------------------------

def test_audit_logging_alone_is_not_a_gate(tmp_path):
    """An audit call RECORDS; it never refuses. It cannot bless a deletion.

    A regression that preserves auditing while dropping the liveness refusal
    must still be caught: the audit line names the deleter of a running
    worker's cwd, it does not prevent the deletion.
    """
    found = _lint(tmp_path, (
        "import shutil\n"
        "def retire(d, *, archive=True):\n"
        "    _audit_workspace_deletion(d, reason='retire')\n"
        "    if archive:\n"
        "        d.rename(dest)\n"
        "    else:\n"
        "        shutil.rmtree(d)\n"
    ))
    assert any("rename" in v for v in found), found
    assert any("rmtree" in v for v in found), found


def test_a_gate_on_a_different_target_does_not_dominate(tmp_path):
    """Checking that SOME other thing is idle says nothing about ``d``.

    This is the cross-owner data-loss shape: ``_task_has_live_run(conn,
    caller_id)`` is true of the CALLER, while the directory being removed
    belongs to a different, live card.
    """
    found = _lint(tmp_path, (
        "import shutil\n"
        "def retire(d, other, *, archive=True):\n"
        "    if _board_has_live_cards(other):\n"
        "        raise ValueError('live')\n"
        "    if archive:\n"
        "        d.rename(dest)\n"
        "    else:\n"
        "        shutil.rmtree(d)\n"
    ))
    assert any("rename" in v for v in found), found


def test_a_discarded_gate_result_does_not_dominate(tmp_path):
    """A gate whose answer nothing reads cannot refuse anything."""
    found = _lint(tmp_path, (
        "import shutil\n"
        "def retire(d, *, archive=True):\n"
        "    _live_owners_of_path(d)\n"
        "    if archive:\n"
        "        d.rename(dest)\n"
        "    else:\n"
        "        shutil.rmtree(d)\n"
    ))
    assert any("rename" in v for v in found), found


@pytest.mark.parametrize(
    "gate_src",
    [
        pytest.param(
            "    if _board_has_live_cards(d):\n"
            "        print('live')\n",
            id="direct-log-only",
        ),
        pytest.param(
            "    live = _board_has_live_cards(d)\n"
            "    if live:\n"
            "        print('live')\n",
            id="bound-log-only",
        ),
        pytest.param(
            "    live = _board_has_live_cards(d)\n",
            id="bound-refusal-after-deletion",
        ),
    ],
)
def test_a_gate_must_refuse_before_the_deletion(tmp_path, gate_src):
    """Logging, or refusing only after destruction, cannot certify a gate."""
    suffix = (
        "    if live:\n"
        "        raise ValueError('live')\n"
        if "live =" in gate_src and "if live:" not in gate_src
        else ""
    )
    found = _lint(tmp_path, (
        "import shutil\n"
        "def retire(d, *, archive=True):\n"
        f"{gate_src}"
        "    if archive:\n"
        "        d.rename(dest)\n"
        "    else:\n"
        "        shutil.rmtree(d)\n"
        f"{suffix}"
    ))
    assert any("rename" in v for v in found), found
    assert any("rmtree" in v for v in found), found


def test_a_gate_bound_to_a_name_and_then_refused_does_dominate(tmp_path):
    """ALLOW control: the real ``remove_board`` shape must stay accepted.

    The gate result is bound to a name and refused one statement later, and
    the destructive target derives from the slug the gate was asked about.
    Without this control the fix above would just be "reject everything".
    """
    found = _lint(tmp_path, (
        "import shutil\n"
        "def retire(slug, *, archive=True):\n"
        "    d = board_dir(slug)\n"
        "    live = _board_has_live_cards(slug)\n"
        "    if live:\n"
        "        raise ValueError('live')\n"
        "    if archive:\n"
        "        d.rename(dest)\n"
        "    else:\n"
        "        shutil.rmtree(d)\n"
    ))
    assert not found, found


def test_routing_through_the_safe_choke_point_is_accepted(tmp_path):
    """ALLOW control: the choke point gates internally, so using it is safe."""
    found = _lint(tmp_path, (
        "def reap(p, tid, *, archive=True):\n"
        "    if archive:\n"
        "        safe_remove_workspace_dir(p, task_id=tid, reason='a')\n"
        "    else:\n"
        "        safe_remove_workspace_dir(p, task_id=tid, reason='b')\n"
    ))
    assert not found, found
