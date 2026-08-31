# `fork-features.json` field conventions

The registry (`docs/sync/fork-features.json`) is a flat JSON **list** of fork-feature entries.
It is consumed by the parity-merge gates (`forkdelta` / `lint_manifest`), which collect the
`tests` nodeids and run them, so **every field below must survive a merge resolution**.

## Lifecycle

| `lifecycle` | Meaning |
|---|---|
| `fork-permanent` | Stays in the fork indefinitely. Upstream cannot or will not take it. Requires evidence, not assumption. |
| `upstream-intended` | We intend this to land upstream, or it is already proposed. **Must** carry `upstream_pr`. |
| `absorbed` | Upstream's implementation is now canonical. `upstream_ref` records where the behavior lives. |

## `upstream_pr` (convention added 2026-08-31)

Records **our own** upstreaming attempt for an `upstream-intended` entry. Distinct from
`upstream_ref`, which records where a behavior *ended up* after absorption (and may point at a
PR that is not ours — see the telegram-polling entry, whose `upstream_ref` is a maintainer's PR).

**Allowed values:**

- A full PR URL — `https://github.com/NousResearch/hermes-agent/pull/<n>` — the normal case.
- The literal string `"PR pending"` — a **placeholder** meaning *an upstreaming lane is in
  flight but no PR number is known yet*.
- `null` / absent — no upstreaming attempt is being tracked.

### Rules

1. **`"PR pending"` is a promise, not a record.** It asserts only that a lane exists. It is
   **not** evidence that a PR was opened. Anything auditing upstreaming progress must treat
   `"PR pending"` as *unverified*, never as a landed or even open contribution.
2. **`lifecycle: upstream-intended` + `upstream_pr: "PR pending"` is an explicitly incomplete
   row.** It must be reconciled — replaced with a real URL, or reverted to `fork-permanent`
   with the reason — once the lane concludes. A row left in this state indefinitely is rot.
3. **Never infer absorption from `upstream_pr`.** Upstream consolidates and re-authors rather
   than direct-merging (measured: 0/130 direct merges), so a merged/closed PR proves neither
   absorption nor rejection. Absorption verdicts require content sampling against
   `origin/main` — distinctive symbols, traced as mechanisms rather than name greps, because
   renames make per-name greps false-negative.
4. **`upstream_pr` records OUR attempt; `upstream_ref` records the CANONICAL home.** An entry
   can legitimately have both (we proposed it, upstream landed its own version), and they may
   point at different PRs.

## Re-adjudication fields (added 2026-08-31)

`readjudicated` (ISO date), `readjudication_verdict` (one of `GENUINELY-FORK-ONLY` /
`UPSTREAMABLE-WITH-RESHAPING` / `PARTIALLY-UPSTREAMABLE`, plus the split when partial), and
`readjudication_note` (the evidence). A `fork-permanent` entry carrying no
`readjudication_verdict` has **not** been tested against upstream's current tree — its
`fork-permanent` status is an inherited assumption, not a finding.
