#!/usr/bin/env python3
"""Phase-0 measurement for change-based selection (Opus B1/B4 fix).

Builds the import graph ONCE, then for each real merged PR computes the
selected set and reports the size distribution — distinguishing ALL / empty /
count honestly (not conflating them). This is the GO/NO-GO gate: if p50 of the
selected fraction ≈ full, the graph is too dense and selection isn't worth the
risk.

Usage: measure_select.py <pr_files_dir>  where the dir has one file per PR,
each containing that PR's changed paths (one per line). Or pass PR file lists
on argv as name=path pairs. Simplest: pipe a JSON of {pr: [files]} on stdin.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import select_tests as st  # noqa: E402


def main() -> int:
    repo_root = Path(st.os.environ.get("HERMES_REPO_ROOT", ".")).resolve()
    data = json.load(sys.stdin)  # {"154": ["file1", ...], ...}

    print(f"building import graph over {repo_root} ...", file=sys.stderr)
    graph = st.ImportGraph(repo_root)
    total = len(graph.all_test_files)
    print(f"  {total} test files, {len(graph._mod_to_path)} source modules, "
          f"{len(graph._parse_errors)} parse errors", file=sys.stderr)

    results = []
    for pr, files in data.items():
        res = st.select(list(files), repo_root)
        if isinstance(res, str):  # ALL
            results.append((pr, len(files), "ALL", None))
        else:
            results.append((pr, len(files), "SUBSET", len(res)))

    print(f"\n{'PR':>8} {'#files':>7} {'verdict':>8} {'selected':>9} {'frac':>6}")
    counts = []
    all_count = 0
    for pr, nf, verdict, sel in results:
        if verdict == "ALL":
            all_count += 1
            print(f"{pr:>8} {nf:>7} {'ALL':>8} {'—':>9} {'100%':>6}")
        else:
            frac = (sel / total * 100) if total else 0
            counts.append(sel)
            print(f"{pr:>8} {nf:>7} {'SUBSET':>8} {sel:>9} {frac:>5.0f}%")

    print(f"\n=== {len(results)} PRs: {all_count} ALL, {len(counts)} narrowed ===")
    if counts:
        counts.sort()
        p50 = statistics.median(counts)
        p90 = counts[int(len(counts) * 0.9)] if len(counts) > 1 else counts[0]
        print(f"narrowed selected-count: min={min(counts)} p50={p50:.0f} "
              f"p90={p90} max={max(counts)}  (of {total})")
        print(f"p50 fraction of suite: {p50/total*100:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
