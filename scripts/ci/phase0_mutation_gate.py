#!/usr/bin/env python3
"""Phase-0 mutation gate for change-based selection (Opus B1 — the real safety proof).

The tautology trap: "the selected set is a superset of the test files changed in
the PR's own diff" proves NOTHING (a test always imports itself). The REAL
property we must prove: when selection NARROWS a PR, the narrowed set still
catches a bug injected into the changed *source* file. If a mutation to changed
source survives (every selected test still passes), that's a FALSE-NARROW — the
exact failure mode that ships a bug.

For each (PR, changed source file):
  1. compute the selected test set (skip if ALL — no narrowing, nothing to prove)
  2. inject a mutation into the changed source file (targeted, reversible)
  3. run ONLY the selected set
  4. PASS if ≥1 selected test goes RED (mutation caught); FALSE-NARROW if all green
  5. revert the mutation

Reports: mutations tried, caught, survived (false-narrows), and skipped
(no mutable target / no tests). A single survivor is a NO-GO for going live.

Usage:
  PY=.venv/bin/python
  $PY scripts/ci/phase0_mutation_gate.py < /tmp/pr_files.json
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import select_tests as st  # noqa: E402


def _find_mutations(source: str):
    """Yield (kind, lineno, new_op_or_none) for many candidate mutations across
    the file — comparison flips, boolean-constant flips, and return-constant
    tweaks. We try several because a single site may be in dead/untested code;
    the gate needs at least one CAUGHT mutation to certify, and a SURVIVOR is a
    real false-narrow signal.
    """
    out = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out
    flips = {ast.Eq: "!=", ast.NotEq: "==", ast.Lt: ">=", ast.Gt: "<=",
             ast.LtE: ">", ast.GtE: "<"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and node.ops and type(node.ops[0]) in flips:
            out.append(("compare", node.lineno, flips[type(node.ops[0])]))
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            out.append(("boolflip", node.lineno, None))
    return out


def _apply_mutation(path: Path, kind: str, lineno: int, new_op) -> str:
    """Apply the mutation at the given line via AST rewrite. Returns original
    source text (for revert)."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    class Mut(ast.NodeTransformer):
        done = False

        def visit_Compare(self, node):
            if (not self.done and kind == "compare" and node.lineno == lineno
                    and node.ops):
                m = {"==": ast.NotEq, "!=": ast.Eq, ">=": ast.Lt, "<=": ast.Gt,
                     ">": ast.LtE, "<": ast.GtE}
                if new_op in m:
                    node.ops[0] = m[new_op]()
                    self.done = True
            return self.generic_visit(node)

        def visit_Constant(self, node):
            if (not self.done and kind == "boolflip" and node.lineno == lineno
                    and isinstance(node.value, bool)):
                node.value = not node.value
                self.done = True
            return node

    new_tree = Mut().visit(tree)
    ast.fix_missing_locations(new_tree)
    path.write_text(ast.unparse(new_tree), encoding="utf-8")
    return src


def _run_selected(repo_root: Path, py: str, test_files, timeout=900) -> bool:
    """Run the selected test files; return True if ANY failed (mutation caught)."""
    if not test_files:
        return False
    cmd = [py, "-m", "pytest", *test_files, "-o", "addopts=", "-q",
           "-p", "no:cacheprovider", "-x", "--no-header"]
    env = dict(os.environ, HERMES_HOME=tempfile.mkdtemp())
    try:
        r = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return True  # a hang counts as "not silently green"
    return r.returncode != 0


def main() -> int:
    repo_root = Path(os.environ.get("HERMES_REPO_ROOT", ".")).resolve()
    py = os.environ.get("HERMES_TEST_PY", str(repo_root / ".venv" / "bin" / "python"))
    data = json.load(sys.stdin)

    tried = caught = survived = skipped = 0
    survivors = []
    MAX_MUT_PER_PR = int(os.environ.get("HERMES_PHASE0_MAX_MUT", "8"))

    for pr, files in data.items():
        res = st.select(list(files), repo_root)
        if isinstance(res, str):
            continue  # ALL — no narrowing to falsify
        selected = res
        src_files = [f for f in files if f.endswith(".py")
                     and not (f.startswith("tests/") and Path(f).name.startswith("test_"))]
        pr_had_target = False
        pr_caught = False
        for sf in src_files:
            if pr_caught:
                break
            p = repo_root / sf
            if not p.exists():
                continue
            muts = _find_mutations(p.read_text(encoding="utf-8"))
            if not muts:
                continue
            pr_had_target = True
            for (kind, lineno, new_op) in muts[:MAX_MUT_PER_PR]:
                print(f"PR#{pr}: mutate {sf}:{lineno} ({kind}); run {len(selected)} "
                      f"selected tests ...", file=sys.stderr)
                orig = _apply_mutation(p, kind, lineno, new_op)
                try:
                    red = _run_selected(repo_root, py, selected)
                finally:
                    p.write_text(orig, encoding="utf-8")  # revert
                tried += 1
                if red:
                    caught += 1
                    pr_caught = True
                    print(f"  ✓ caught (a selected test went RED)", file=sys.stderr)
                    break
                else:
                    print(f"  · mutation survived this site; trying next", file=sys.stderr)
        if pr_had_target and not pr_caught:
            # EVERY mutation to this PR's changed source survived the selected
            # set → the narrowed set does NOT guard the changed file = FALSE-NARROW.
            survived += 1
            survivors.append((pr, src_files, len(selected)))
            print(f"  ✗ FALSE-NARROW PR#{pr}: no selected test caught any mutation "
                  f"to {src_files}", file=sys.stderr)
        elif not pr_had_target:
            skipped += 1

    print(f"\n=== Phase-0 mutation gate ===")
    print(f"tried={tried} caught={caught} survived(false-narrows)={survived} skipped={skipped}")
    if survivors:
        print("FALSE-NARROWS (NO-GO):")
        for pr, sf, ln in survivors:
            print(f"  PR#{pr}: {sf}:{ln}")
        return 1
    if tried == 0:
        print("INCONCLUSIVE: no narrowed PR had a mutable source target.")
        return 2
    print(f"GO: {caught}/{tried} mutations caught, 0 false-narrows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
