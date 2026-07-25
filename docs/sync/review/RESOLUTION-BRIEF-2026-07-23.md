# Parity Merge Resolution Brief — 2026-07-23

You are resolving the staged upstream→fork parity merge in THIS worktree
(`~/.hermes/worktrees/parity-2026-07-23`, branch `sync/upstream-2026-07-23`).
Frozen upstream target: `a7a696ba5`. Fork base: `37fa0a353`. 125 conflict files / 912 hunks.

## Objective
Resolve ALL conflicts (UU/AA/DU) preserving BOTH histories' intent, then run
`python3.11 -m hermes_parity gates` until green (or a documented residual list).
Do NOT commit the merge / do NOT run `finish` — leave the resolved tree + gates
output for the orchestrator (Apollo) to review and land.

## Method (non-negotiable)
- Protocol reference: `docs/sync/README-hermes-parity.md` + the conflict report
  at `docs/sync/review/conflict-buckets.md`.
- For EVERY semantic hunk: read both sides' history (`git log -p fork/main -- <file>`,
  `git log -p origin/main -- <file>` scoped to the region) before choosing.
  NEVER blind-pick a side — a blind pick silently regresses a live feature.
- Fork features that MUST survive are registered in `docs/sync/fork-features.json`.
  After resolving, verify every registered feature's paths/tests still exist and pass.
- Locales (14 files): both-sides-added keys → union merge, keep YAML valid,
  dedupe identical keys, upstream wins on identical-key conflicting values ONLY
  for upstream-owned keys; fork-owned keys (gateway.branch/gateway.merge and any
  key absent upstream) keep fork values.
- AA files (tui_gateway/compute_host.py, host_supervisor.py, synthetic_turn.py,
  scripts/iso-certify.py, + their tests): these are fork features upstream ingested
  and evolved. Base on upstream's copy (:3) and re-apply any fork-side (:2) delta
  that is a real fix/behavior difference — check `source=` vs `platform_override=`
  kwarg against the actual callee signature in THIS merged tree, and the
  `tick`/`now` monotonic fix in synthetic_turn.py (fork side re-samples time —
  determine which is correct, don't assume).
- DU mem0 plugin files (plugins/memory/mem0/_backend.py, _oss_providers.py,
  _setup.py + tests): upstream deleted/relocated. Find where upstream moved the
  mem0 logic; port the fork's self-host modifications there. If upstream truly
  removed the capability, KEEP the fork files (fork runs self-hosted mem0 in prod).
  tests/run_agent/test_run_agent.py: check whether upstream split it; migrate
  fork-only test cases to the new location.
- gateway/run.py (84 hunks), slash_commands.py, web_server.py, hermes_state.py:
  highest regression risk. Fork-critical behaviors that MUST survive include:
  /branch + /merge reciprocal links + footer-consistent counts
  (tests/gateway/test_discord_branch_thread_merge.py), fork_ext call sites
  (agent/fork_ext/*, cron/fork_ext/*), relay lane headers, tool-gate seams,
  compaction announce. Every fork_ext 1-line call site must survive.
- Known trap: a resolved hunk may need an IMPORT the other side removed —
  after each file, syntax-check (py_compile / tsc for .ts) before moving on.

## Gates
- `python3.11 -m hermes_parity gates` — the full ladder. The traps stage
  (lint_merge_traps.py) warnings: investigate each, they key to real past incidents.
- For test reds use `python3.11 -m hermes_parity bisect <nodeids>` to classify
  MERGE-REGRESSION vs STALE-TEST vs INHERITED. Spot-verify any INHERITED verdict
  with a direct fork/main baseline run before accepting.
- STALE tests: update to the upstream contract the merge adopted.
  REGRESSIONS: fix the CODE, preserving fork behavior.

## Output
Write a running ledger to `docs/sync/review/RESOLUTION-LEDGER.md`:
per file — resolution choice, why, residual risk. Final section: gates status,
unresolved items, anything needing an operator decision. Honest blockers > fake greens.
