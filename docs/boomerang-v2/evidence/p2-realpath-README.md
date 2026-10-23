# AC-4 / AC-4b re-measured through the LIVE supervisor (QA F-1)

Date: 2026-10-19 23:13–23:26 PT. Runtime under test: fork/main `05071447` (includes #2144, the F-1 fix).

Why this was re-run: Argus's milestone QA (t_230d37a3) found that the original pack (`p2-cache-trials.jsonl`,
`p2-cost-pairs.jsonl`) drove children with `fork_run_inputs` + `run_conversation` itself. That path was not
the one production took: the live supervisor `delegate_tool._run_single_child` ran fork children on the bare
goal. This harness now calls `_run_single_child(0, goal, child=..., parent_agent=...)`, the exact supervisor a
`delegate_task` fork goes through.

Command (scratch `HERMES_HOME`, live `plugins/model-providers` linked read-only, `CLAUDE_POOL_*` only):

```
HERMES_HOME=<scratch> python tools/dev_probes/boomerang_v2_p2_live.py cache --prefix-tokens 205000 --trials 16 --out p2-cache-trials-realpath.jsonl
<!-- no-tmp: ok — verbatim record of the command the evidence run used -->
HERMES_HOME=<scratch> TERMINAL_CWD=/tmp python tools/dev_probes/boomerang_v2_p2_live.py cost --prefix-tokens 205000 --max-iterations 6 --out p2-cost-pairs-realpath.jsonl
```

## AC-4: cache gate: PASS (16/16)

- Warm parent: 326,884 prompt tokens, 1h write, served by `sub-vps-10`.
- 16/16 trials `mode=fork`, every one pinned to `sub-vps-10` and served by `sub-vps-10`.
- `read_share` min **0.9994** (gate ≥ 0.85). Typical first request: 326,884 read, ~195 written, 4 uncached.
- Rows: `p2-cache-trials-realpath.jsonl`.

## AC-4b: cost: PASS (5/5 fork ≤ fold)

Cost in input-token equivalents (README ratios: read 0.1, 1h write 2.0, 5m write 1.25, output 5.0):

- completed_verify: fork 87,825 vs fold 179,687 (fork first read_share 0.9991)
- red_review: fork 105,222 vs fold 196,885 (0.9993)
- changes_requested: fork 84,005 vs fold 178,461 (0.9992)
- first_crash_retry: fork 70,311 vs fold 160,773 (0.9992)
- long_investigation: fork 146,014 vs fold 220,471 (0.9990)

Rows: `p2-cost-pairs-realpath.jsonl`.

## Live production cross-check

- Aegis, 2026-10-19 22:23 on `05071447`: a real `delegate_task(inherit_mode="fork")` child recalled a nonce
  planted only in the parent's earlier turn. `boomerang.fork served_by=sub-vps-2 read_share=0.9969`, child
  cache write 215 tokens.
- Apollo smoke step 3, 22:59: `boomerang.fork served_by=sub-vps-1 read_share=0.9845`.
- Before #2144 (5 production forks on 2026-10-19): read_share ≈ 0.224, ~55k cold write each.

The original `p2-cache-trials.jsonl` / `p2-cost-pairs.jsonl` stay in place as the record of what was measured
first. They are superseded by the `-realpath` files for AC-4 / AC-4b.
