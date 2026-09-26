# CI cloud overflow — runbook and rollback (as built 2026-09-25)

Spec: `~/.hermes/plans/2026-09-21_ci-cloud-overflow-SPEC.md` (v1.3 + "As built 2026-09-25").
Program card t_af9552d9; this doc is its AC8. Every claim below cites a deploy receipt:
merge sha + controller pin + controller restart/heartbeat.

## Moving parts

| Part | Where | What it does |
|---|---|---|
| Request producer | this repo, `scripts/ci_overflow_request.py` (`Python tests / Generate slices`) | Uploads `ci-overflow-request-<run_id>-<attempt>` for every `merge_group` run. |
| Planner / ledger (P1) | this repo, `scripts/ci_overflow_plan.py`, `scripts/ci_overflow_ledger.py` | Pure plan + CAS ledger on branch `ci-overflow-ledger`, file `state.json`. |
| Controller | Kyzcreig/fleet-ops-scripts `ci-overflow-controller/` (vendors P1 by `PIN.json`) | Studio LaunchAgent `ai.hermes.ci-overflow-controller`; admits, reconciles, pages. State in `~/.hermes/var/ci-overflow/`. |
| Placement consumer | this repo, `scripts/ci_overflow_placement.py` (`Python tests / Placement`) | Runs only when `CI_OVERFLOW_PLACEMENT_ENABLED == 'true'`; reads the plan, else falls back. |
| Routing vars | repo variables on ANG-Ventures/hermes-agent | Written ONLY through `~/.hermes/scripts/ci_routing_ledger.py set <repo> <VAR> <value> --who … --why '<reason + rollback>'` (ledger `~/.hermes/state/ci-routing-vars.jsonl`). |

P1 changes land here first (fork-first), then fleet-ops-scripts re-pins `PIN.json`
(`source_sha` + sha256 of each vendored file) and the controller is kickstarted.

## What changed on 2026-09-25 (receipts)

Restart rows are the first `ticks.jsonl` row of each new controller pid (UTC).

| Change | Merge | Controller restart | Pin |
|---|---|---|---|
| Release reservations of skipped-Placement attempts; no admission while placement != `true`; `--release-unused-plans` backfill | fos#52 `0f260ce4b4` 14:07:25 | pid 64973 @ 14:07:35 | `9f6ab6903c` |
| Charge-exact compaction fold (terminal row folds into `daily_totals[admitted_on]` when `terminal_on < today` or `== admitted_on`) | #1083 `0f70e298b7` 18:59:29; fos#60 `20b9ec3fd7` 18:46:57 (PR head), fos#62 `21442292f0` 19:50:04 (squash, bytes unchanged) | pid 88301 @ 18:47:05; pid 96009 @ 19:50:56 | `b49030ccc2` → `0f70e298b7` |
| Billed-minute charging (`hosted_minutes = ceil(completed_at − started_at)`, remainder returned) + fold on every admission/reconcile | #1125 head `d9ecae9da0` (**PR still OPEN**); fos#66 `95d7720e06` 22:34:59 | pid 79192 @ 22:36:28 | `d9ecae9da0` |
| Sampler: memoize only successes, retry once, ≤2-tick stale, `blind-while-saturated` incident, tick log | fos#67 `af7e1531f6` 22:46:06 | pid 37890 @ 22:46:38 | `d9ecae9da0` |
| Batched admissions: one `state.json` CAS PUT per tick | fos#68 `0cc7bb6c74` 23:45:30 | pid 36499 @ 23:45:41 | `d9ecae9da0` |

Heartbeat 2026-09-26T00:55:04Z: pid 36499, pin `d9ecae9da0b0dc44f0cddf4915b956b50ce3272d`,
`error null`, `admission_failures 0`, `reconcile_failures 0`, `placement_enabled "false"`.

