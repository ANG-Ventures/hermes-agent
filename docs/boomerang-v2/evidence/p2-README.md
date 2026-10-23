# Boomerang v2 P2: live fork checks (AC-4, AC-4b)

Card t_a134504f, 2026-10-18 22:33–23:05 PDT. Spec `~/.hermes/plans/2026-10-18_boomerang-fork-autowake-spec.md`
§5.1, §5.3, AC-4, AC-4b. The AC-5 / INV-4 probes are not here; they were carved out to t_c4e7974e
(`inv4-probes/README.md`, PASS 19/20, 0/20). The frame turn in this PR is the D-10 user turn that passed there.

## Verdict

| check | bar | result |
|---|---|---|
| AC-4 cache trials | 16/16 trials with first-request read_share >= 0.85 on a >= 200k prefix | **16/16**, every trial 0.9994, prefix 325,650 tokens |
| AC-4b cost pairs | fork total cost <= fold total cost on each of 5 pairs | **5/5** |

## Method

Script: `tools/dev_probes/boomerang_v2_p2_live.py` (this PR). It drives the production code, not a replay:
a real `AIAgent` parent on `claude-alr` (`claude-sonnet-5-5`), and children built by the real
`_build_child_agent(inherit_mode=...)` and run by `run_conversation` with the inputs from
`fork_run_inputs`. Usage is read off each provider response (`input_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens`, `cache_creation.ephemeral_{1h,5m}_input_tokens`), the same numbers the relay
writes to its ledger. Scratch `HERMES_HOME` (profile scratch dir) with the live
`plugins/model-providers` symlinked read-only and only the two `CLAUDE_POOL_*` lines in `.env`.

```
HERMES_HOME=<scratch> python tools/dev_probes/boomerang_v2_p2_live.py cache --prefix-tokens 205000 --trials 16 --out p2-cache-trials.jsonl
<!-- no-tmp: ok — verbatim record of the command the evidence run used -->
HERMES_HOME=<scratch> TERMINAL_CWD=/tmp python tools/dev_probes/boomerang_v2_p2_live.py cost --prefix-tokens 205000 --max-iterations 6 --out p2-cost-pairs.jsonl
```

Parent prefix: 13 repo source files as user turns with short assistant acks, ending on a complete
text-only assistant turn (D-8). One parent turn wrote it: 325,646 tokens of 1h cache creation on
`sub-vps-13` (P1 captured `last_served_by=sub-vps-13`, P2 captured the split `{"1h": 325646, "5m": 0}`).

## AC-4 (`p2-cache-trials.jsonl`, 16 rows)

Each trial: one fork child, one request. All 16: `mode=fork`, `pin=sub-vps-13`, `served_by=sub-vps-13`,
`cache_read` 325,646–325,650, `cache_creation` 195–199 (the frame turn), read_share 0.9994, age 9–175 s.
No trial downgraded.

## AC-4b (`p2-cost-pairs.jsonl`, 5 rows)

Each pair runs the same task as a fork child and as a fold (`inherit_mode="summary"`) child, toolset
`file`, `max_iterations=6`, and sums usage over every request. Cost is in input-token equivalents with
Anthropic list ratios (input 1, cache read 0.1, 1h write 2, 5m write 1.25, output 5), so the two arms
compare on one axis.

| task | fork reqs / cost | fold reqs / cost | fork <= fold |
|---|---|---|---|
| completed_verify | 2 / 87,530 | 3 / 179,700 | yes |
| red_review | 4 / 137,609 | 5 / 204,432 | yes |
| changes_requested | 2 / 85,164 | 2 / 179,868 | yes |
| first_crash_retry | 2 / 70,950 | 2 / 161,327 | yes |
| long_investigation | 4 / 191,200 | 4 / 203,778 | yes |

The fork's first request read 0.999 of its prompt in every pair. The fold's first request wrote its
whole prompt cold (~77k: slim child prompt + tool schemas + the ≤50k fold), which is why it costs
more even though its prefix is a quarter of the size. The margin narrows as iterations grow
(long_investigation, 4 requests: 191k vs 204k): each fork iteration re-reads 325k at 0.1. This is the
§5.3 caveat; at much longer runs the fold would win, which D-6's per-kind `mode` column is for.

Caveats, reported as found:
- `red_review`: the task text names the test file without its directory and the scratch terminal cwd
  <!-- no-tmp: ok — describes the scratch cwd the run actually had -->
  is `/tmp`, so BOTH arms answered "couldn't find test_delegate_tool_fork_mode.py". The pair compares
  two equal failures; it is still a matched pair, but not a useful answer.
- The cost run's parent turn READ the cache the AC-4 run had written 3 minutes earlier
  (`cache_creation 0`), so no split was recorded for it and `warm` fell back to `_cache_ttl=1h`.
  Every fork in that run still read warm on `sub-vps-13`.

## Spend

AC-4: 1 cold 325k 1h write + 16 fork reads (~5.2 M cache-read tokens). AC-4b: 1 parent read +
10 children, 31 requests. Plus one 27k smoke (2 trials) before the gate run.
