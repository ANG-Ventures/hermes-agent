"""07-23 parity-merge COLLATERAL-LOSS SWEEP.

Finds definitions silently dropped by a merge commit (not by either side
deliberately). For each .py the merge touched:

  offered  = names in the merge's UPSTREAM parent (^2)
  ingested = names in the merge RESULT
  lost     = offered - ingested, still absent from fork HEAD today,
             and still shipped by upstream (MERGE_HEAD)

A name in `lost` was offered by the upstream side of that merge, silently
did not survive the merge resolution, is still missing from the fork, and
upstream still ships it => merge collateral, not a deliberate deletion.

NOTE: an earlier version of this script required the name in BOTH parents
("neither side deleted it"). That is the WRONG criterion and reports a
false 0 — the whole point is that these names existed on the UPSTREAM side
only, and the merge dropped them on the way in. Provenance: 2 such losses
found by hand during the 2026-08-07 parity merge
(tests/test_retry_utils.py, tests/tools/test_url_safety.py), both from
d995f30b3c; the both-parents criterion missed both.

Usage:  python docs/sync/review/tools/collateral-loss-sweep.py [merge-sha]
"""
import ast
import subprocess
import sys

MERGE = sys.argv[1] if len(sys.argv) > 1 else "d995f30b3c"


def blob(rev, path):
    r = subprocess.run(
        ["git", "cat-file", "-p", "{}:{}".format(rev, path)],
        capture_output=True, text=True,
    )
    return r.stdout if r.returncode == 0 else None


def names(src):
    if not src:
        return None
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    return set(
        n.name for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )


def rev(expr):
    return subprocess.run(
        ["git", "rev-parse", expr], capture_output=True, text=True
    ).stdout.strip()


def main():
    p1 = rev(MERGE + "^1")
    p2 = rev(MERGE + "^2")
    if not p1 or not p2:
        print("not a merge commit: " + MERGE)
        return 1
    changed = subprocess.run(
        ["git", "diff", "--name-only", MERGE + "^1", MERGE],
        capture_output=True, text=True,
    ).stdout.split()
    files = [f for f in changed if f.endswith(".py")]
    print("merge {} parents {} {}".format(MERGE, p1[:10], p2[:10]))
    print("python files touched: {}".format(len(files)))

    hits = []
    total = 0
    for f in files:
        offered = names(blob(p2, f))
        ingested = names(blob(MERGE, f))
        head = names(blob("HEAD", f))
        up = names(blob("MERGE_HEAD", f))
        if offered is None or ingested is None or head is None or up is None:
            continue
        lost = offered.difference(ingested).difference(head).intersection(up)
        if lost:
            hits.append((f, sorted(lost)))
            total += len(lost)

    print("\nfiles with collateral loss: {}   names lost: {}\n".format(len(hits), total))
    for f, ns in sorted(hits, key=lambda x: -len(x[1])):
        print("{:4d}  {}".format(len(ns), f))
        for n in ns[:6]:
            print("        " + n)
        if len(ns) > 6:
            print("        ... +{} more".format(len(ns) - 6))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
