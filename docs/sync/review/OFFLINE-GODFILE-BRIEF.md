# OFFLINE GOD-FILE LANE BRIEF (one file per worker)

You resolve ONE god-file from the parity merge, OFFLINE. You never write inside the
worktree and NEVER run `git add` / commit / touch the index. The orchestrator stages.

## Setup (your file = $GODFILE, given in your card body)
WT=/Users/alexgierczyk/.hermes/worktrees/parity-2026-08-29
OUT=/tmp/godfile-$(basename $GODFILE .py)
mkdir -p $OUT
cd $WT   # READ-ONLY: git show / git log / grep only. No writes in this tree.
git show :1:$GODFILE > $OUT/base.py    # merge base
git show :2:$GODFILE > $OUT/ours.py    # fork side
git show :3:$GODFILE > $OUT/theirs.py  # upstream side

## Method
1. READ the worktree's docs/sync/review/RESOLUTION-BRIEF-2026-08-29.md — especially the
   ADDENDUM (absorption census rulings). Several subsystems in god-files are now
   UPSTREAM-CANONICAL (compute-host, ws-interrupt, startup-restore gate, typing-stop,
   retry_status, slash handler bodies). Honor those rulings.
2. Try algorithm selection FIRST: `git merge-file -p --diff3 $OUT/ours.py $OUT/base.py $OUT/theirs.py`
   and diff-algorithm variants — but NEVER trust a suspiciously clean result: run the
   symbol-diff oracle (below) before believing any auto-merge.
3. Resolve hunk-by-hunk into $OUT/RESOLVED.py. Upstream refactor structure wins; fork
   BEHAVIOR re-threads into it. fork_ext call sites MUST survive (grep fork_ext, ≥1
   non-import use). Dual-param contracts thread through every helper.
4. Symbol-diff oracle (MANDATORY, in EVIDENCE.md):
   - python3 -c AST dump of def/class names for ours/theirs/RESOLVED
   - upstream-only functions retained = ALL (list count), fork-only retained = ALL,
     any exception listed with a reason citing the census addendum or a ledger rule.
5. `python3 -m py_compile $OUT/RESOLVED.py` must pass. Zero conflict markers.
6. Write $OUT/EVIDENCE.md: per-hunk choice + why, symbol counts both directions,
   census rulings applied, residual risks, and any OPERATOR-DECISION flags (test
   contract ahead of both sides etc. — flag, don't fabricate).

## Deliverable
$OUT/RESOLVED.py + $OUT/EVIDENCE.md. Report the paths + headline symbol counts.
Do NOT touch the worktree index. Do NOT git add. The orchestrator validates + stages.
