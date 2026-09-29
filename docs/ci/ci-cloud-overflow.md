# CI cloud overflow: runbook and rollback (as built 2026-09-29)

Audience: the operator (Apollo) who runs, changes or rolls back merge_group CI routing for
`ANG-Ventures/hermes-agent`. Every value below was read on 2026-09-29 between 03:40Z and 04:10Z
from the file, repo variable or command named next to it. Where the live system and `main`
disagree, both are stated.

Replaces the unmerged 2026-09-25 draft (#1179). Background and the full decision log live in the
Obsidian note "CI Routing — How It Works" and in
`~/.hermes/plans/2026-09-27_ci-placement-event-driven-admission-SPEC.md` §17.

## Venue policy (Ace, 2026-09-28 17:57 PT)

- **Public repos (this repo):** GitHub-hosted runners, which are free for public repos. Overflow
  ladder, in order: GitHub-hosted → Blacksmith (the saturation valve below) → the local CI box
  once it exists (card `t_e3e80681`). Overflow is expected to be rare.
- **Private repos:** Blacksmith only. There is no overflow venue: never GitHub-hosted, never
  local runners.

The local pools (ACE-AI, ACE-MEDIA) run no CI for this repo. See `docs/ci/runner-venues.md` for
what may bring them back.

## Moving parts

| Part | Where | What it does |
|---|---|---|
| Request producer | `scripts/ci_overflow_request.py` (`Python tests / Generate slices`) | Uploads `ci-overflow-request-<run_id>-<attempt>` for each managed `merge_group` run. |
| Planner and ledger (P1) | `scripts/ci_overflow_plan.py`, `scripts/ci_overflow_ledger.py` | Pure plan, and a CAS ledger: branch `ci-overflow-ledger`, file `state.json`, `HARD_LIMIT = 500 * 1024` bytes. |
| Controller | `ANG-Ventures/fleet-ops-scripts` `ci-overflow-controller/`, LaunchAgent `ai.hermes.ci-overflow-controller` on the Mac Studio, run from `~/dev/fleet-ops-scripts` | Discovers runs, admits a plan, reconciles, pages. Vendors the P1 files by `PIN.json`. State in `~/.hermes/var/ci-overflow/`. |
| Placement consumer | `scripts/ci_overflow_placement.py` (`Python tests / Placement`) | Reads the ledger every `POLL_INTERVAL_S = 2` s for up to `POLL_DEADLINE_S = 180` s. No valid plan means `plan_valid=false` and the static fallback split. |
| Blacksmith valve | hermes-home `scripts/ci-placement.py`, cron job `ci-placement` (`*/5 * * * *`) | Sole writer of `CI_BLACKSMITH_SLICES`. |
| Routing-variable writer | hermes-home `scripts/ci_routing_ledger.py` (live copy `~/.hermes/scripts/ci_routing_ledger.py`) | The only sanctioned way to write a routing variable. Appends to `~/.hermes/state/ci-routing-vars.jsonl`. |
| Synthetic bench | `.github/workflows/ci-placement-probe.yml` + fos `tools/p4_bench.py`, `tools/p4_gates.py` | `workflow_dispatch` probe of the Placement exchange only. The controller admits probes as synthetic: 0 reserved minutes, no ledger or budget charge. |

P1 changes land here first. fleet-ops-scripts then re-pins `PIN.json` (`source_sha` plus the
sha256 of each vendored file) and the controller is restarted. Never hand-edit `vendor/`.

## Live state (read 2026-09-29)

Repo variables (`gh api repos/ANG-Ventures/hermes-agent/actions/variables`):

| Variable | Value | Last updated (UTC) |
|---|---|---|
| `CI_OVERFLOW_PLACEMENT_ENABLED` | `true` | 2026-09-26 05:16:35 |
| `CI_CLOUD_OVERFLOW` | `cloud-only` | 2026-09-27 15:51:43 |
| `CI_RUNNER_LABELS` | `["ubuntu-latest"]` | 2026-09-27 15:51:44 |
| `CI_SELF_HOSTED_SLOTS` | `0` (`CI_SELF_HOSTED_SLOTS_BASELINE=9`) | 2026-09-27 15:51:45 |
| `CI_CLOUD_MINUTES_PER_DAY` | `30000` | 2026-09-26 04:16:46 |
| `CI_ARM_HOSTED_SLICES` | `3` | 2026-09-21 01:50:32 |
| `CI_BLACKSMITH_SLICES` | `0` | 2026-09-28 01:05:39 |
| `CI_MERGE_GROUP_SLICES` | unset | n/a |
| `CI_BLACKSMITH_ARMED` | unset (lane armed) | n/a |

Controller (`heartbeat.json` and the last `ticks.jsonl` row): pid 25551, started
2026-09-28 18:36:41 PT (`ps -o lstart`), checkout `~/dev/fleet-ops-scripts` at `a2e779d`
(fos#136), pin `c93291f9831d9f3244d89e722db48b264668cce4` (#1437), `error null`,
`admission_failures 0`, `reconcile_failures 0`, `placement_enabled "true"`, `daily_limit 30000`,
`remaining_allowance 24910`, `ledger_bytes 26899` of `ledger_soft_limit 409600`, `commit_lost 0`.

**Drift:** the checkout has one uncommitted edit, `config.json` `wake.enabled: "shadow"`. `main`
still ships `false` (fos#119). The live controller runs shadow (`heartbeat.json` `.wake.mode`).
A `git pull` that touches `config.json` can conflict, and a clean checkout would silently turn the
wake tailer off. Land the ruling as a fleet-ops-scripts PR.

## Slice count: derived, not pinned

`config.json` no longer pins `slice_count` (fos#69, 2026-09-26). The controller reads `ci.yaml` at
the attempt's own head SHA and evaluates `slice_count:` for `merge_group`, resolving `vars.*` from
the same repo-variable read the tick already does (fos#78). `config.json` keeps only the ceiling,
`max_slices: 17`, and `max_reservation: 615`. A slice count above the ceiling is refused.

`ci.yaml` today: `merge_group` runs `CI_MERGE_GROUP_SLICES` clamped to 4..16, and **16 when the
variable is unset** (t_21d0a6cf, 2026-09-26; it was 8 before). Other events run 16. A `ci.yaml`
change to the slice count needs no controller change or restart.

## Hosted allowance

`CI_CLOUD_MINUTES_PER_DAY=30000` (raised from 6000). The controller reads it on every tick
(`cioc/service.py`: absent or non-decimal reads as 0 and logs a diagnostic). On this public repo
GitHub-hosted minutes bill $0, so the allowance is a throughput guard, not money. When it is
spent, a `cloud-only` plan marks its jobs `budget-overrides-cloud-only` with 0 reserved minutes;
that is the designed fallback, not a missing plan. Read the day's charge in `state.json` on
`ci-overflow-ledger` (`daily_totals[<UTC day>]`) or `remaining_allowance` in `ticks.jsonl`.

## Blacksmith valve

`ci-placement.py` (hermes-home, Blacksmith section, `BS_*` constants) writes
`CI_BLACKSMITH_SLICES`. Nothing else writes it; the controller only reads it.

It writes `BS_SLICES_ON = 4` when all of these hold:

1. A hosted-saturation signal. Any one is enough. Counts are this repo's GitHub-hosted jobs only
   (the 120-runner cap is org-wide, so this under-counts and errs toward not paying):
   - hosted queue-wait p90 over the last 30 min `> 60` s (`BS_QUEUE_P90_S`);
   - hosted jobs `in_progress` `>= 110` (`BS_HOSTED_CONCURRENCY`; the cap is 120);
   - `>= 16` hosted jobs queued for `>= 45` s (`BS_QUEUED_DEPTH`, `BS_QUEUED_MIN_WAIT_S`);
   - **any one** hosted job queued `> 120` s (`BS_QUEUE_AGE_S`, hermes-home#1584). A job still
     queued is aged `now - created_at`, because GitHub sets `started_at = created_at` while a job
     is queued. The code comment records why 120: a normal 4 h window (09-28 17-21Z, 2,821 hosted
     jobs) peaked at 73 s, and the real 09-26 saturation had 351 of 2,167 jobs over 120 s.
2. Today's Blacksmith billed minutes are under `BLACKSMITH_MINUTES_PER_DAY` (default
   `BS_CAP_DEFAULT = 6000`, 2-vCPU basis; a 4-vCPU minute bills 2).
3. The lane is armed: `CI_BLACKSMITH_ARMED` is not `0`.

After the last signal it holds 4 for `BS_HOLD_S = 30 min`, then writes 0 (hermes-home#1331). It
fails closed: any unreadable input writes 0 and exits nonzero. A Blacksmith ARM job in the tick
forces 0 (x64 only).

The controller puts that many slices into each `merge_group` plan as `blacksmith-4vcpu-ubuntu-2404`
(reserved non-core x64 slices; fos#124, #1412). Placement runs the plan as written, so the
plan-equals-execution check covers Blacksmith slices too. #1437 recounts `summary.blacksmith`
after budget demotions. `CI_BLACKSMITH_ARMED=0` also zeroes the share inside the controller.

Firings in the routing ledger (`grep CI_BLACKSMITH_SLICES ~/.hermes/state/ci-routing-vars.jsonl`):
0→4 at 2026-09-27 23:45:46Z ("hosted concurrency 120 >= 110"), back to 0 at 23:51:08Z; 0→4 at
2026-09-28 01:00:49Z ("hosted concurrency 115 >= 110"), back to 0 at 01:05:39Z. Both predate the
hold (#1331) and the queue-age trigger (#1584). No firing since, so the valve has not yet sent a
merge_group slice to a real Blacksmith runner.

## Ledger CAS and the tip PUT

The ledger is `state.json` on branch `ci-overflow-ledger`, written only by the controller with a
Contents-API compare-and-swap on the blob SHA. A 409 means someone else wrote first; the
controller re-reads and replays. Admissions are batched: one CAS PUT per tick (fos#68).

- fos#126: ledger reads go by branch tip; no Contents read while the tip is unchanged.
- fos#132: the admission commit PUTs from the ledger tip the controller last wrote, skipping the
  `git/ref` GET. Blob-SHA CAS is unchanged. Knob `ledger_put_from_tip` (default `true`). Adds the
  `ledger_read`, `ledger_put` and `paced_s` tick fields. Before-table on the live controller
  (t_bd45626d, n=125): tick→CAS p50 5.885 s / p95 12.198 s; largest stage `ledger_commit` p50
  2.813 s. No after-table was recorded on that card.
- fos#133: a failed PUT keeps the stored tip; a 5xx that did not land replays the same commit.

## Event-driven wake (`wake.enabled`)

`cioc/wake.py` tails `~/.hermes/var/gh-webhook/queue.jsonl` (written by hermes-home
`gh-webhook-front.py`, fields kept since hermes-home#1273) and nominates a run when its
`Generate slices` check run completes. Admission still comes only from live GitHub reads in
`admit()`; polling runs either way.

| Value | Effect |
|---|---|
| `false` | Tailer not started. |
| `"shadow"` | Tailer counts nominations in `heartbeat.json` `.wake`, never wakes the controller or adds a run. |
| `true` | Tailer wakes the controller and nominates runs. |
| anything else | Read as `false`, logs a `wake-config` line. |

The value is read at process start only; change it, then restart.

**Ruling: shadow.** Phase 3 A/B (t_2e0647ee, 2026-09-28, 100/100 probe runs, controller with
`enabled: true`): Hodges-Lehmann saving 1.7 s, 95 % CI [-0.5, 3.9], p = 0.14. The lower bound
crosses zero, so Apollo ruled `"shadow"` at 12:51 PT. Live read 2026-09-29: `mode "shadow"`,
`alive true`, `nominated 84`, `parse_fail 0`, `last_delivery_lag_s 1.612`.

## P4 gates and the latest bench

`tools/p4_gates.py` computes every gate for a UTC window (`--synthetic --wave W` for bench runs):

| Gate | Pass condition |
|---|---|
| `cb5_latency` | artifact → Placement `plan_valid=`: n ≥ 20, p95 ≤ 30 s, max ≤ 60 s |
| `rc11_observed` | peak rolling-hour controller requests < 50 % of the 5000/h installation limit |
| `saturated_wave` | ≥ 1 run admitted on a saturated snapshot, every such plan fully hosted |
| `ac6_mixed` | plan == execution on every non-cancelled run |
| `budget` | projected billed day < daily limit, and remaining allowance never reached 0 |
| `ledger_incidents` | ledger < `HARD_LIMIT`, 0 state-capacity, 0 secondary 429s |
| `job_to_plan` | Placement job created → `plan_valid=` ≤ 10 s (stricter residual, not the GO gate) |

P4 was ruled **GO** on 2026-09-27 19:44 PT (window 5: burst p95 29.1 s / max 30.7 s, spread p95
18.2 s / max 20.3 s, RC11 18 %).

Re-bench 2026-09-28 17:46 PT (t_ed1c7c8e, controller pid 63128 on fos#134 `ed70406`):

| Wave | Planned | Median | p95 | Max | RC11 peak |
|---|---|---|---|---|---|
| burst | 19/20 | 15.4 s | 20.5 s | 20.5 s | 12.7 % |
| spread | 20/20 | 13.7 s | 17.6 s | 18.0 s | 20.5 % |

Spread passes CB5. Burst did not: probe #12 (run 36504787098) was refused `synthetic-wave` on a
stale queued-listing title and timed out at 186.7 s with `plan_valid=false`. fos#136 (t_ee5ca708)
now decides that refusal on a fresh per-run read and is live (pid 25551). No bench has run on
fos#136 yet, so burst CB5 on the live controller is unmeasured.

## Operating

Read-only health:

    jq '{pid,pin,error,placement_enabled,admission_failures,reconcile_failures,wake}' \
      ~/.hermes/var/ci-overflow/heartbeat.json
    tail -1 ~/.hermes/var/ci-overflow/ticks.jsonl        # daily_limit, remaining_allowance, commit_lost
    tail ~/.hermes/var/ci-overflow/launchd.err.log
    python3 ~/.hermes/scripts/ci_routing_ledger.py last   # who set which routing var, and why
    git -C ~/dev/fleet-ops-scripts status --short          # expect only the wake drift above

Deploy a controller change: merge in fleet-ops-scripts, `git -C ~/dev/fleet-ops-scripts pull
--ff-only`, then `launchctl kickstart -k gui/502/ai.hermes.ci-overflow-controller`. Confirm a new
pid and a clean heartbeat. The heartbeat `pin` re-reads `PIN.json` from disk, so it does not prove
the new code is loaded; the new pid's `event: start` row in `ticks.jsonl` does. Do not deploy
controller changes while an A/B is measuring the live controller.

## Rollback, per knob

Every repo-variable write goes through the ledger writer, the sole writer:

    python3 ~/.hermes/scripts/ci_routing_ledger.py set ANG-Ventures/hermes-agent <VAR> <value> \
      --who <agent/session> --why '<reason, card, rollback>'

It guards `CI_RUNNER_LABELS`, `CI_OVERFLOW_PLACEMENT_ENABLED`, `CI_MERGE_GROUP_SLICES` and
`CI_BLACKSMITH_SLICES`; any other variable needs `--allow-unguarded`. A bare `gh api` PATCH pages as
unattributed.

| Knob | Rollback | Effect |
|---|---|---|
| `CI_OVERFLOW_PLACEMENT_ENABLED` | `false` | Controller stops admitting; Placement finds no plan and `merge_group` uses the static fallback split. The heartbeat monitor pages after 15 min in this state. |
| `CI_CLOUD_OVERFLOW` | `self-only` (unguarded) | Admissions and ledger continue; the planner puts every slice on the self-hosted pool label. With the local runners offline those jobs queue, so use it only with the pool back. |
| `CI_CLOUD_MINUTES_PER_DAY` | previous value (unguarded) | Allowance change takes effect on the next tick. |
| `CI_MERGE_GROUP_SLICES` | `8`, or unset for 16 | Picked up per run from `ci.yaml`; no restart. |
| `CI_BLACKSMITH_ARMED` | `0` (unguarded) | Kill switch. `ci-placement.py` holds `CI_BLACKSMITH_SLICES` at 0 and logs what it would have written; the controller also plans 0 Blacksmith slices. |
| `CI_BLACKSMITH_SLICES` | do not hand-write | `ci-placement.py` overwrites it every 5 min. Use `CI_BLACKSMITH_ARMED=0`. |
| `wake.enabled` | `false` in fos `config.json`, then kickstart | Tailer off. Not a repo variable; land the change as a fleet-ops-scripts PR. |
| `ledger_put_from_tip` | `false` in fos `config.json`, then kickstart | Back to a `git/ref` GET before each PUT. |
| Controller code | revert the fleet-ops-scripts merge, pull, kickstart | A pin rollback restores `PIN.json` and the vendored bytes together. |
| Controller off | `launchctl bootout gui/502/ai.hermes.ci-overflow-controller` | No plans; set `CI_OVERFLOW_PLACEMENT_ENABLED=false` first so Placement does not wait 180 s per run. |

## Receipts

All merged unless noted (`gh api repos/ANG-Ventures/<repo>/pulls/<n>`, read 2026-09-29).

| PR | Merged (UTC) | Change |
|---|---|---|
| hermes-agent#1083 | 2026-09-25 18:59 | Fold terminal ledger rows once charge-exact |
| hermes-agent#1125 | 2026-09-26 04:37 | Charge hosted jobs their billed minutes |
| hermes-agent#1147 | 2026-09-26 00:51 | Blacksmith as the paid third venue rung in the slice generator |
| hermes-agent#1117 | 2026-09-26 05:40 | merge_group slice count follows `CI_MERGE_GROUP_SLICES` |
| hermes-agent#1175 | closed, not merged | No-plan fallback to hosted; #1177 (merged 2026-09-26 04:20) shipped the static-split fallback instead |
| hermes-agent#1412 | 2026-09-28 16:36 | merge_group plan names the Blacksmith share |
| hermes-agent#1437 | 2026-09-28 18:20 | Ledger recounts `summary.blacksmith` after budget demotions (current pin) |
| fleet-ops-scripts#69 | 2026-09-26 01:29 | Derive merge_group slice count from `ci.yaml` at head SHA |
| fleet-ops-scripts#73 | 2026-09-26 01:37 | `tools/p4_gates.py` |
| fleet-ops-scripts#78 | 2026-09-26 03:29 | Evaluate the #1117 slice-count expression |
| fleet-ops-scripts#112 | 2026-09-28 01:04 | Conditional GETs, imminent-only prewarm (P4 GO build) |
| fleet-ops-scripts#119 | 2026-09-28 04:07 | Wake tailer, ships `wake.enabled=false` |
| fleet-ops-scripts#124 | 2026-09-28 20:30 | Plan the Blacksmith share for merge_group |
| fleet-ops-scripts#126 | 2026-09-28 14:12 | Ledger reads by branch tip |
| fleet-ops-scripts#127, #129, #130 | 2026-09-28 18:47–19:43 | Wake A/B tooling and two wake-miss fixes |
| fleet-ops-scripts#131 | 2026-09-28 21:20 | Nominee admissible only while queued or in_progress |
| fleet-ops-scripts#132 | 2026-09-28 21:20 | Admission PUT from own ledger tip + stage telemetry |
| fleet-ops-scripts#133 | 2026-09-28 22:44 | Failed PUT keeps the stored tip |
| fleet-ops-scripts#134 | 2026-09-29 00:03 | Discover runs queued between producer and Placement |
| fleet-ops-scripts#135 | 2026-09-29 00:56 | Self-healing incidents page only past a 5-min grace |
| fleet-ops-scripts#136 | 2026-09-29 01:36 | Synthetic-wave refusal on a fresh per-run read (live checkout) |
| hermes-home#1273 | 2026-09-28 07:13 | Webhook front keeps `check_run` fields the tailer needs |
| hermes-home#1331 | 2026-09-28 08:21 | Valve: queued-depth lead, 30-min hold, warm-up bound |
| hermes-home#1584 | 2026-09-29 03:29 | Valve: correct queued-job ages + one-job queue-age trigger |