Not yet deployed (do not assume otherwise):
- **slice_count pin → derived** (t_89964c9c, in flight): the controller will derive the merge_group
  `slice_count` from ci.yaml at the request's ref, with `config.json` as a ceiling. Until then
  `config.json` pins 8, and a ci.yaml `slice_count` change needs a fleet-ops-scripts config PR **and a
  kickstart** (config is read at process start: fos#54 merged 15:48:45Z but id-domain refusals ran until
  the 18:47:05Z restart — 63 `request-refused` episodes 11:26Z–18:45Z).
- **Blacksmith third rung** (t_6804be8c): #1147 (generator venue, `CI_BLACKSMITH_SLICES`) and
  hermes-home#761 (ci-placement signal + spend monitor) are OPEN. `CI_BLACKSMITH_SLICES=4` is set
  (ledgered 23:48:39Z) but only #1147's tests.yml reads it.
- **#1125 merge**: when it merges, bump fos `PIN.json` `source_sha` to the squash sha (same shape as fos#62).

## Operating

Health (read-only):

    cat ~/.hermes/var/ci-overflow/heartbeat.json          # pid, pin, *_failures, placement_enabled
    tail -3 ~/.hermes/var/ci-overflow/ticks.jsonl          # per-tick; non-fresh snapshots logged with errors
    tail ~/.hermes/var/ci-overflow/launchd.err.log
    sqlite3 ~/.hermes/var/ci-overflow/outbox.sqlite \
      "select kind,count(*) from episodes where opened_at>strftime('%s','now','-1 day') group by kind"
    python3 ~/.hermes/scripts/ci_routing_ledger.py last    # who set which routing var, why

Deploy a controller change: merge in fleet-ops-scripts → `git -C ~/dev/fleet-ops-scripts pull --ff-only`
→ `launchctl kickstart -k gui/502/ai.hermes.ci-overflow-controller` → confirm a new pid with
`event: start` in `ticks.jsonl` and a clean heartbeat. The heartbeat `pin` re-reads `PIN.json` from disk,
so it is not proof the new code is loaded; the restart row is.

Budget reading: `daily_totals[<UTC day>]` in `state.json` on `ci-overflow-ledger` is the day's charge
(default allowance 6000). A plan whose jobs all read `budget-overrides-cloud-only` with 0 reserved minutes
is the designed "budget spent → local" fallback, not a missing plan.

## Rollback

All variable writes go through `ci_routing_ledger.py set … --who … --why '<reason; rollback = …>'`.

1. **Stop managed placement (fast, reversible):** `CI_OVERFLOW_PLACEMENT_ENABLED=false`. merge_group then
   uses the legacy matrix, which honours `CI_RUNNER_LABELS`. The controller stops admitting (attempts are
   recorded `unmanaged`) and reconcile releases reservations of attempts whose Placement was skipped.
   The heartbeat monitor pages #alerts after 15 min in this state — expected during a stopgap.
   Live now: set false 2026-09-25T23:52:20Z by Apollo as the t_89964c9c stopgap; rollback = `true` once
   that fix is deployed and a merge_group run shows `plan_valid=true`.
2. **Mode:** `CI_CLOUD_OVERFLOW=self-only` keeps admissions/ledger (never erase evidence) and places no cloud.
3. **Controller code:** revert the fleet-ops-scripts merge (or check out the previous sha in
   `~/dev/fleet-ops-scripts`) and kickstart. A pin rollback restores the previous `PIN.json` + vendored
   bytes together; never hand-edit `vendor/`.
4. **Controller off:** `launchctl bootout gui/502/ai.hermes.ci-overflow-controller` (literal label; the
   scheduler guard refuses `$C`). With no controller there is no plan; Placement times out and falls back,
   so do step 1 first.
5. **Ledger capacity emergency:** `python3 -m cioc.service --release-unused-plans --admitted-on <day> --dry-run`
   first; run before any fold-changing deploy (the first post-deploy reserve() folds same-day rows).
