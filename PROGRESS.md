# t_a27dd0c4 progress

## Scope

1. Refresh all open NousResearch/hermes-agent PRs authored by Kyzcreig, desktop-first.
2. Port fork PR #308 (SessionDB executor offload) to upstream.
3. Port fork PR #307 (dashboard turn isolation) to upstream and run AC-4 in an isolated scratch dashboard.
4. Open upstream PRs only; never merge upstream.

## Ledger

- 2026-07-12 01:28 PDT — Run 41 resumed. Confirmed this worker owns the board claim and assigned worktree. Recovered prior run's uncommitted #308 port from the old task workspace; focused suite had previously reached 375 green tests, but no commit or PR exists yet.
- 2026-07-12 01:30 PDT — Fresh upstream query found 42 open PRs authored by Kyzcreig, not 13 or 30. Desktop priority state: #62398, #56827, and #40174 are CONFLICTING/DIRTY; #62716 and #62399 CLEAN; #62703 and #62699 BLOCKED with green-or-mostly-green checks. Starting the three dirty desktop rebases first.
- 2026-07-12 01:34 PDT — Rebased #62398 onto upstream `095b9eed3`, preserved current-main `use-prompt-actions` coverage during its sole conflict, fixed two touched-file lint errors, and force-pushed verified head `0a02a4ce3`. Fresh local evidence: Electron platform tests 320 pass / 1 skip / 0 fail; Vitest excluding one upstream-red panes test 1,229 pass / 0 fail; typecheck green. The panes failure reproduces unchanged on clean upstream.
- 2026-07-12 01:35 PDT — #56827 is obsolete on current upstream: `fe82b3a77 fix(desktop): read attachment previews local-first in remote mode` implements the same local-first/fallback behavior and current `use-composer-actions.test.ts` carries the same regression cases. Aborted the rebase; did not mutate or close the PR. #40174 is likewise obsolete: current upstream has `oauth-net-request.ts` plus `oauth-session-request.test.ts`, and the OAuth Electron request path no longer sets restricted Content-Length.
- 2026-07-12 01:49 PDT — Repaired #62699 attribution by amending author/committer to `9063726+Kyzcreig@users.noreply.github.com`; force-with-lease pushed verified head `2b8dbc3c5`.
- 2026-07-12 01:56 PDT — Ported fork #308 to upstream PR #63082 (`44918ccf9`): SessionDB open/use/close moved off the dashboard event loop. Base proof: 2/2 focused tests fail on clean upstream. Fixed branch: 2/2 focused tests pass; 557 web-server tests pass; Ruff and diff checks pass. Broad `tests/hermes_cli` remains ambient-red outside this patch.
- 2026-07-12 02:38 PDT — Ported the full fork #307 mechanism to current upstream PR #63096, head `8d08fecc1`: default-off compute-host supervisor, streamed delta/control protocol, metadata mirror, PPID orphan guard, PID-reuse-safe reconciliation, inline dispatch fail-open, synthetic heavy-turn seam, and scratch AC-4 harness. Clean-current-base focused suite: 343 passed; Ruff/diff checks passed. Exact 360s/six-lane AC-4 PASS runs observed at 7.08ms, 6.92ms, and final-current-base 6.11ms serving p99, all with zero serving stalls and valid load. One additional current-base run correctly reported INCONCLUSIVE after a stale terminal event; harness now requires the new turn's `message.start` before scoring and the full rerun passed.

- 2026-07-12 — Senior review independently accepted both dashboard ports. Fresh GitHub rollups now show #63082 and #63096 at 31 passing / 0 failing / 0 pending checks. Neither upstream PR was merged.
- 2026-07-12 — Rebased and force-with-lease pushed #62716 at `c9c6a67e56`; resolved the current-main workspace-target conflict while preserving server-owned pins. Verification: 673 Python tests, 29 desktop tests, and both desktop typechecks passed.
- 2026-07-12 — Rebased and verified #60253, #60146, #47017, #42447, #40157, #34537, and #34298. After fresh CI exposed Slack's 50-command cap on the stacked undo/redo family, rebased #47017 again on current main, explicitly routed low-frequency `/version` through `/hermes version`, and rebuilt #60253 on that corrected base. Final local evidence: #47017 382 undo/redo tests + 173 command-registry tests passed; #60253 561 focused tests passed.
- 2026-07-12 — Rebuilt contaminated-history #59463 rather than rebasing its ~300 unrelated fork commits. First rebased/re-authored prerequisite #58144 at `b87e3d4485` (30 tests + subprocess guard pass), then replayed the single SSRF-proxy commit on top and pushed #59463 at `f7af41628b` (186 tests + subprocess guard pass). Added a PR comment documenting the stack and verification.
- 2026-07-12 — Resolved and refreshed formerly conflicting #34294 at `08de1dd040` and #23331 at `d8c98f48fc`. #34294 preserves upstream's dynamically-derived blocklist while allowing `execute_code`; focused feature tests and the CI-failing MCP seam pass locally. Its previous CI failure was a profile-local MCP discovery flake that passes on both current main and the PR branch; a fresh CI run is active. #23331 carries current dynamic context-file truncation through HERMES_HOME AGENTS.md; 165 passed / 1 skipped.
- 2026-07-12 — Rebased/re-authored #62925 (`5086604fa9`), #37513 (`e3b85403cd`), #37418 (`9c364e77f5`), and #37381 (`75d891978a`) to repair contributor-check failures. Focused results: 5, 76, 5, and 15 passed respectively. #62925's broader delegate file reproduced the same heartbeat timing failure on clean current main; feature-specific tests passed.
- 2026-07-12 — Final inventory remains 42 open Kyzcreig PRs. Fresh local `merge-tree` assessment against `origin/main` is 36 clean / 6 conflicting. All six conflicts are report-only rather than safe mechanical rebases: #38976 violates current config.yaml-over-env policy; #34295 and #34146 are implemented on main; #25397/#25396/#24586 are superseded by newer clean/refreshed PRs. No upstream PR was closed or merged.

