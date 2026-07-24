# Fork Parity Doctrine

## Problem
Parity cost is superlinear in drift (datapoints: 1216 behind→147 hunks; 1778→912 —
n=2, confounded by absorption events; treat as directional until calibration card
t_c4376da6 adds points). Upstream measured velocity ≈ 174–222 commits/day.

## Doctrine

### D1 — Drift-triggered syncing
Daily watch (cron `parity-drift-watch`, 07:30 PT). Bands in DAYS-EQUIVALENT of drift:
`days_equiv = behind ÷ velocity`, where **velocity = trailing-14-day upstream commit
rate measured from upstream's own log** (`git rev-list --count --since="14 days ago"
origin/main` ÷ 14), clamped to MIN 50/day; an EMPTY window uses the floor (50/day) — never a higher prior (RC-a).
Velocity is independent of `behind` — the ratio cannot collapse into a calendar timer (CB1).
Band table (ILLUSTRATIVE, NON-NORMATIVE — skill `fork-parity-doctrine` §D1 is the
single authoritative copy; calibration edits the skill only — RC-β):
- GREEN  (< 3 days-equiv): positive heartbeat line to #logs (never silent-silent).
- YELLOW (3–5 days-equiv): #logs advisory "plan a full sync this week."
  NO automated mutation in v0.5.
- RED    (> 5 days-equiv): page #alerts → start a full sync now.
- DEGRADED (fetch failed — see D1a): #logs error only, never classified GREEN; paging
  for a persistently-degraded watch is owned by the liveness check (RC-3).
Bands live ONLY in the skill (D1b); PROVISIONAL until calibration.

#### D1a — Liveness (B1, RC1, CB-A)
The watch writes TWO state files:
- `~/.hermes/state/parity-drift-watch.last-run` — EVERY run, any band (cron liveness).
- `~/.hermes/state/parity-drift-watch.last-success` — ONLY after a successful
  `git fetch` (data freshness).
Fetch failure ⇒ band=**DEGRADED, never GREEN** (a blind watch must look blind), the
real error posts to #logs, and `last-success` is NOT updated.
The **daily** `no_agent` staleness check (`parity-drift-watch-liveness`) alerts #alerts
when **`last-success`** is >48h old — keyed on data freshness, NOT cron liveness, so a
dead watch AND a running-but-fetch-failing watch both page within 48h (weekly cadence
would give a ~7-day blind spot exceeding the RED bound — RC1). **Absent `last-success`
(fresh deploy, state-file loss) = STALE → page (RC-1)** — a watch that has never
successfully fetched must not stay dark; the liveness script's missing-file branch is
empirically verified (missing → pages, stale+fresh-run → pages, fresh → silent).
Green is observably green; a dark drift signal pages.

#### D1b — Single source of thresholds
Canonical bands: skill `fork-parity-doctrine` §D1. The cron prompt contains NO numeric
bands — it loads the skill (cron `skills` param) and applies its current table. Spec
mirrors the skill; postmortem calibration edits the skill only.

#### D1c — catchup (demoted from auto)
`hermes_parity catchup` remains the <50-commit post-sync drift closer, run manually by
Apollo after a sync lands. Precise eligibility predicate, no-op-on-conflict + loud
abort + revert command spec'd on card t_c4376da6 BEFORE any future re-automation.
Unattended tree mutation is out of scope for v0.2 entirely.

### D2 — Absorption = ownership transfer
1. Upstream copy becomes canonical; adopt as base at next sync.
2. Re-apply only verified fork deltas; PR them upstream immediately.
3. Registry: `lifecycle: "absorbed"` + `upstream_ref` + `absorbed_date`.
4. **Canary tests GATE (RC):** absorbed entries keep their `tests` nodeids in
   fork-features.json, which the gates manifest stage executes — a red canary FAILS the
   sync's gates ladder. This is enforced by the existing manifest+forkdelta stage
   (nodeids run directly via venv pytest); absorbed entries are never pruned from the
   manifest without an explicit `ack --reason`.

#### D2a — Mixed files (RC)
Lifecycle is FEATURE-granular; conflicts are FILE-granular. When one file carries both
an absorbed feature and fork-permanent seams (e.g. compute_host.py: absorbed feature +
fleet auth/approval hooks), resolution = adopt upstream base for the absorbed feature,
then RE-THREAD each fork-permanent seam listed in fork-features.json `paths`, verified
by that entry's gating tests. "Take upstream's version" is never license to drop a
fork-permanent seam sharing the file.
**Seam-completeness audit (RC2):** before adopting upstream base on a mixed file, grep
the FORK side for fork-seam markers (fork_ext imports, fleet-auth/approval identifiers)
and diff the hits against the entry's registered `paths` — any UNLISTED seam BLOCKS the
resolution until it is registered or explicitly acked.
**Seam-ack governance (RC-b):** a seam-ack carries the same bar as a canary prune —
the `ack --reason` posts to #alerts naming a reviewer; never a quiet single-actor
dismissal. (Known residual: the audit is only as complete as its marker set.)

### D3 — Parallel invention: converge unless contracted delta
Default converge on upstream. Exception requires a TEST contracting the delta —
**and (RC/QA lens) the absorption sweep audits that every fork-permanent feature's
`tests` list is non-empty and RED-proofed**; an uncovered delta must gain a test or is
presumed droppable. Full parallel impls only for `fork-permanent` fleet-private work.

### D4 — Upstream-first authoring
Build vs upstream HEAD → PR NousResearch → carry on fork while pending
(`upstream-intended` + PR link). Never self-merge upstream. Permanent delta shrinks.

### D5 — Extraction debt is parity debt
Finish rank-1 gateway/run.py (84 hunks this sync) + rank-2 hermes_state.py fork_ext
extractions. New inline fork edits to god-files need a written why-not-fork_ext.

## fork-features.json schema v2 (B5 hardened)
Fields: `lifecycle` ∈ {upstream-intended, fork-permanent, absorbed}, `upstream_ref`
(URL|null), `absorbed_date` (ISO|null).
- **Legacy default:** entries missing `lifecycle` are treated as `fork-permanent`
  (the safe default — maximum protection) by all consumers.
- **Lint rollout:** warn-only for missing lifecycle in the first release; strict
  (fail) only after the migration card lands all entries. Enum violations always fail.
- **Rollback:** schema change is one commit touching fork-features.json +
  lint-manifest; revert = `git revert <sha>` (consumers tolerate v1 via legacy default).
- **State machine (RC):** upstream-intended →(upstream PR merges)→ absorbed;
  upstream-intended →(PR rejected/wontfix)→ fork-permanent (with `upstream_ref` kept as
  provenance); fork-permanent →(we later upstream it)→ upstream-intended. No other
  transitions; absorbed is terminal unless upstream reverts (then → fork-permanent).
- **Queue derivation (RC):** "open PR?" is computed LIVE at sweep time via
  `gh pr view <upstream_ref>` — never inferred from the static JSON. Entries with
  `upstream_ref: null` (all fork-permanent, any unsubmitted upstream-intended) are
  SKIPPED by the PR-state check, not errored (RC-γ) — null-ref upstream-intended
  entries are precisely the "needs submitting" backlog.

## Build checklist (B4: relabeled)
- [x] Spec rev2 (this file)
- [x] Skill `fork-parity-doctrine` — **PROVISIONAL: bands uncalibrated** (rev2 edits applied)
- [x] Cron `parity-drift-watch` — **PROVISIONAL: RED+advisory only, no auto-catchup**
- [x] Cron `parity-drift-watch-liveness` (DAILY no_agent, keys on last-success — D1a/CB-A; job 00e94d66358c, 3-path tested)
- [ ] POST-MERGE cards (board parity-doctrine): doctrine doc PR (t_3ccecce4), schema v2
      (t_12df7dc2), absorption sweep (t_d82545b8), upstream PRs (t_276c88ec),
      calibration → flips PROVISIONAL→DONE (t_c4376da6), extractions (t_a734871f, t_c6371702)
