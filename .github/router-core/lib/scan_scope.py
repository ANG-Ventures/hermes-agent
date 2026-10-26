"""scan_scope -- THE answer to "is this repo path live source a tree-wide lint should grade?".

Two trees under scripts/ are NOT live source:
  scripts/retired/  code taken out of service; kept for history, never run.
  scripts/attic/    byte-copied v1 merge-gate code (merge-path-simplify P1b, t_e860148e). The live tree never
                    imports it; fleet-merge.sh only execs it for a profile-v1 repo. Its gates are its own:
                    `merge_decision_shape_lint.py --attic-callees` (CALLEES.txt) and the `## attic` section of
                    scripts/test-fleet-merge.sh. A tree-wide lint that grades it re-flags code its live
                    allowlists already cover (the 109 copies tripped 13 lints on hermes-home#4912).

Every tree-wide lint asks `is_live_source(rel)` instead of hard-coding the prefixes. A third non-live tree is one
edit here. `scripts/lints/scan-scope-lint.py` fails a lint that names `scripts/retired/` as its own literal.
Python 3.9-compatible: lint_live_sqlite_reads.py runs on 3.9..3.14.

Shell lints filter a path list through it: `... | python3 scripts/lib/scan_scope.py --filter` prints only the live
paths, one per line (stdin is newline-separated repo-relative paths).
"""
from __future__ import annotations

NON_LIVE_PREFIXES = ("scripts/retired/", "scripts/attic/")


def is_live_source(relpath) -> bool:
    """False for a path under a non-live tree. `relpath` is repo-relative (os.sep or '/', leading './' ok)."""
    rel = str(relpath).replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    rel = rel.lstrip("/")
    return not any(rel == p.rstrip("/") or rel.startswith(p) for p in NON_LIVE_PREFIXES)


if __name__ == "__main__":
    import sys
    if sys.argv[1:] != ["--filter"]:
        sys.stderr.write("usage: scan_scope.py --filter  (stdin: repo-relative paths; stdout: the live ones)\n")
        sys.exit(2)
    for line in sys.stdin:
        path = line.rstrip("\n")
        if path and is_live_source(path):
            sys.stdout.write(path + "\n")