## Final 42-PR sweep table

Check cells are `passing / failing / pending` at the final query; newly pushed branches may still be running.

| PR | Local apply | Checks | Action |
|---:|:---:|:---:|---|
| #63096 | CLEAN | 31 / 0 / 0 | ported full #307 mechanism; CI green; senior review accepted |
| #63082 | CLEAN | 31 / 0 / 0 | ported #308 SessionDB offload; CI green; senior review accepted |
| #62925 | CLEAN | 17 / 0 / 10 | rebased, attribution repaired, pushed `5086604fa9`; 5 focused pass |
| #62716 | CLEAN | 36 / 0 / 0 | rebased/pushed `c9c6a67e56`; 673 Python + 29 desktop pass; typechecks pass |
| #62703 | CLEAN | 22 / 0 / 0 | no branch mutation; policy/review blocked |
| #62699 | CLEAN | 31 / 0 / 0 | attribution refreshed; clean |
| #62399 | CLEAN | 36 / 0 / 0 | no mutation; clean |
| #62398 | CLEAN | 22 / 0 / 0 | rebased/pushed; desktop suites + typecheck pass |
| #60253 | CLEAN | 17 / 0 / 10 | rebased/pushed `37bcf6d350`; Slack-cap drift fixed in base; 561 focused pass |
| #60146 | CLEAN | 31 / 0 / 0 | rebased/pushed `d44f92c800`; 20 pass |
| #59463 | CLEAN | 31 / 0 / 0 | rebuilt on refreshed #58144; pushed `f7af41628b`; 186 pass |
| #58144 | CLEAN | 31 / 0 / 0 | rebased/re-authored/pushed `b87e3d4485`; 30 pass + subprocess guard |
| #47600 | CLEAN | 35 / 0 / 0 | no mutation; clean |
| #47017 | CLEAN | 17 / 0 / 10 | rebased/pushed `948aaf2101`; current Slack cap curated; 382 + 173 pass |
| #46453 | CLEAN | 30 / 0 / 0 | no mutation; clean |
| #42447 | CLEAN | 31 / 0 / 0 | rebased/pushed `e315dac093`; 41 pass |
| #41653 | CLEAN | 20 / 0 / 0 | no mutation; clean |
| #40237 | CLEAN | 23 / 0 / 0 | no mutation; clean |
| #40157 | CLEAN | 31 / 0 / 0 | rebased/pushed `0a526ace0c`; 15 pass |
| #39587 | CLEAN | 23 / 0 / 0 | no mutation; clean |
| #39584 | CLEAN | 23 / 0 / 0 | no mutation; clean |
| #39520 | CLEAN | 23 / 0 / 0 | no mutation; clean |
| #38976 | CONFLICT | 23 / 0 / 0 | report only: env-gated behavioral config violates current config.yaml policy |
| #37513 | CLEAN | 15 / 0 / 12 | rebased/re-authored/pushed `e3b85403cd`; 76 pass |
| #37418 | CLEAN | 15 / 0 / 12 | rebased/re-authored/pushed `9c364e77f5`; 5 pass |
| #37381 | CLEAN | 10 / 0 / 17 | rebased/re-authored/pushed `75d891978a`; 15 pass |
| #34537 | CLEAN | 31 / 0 / 0 | rebased/pushed `e6a736f7c1`; 28 pass |
| #34368 | CLEAN | no rollup | no mutation; clean |
| #34299 | CLEAN | no rollup | no mutation; clean newer resolver variant |
| #34298 | CLEAN | 31 / 0 / 0 | rebased/pushed `e1c9a7d010`; 2 pass |
| #34297 | CLEAN | no rollup | no mutation; clean newer cached-client variant |
| #34295 | CONFLICT | no rollup | report only: behavior implemented on main (`stop_typing` before stale return) |
| #34294 | CLEAN | rerunning, 0 failing | rebased/re-authored/pushed `08de1dd040`; 5 feature + MCP seam pass |
| #34293 | CLEAN | no rollup | no mutation; clean newer inline-provider variant |
| #34292 | CLEAN | no rollup | no mutation; clean newer Codex slash parser variant |
| #34146 | CONFLICT | no rollup | report only: dirty lineage; stale-result typing fix implemented on main |
| #25403 | CLEAN | no rollup | no mutation; superseded by newer #34293 |
| #25397 | CONFLICT | no rollup | report only: superseded by refreshed #34298 |
| #25396 | CONFLICT | no rollup | report only: superseded by clean newer #34292 |
| #24599 | CLEAN | no rollup | no mutation; superseded by newer #34297 |
| #24586 | CONFLICT | no rollup | report only: superseded by clean newer #34299 |
| #23331 | CLEAN | 31 / 0 / 0 | rebased/resolved/pushed `d8c98f48fc`; 165 pass / 1 skip |

NEXT: Apollo senior review of the final sweep mutations. Watch only the fresh pending CI on #62925, #60253, #47017, #37513, #37418, #37381, and #34294; do not merge upstream. #63082/#63096 are green and remain upstream-maintainer decisions.
