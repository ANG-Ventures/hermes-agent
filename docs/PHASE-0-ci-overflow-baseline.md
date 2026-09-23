# CI overflow Phase 0 — integration capability baseline

Observed 2026-09-23 UTC. Target: public `ANG-Ventures/hermes-agent` (not NousResearch or Kyzcreig). Machine-readable evidence: [`receipts/preflight.json`](../receipts/preflight.json). A BLOCK is an activation stop; UNVERIFIABLE is never treated as PASS. The preflight command exits 1 while any check is not PASS. No runner, repo variable, App, live host cache, or main workflow was modified. The disposable data-only probe branch was deleted after checking its run count.

| Check | Verdict | Evidence / exact reason |
|---|---|---|
| Routing owner and workflow SHA | PASS | Main HEAD `4a8affef734e2e90cce7a9cc3bad1e53c90ccf1d`; `tests.yml` blob SHA and open PR inventory in receipt. PR [#929](https://github.com/ANG-Ventures/hermes-agent/pull/929) modifies `scripts/run_tests_parallel.py`, a live routing collision to coordinate, not one of the unrelated survivor/usage cards named in the spec. |
| §5.3b call-site and producer | PASS | `ci.yaml` has one `tests.yml` call (`jobs.tests`), merge_group declared; numeric workflow ID `340224912`; successful merge_group run/job ID and exact `Python tests / Generate slices` name in receipt. |
| RC8 Actions output budget | PASS (measured estimate) | Generated real 16-slice full and 2-slice `plugin:memory` matrices on this checkout with current routing args; modelled local_matrix twin and placement output using UTF-16LE bytes including `key=value\n`. Full: generate **710,442 B** (<768,000 B/job, margin 57,558 B), placement **354,892 B**, combined **1,065,334 B** (<41,943,040 B/run). Scoped: generate **7,118 B**, placement **3,608 B**, combined **10,726 B**. The proposed new outputs do not yet exist in production; this is a capacity estimate, not an observed Actions-output emission. |
| Ledger-branch data-only push | PASS (disposable branch) | Inventory of every current `on: push`/`workflow_run` is in receipt. Pushed `state.json` only as `4a8dbc7c3b05976557afe94f5b680dd0fdf32303` to `ci-overflow-ledger-probe`; Actions API returned zero runs for that head SHA. Deleted the branch. This demonstrates the current probe branch, not a future ledger ruleset or rewritten triggers. |
| Controller App | BLOCK | Org installations API returned no `ci-overflow-controller`. See Apollo card `t_73f36116`. No App was created. |
| Contents CAS + GITHUB_TOKEN denial | UNVERIFIABLE | Intended App identity and ledger-branch ruleset are absent. No claim about GitHub Contents CAS or ruleset denial follows from an unauthenticated/synthetic test. |
| App runner read + fork isolation | UNVERIFIABLE | Intended App identity absent; no App-scoped runner read or fork-event credential boundary demonstrated. |
| Standard hosted x64 e2e | BLOCK | Current `tests.yml` e2e is `workflow_call` only; its `runs-on` resolves to self-hosted/X64 under current `CI_RUNNER_LABELS`. No `workflow_dispatch` override exists on a branch and no hosted e2e run URL/result was produced. Phase 2 must add reviewed branch wiring and run the actual job before activation; an unrelated local e2e test is not a substitute. |
| Cache consumers / XFS | UNVERIFIABLE | Read-only SSH to ACE-AI and ACE-MEDIA confirmed `/srv/ci` XFS, `reflink=1`; Docker inspected all containers and recorded every matching bind, with seed byte counts and SHA-256 per-file manifests in receipt. ACE-AI pip seed 111,187,447 B / 51 files; ACE-MEDIA empty. Unit-name and `/etc/cron.d` path scans are not exhaustive warmer/pruner and user-cron/unit-content evidence. Do not migrate on this inventory. |
| RC11 rate headroom | UNVERIFIABLE | `720*D + W*S + 12*T + P` is recorded but D/W/S/T/P need 20 actual exchanges. App installation hourly limit cannot be read while App absent. No invented headroom number. |

## Falsified or unproven spec assumptions

1. The expected App does **not** exist; App-dependent Phase 0 security assertions cannot run.
2. There **is** a current routing-owner collision: PR #929 touches the generator, contrary to treating the routing path as uncontended. Coordinate integration against that PR's head/merge.
3. Cloud e2e portability is **not demonstrated** by the existing job; `workflow_dispatch` does not reach `tests.yml` directly and repository routing vars select self-hosted. The prescribed hosted run requires reviewed temporary wiring.
4. The prior seed observation (107 MB / 20 KB) is stale: current measured ACE-AI pip seed is 111,187,447 B; ACE-MEDIA's seed directories contain zero files. No inference about future warmer writes.
5. RC8 headroom is measured against current generator output, not a deployed placement implementation. Rerun against the exact Phase 2 emitter before activation, especially with only ~57 KiB margin for generate.

## Follow-on gates before activation

Apollo provisions the App/ruleset, then repeat CAS contention, GITHUB_TOKEN denial, runner read and fork negative test using the intended identity. Phase 2 creates the hosted e2e branch path and records its actual run URL/conclusion. Cache relocation requires a complete per-host warmer/pruner and unit-content inventory. RC11 waits for 20 real exchanges and installation-specific hourly limits. No result here authorizes switching `CI_CLOUD_OVERFLOW` from self-only.
