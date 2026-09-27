# Fork-PR audit — keep / drop / upstream every fork-only PR

Card: t_753e8ac1 (lead/census run 2026-09-25). Spec: `2026-09-23_fork-pr-audit-fable-team-SPEC.md` (attached to the card).
Asked by Ace 2026-09-23: "audit all of our fork PRs and assess whether they provide any value really. They solve
problems that maybe no longer exist, and create a maintenance burden."

## 0. Census (done, this dir)

Range: `upstream/main..origin/main --no-merges` on a fresh clone (fork `origin/main` = `b029b53035` 2026-09-25,
`upstream/main` = `59004a6235` 2026-09-24 21:54 +0530, merge-base `1e5b507440` = the 2026-08-07 sync target).

- **1,119 no-merge commits = 1,119 PR rows** (squash merges: one commit per PR; 892 carry `(#N)`, 227 are direct commits, keyed `nopr:<sha>`).
- **Excluded (3):** the parity-sync squashes #510, #643, #644 — upstream content, not fork patches.
- **1,116 rows** remain, cut into tranches (`census_v2.json` → `rows[].tranche`, one `tranche_<name>.md` table each):

| tranche | rows | note |
|---|---|---|
| gateway | 181 | gateway/, gateway/platforms, plugins/platforms, tui_gateway |
| agent | 240 | agent/, run_agent.py, hermes_state*.py, hermes_undo.py |
| hermes_cli | 177 | hermes_cli/, cli.py, locales, pyproject |
| plugins | 84 | memory 35, context_engine 25 (vendored LCM — D1a rules), blackbox 20, kanban 3, cron_providers 1 |
| cron+tools | 111 | tools/, cron/ |
| scripts+misc | 117 | scripts/ 95, contributors, staging, eval, receipts, gates.jsonl … |
| auto | 167 | tests-only, docs-only, tests+docs: **follow the verdict of the code they test/document**; no Fable time |
| auto-cherry-pick | 2 | `cherry picked from commit <sha>` with the sha an ANCESTOR of upstream/main (#585 Browser-Use 3.0 port, #784) → **SUPERSEDED-BY-UPSTREAM** (cron+tools auditor spot-checks #585 for fork edits on top of the picks). 11 other rows carry the trailer but pick from fork-internal/unknown commits — they stay in their tranche. |
| auto-desktop-retired | 37 | purely `apps/desktop` → **DROP by D9** (desktop is upstream-owned since 2026-08-08; taken wholesale every sync). 13 mixed desktop rows were re-bucketed by their non-desktop code paths (`note` field). |

Absorption-probe headline (`absorb.json`, 1,116 rows): 6 rows ≥80% src lines already on upstream, 101 rows 20–80%,
833 rows <20%, 176 rows with no substantive src lines (tests/docs/config). The ≥50% rows are listed per tranche — start there.

Columns per row (`census_v2.json`): `pr, shas, subject, first/last_date, add/del/loc, files, paths, subsystem, kind,
class_lines, conflict_files, conflict_syncs, cherry_picked_from_upstream, tranche`.

**Cost proxy — `conflict_files (conflict_syncs)`**: how many of the PR's paths appear in the three sync resolution ledgers
(`docs/sync/review/RESOLUTION-LEDGER{,-20260807,-2026-08-29}.md` = 07-23 / 08-07 / 08-29 syncs) and in how many of the three.
This is FILE-level ("this PR sits on a file that conflicts every sync"), not "this PR itself conflicted N times" — a PR
dated 09-20 can show (3). 795/1,116 rows touch at least one ledger file; 458 touch files that conflicted in all three syncs.

**Absorption signal — `absorb.json`** (per row: `src_hit/src_total` substantive added source lines found verbatim in an
upstream/main snapshot, `sym_hit/sym_total` added def/class names found, `syms_missing`). Starting signal ONLY:
per pr-absorption-census §6 a high hit% can mean the OPPOSITE of absorption (refactor PRs, shared scaffolding, lines
that are upstream's that we edited near), and zero symbol hits can mean upstream RENAMED/RELOCATED it (§6.1: run
`git log upstream/main -S'<symbol>'` before writing DROP). Auditors set verdicts from the distinctive line, never the score.

Known topology defect found during the census (NOT this audit's scope, flagged to the orchestrator): the 08-30 anchor
`parity-sync-20260830` (`991af03f`) is an ancestor of upstream/main but **not of origin/main** — #644 "record upstream
ancestry" landed squashed. merge-base is stuck at 08-07; raw drift counts (22,732 "upstream-only") are inflated vs the true
19,377 since the anchor. See fork-parity-doctrine D1b.

## 1. Team shape (as spec'd; all Fable = profile `daedalus-fable`)

- **Auditor, one per tranche** (6 cards, parallel; gateway highest priority).
- **Adversary, one per tranche** (6 cards, each `parents=[auditor]`): tries to overturn every DROP.
- **Lead review + roll-up** (1 card, `parents=[all 6 adversaries]`): reviews every verdict, resolves auditor/adversary
  conflicts, writes the final table + roll-up, files one card per surviving DROP / UPSTREAM with the branch attached.
- `auto*` tranches need no worker; the lead folds them into the final table.

## 2. Per-PR protocol (the auditor) — verdict needs ALL five columns

1. **Problem + evidence.** What the patch fixed, citing the original evidence (incident, log line, card id, PR body via
   `gh api repos/ANG-Ventures/hermes-agent/pulls/N` — REST, never `gh pr view`). No evidence found ⇒ "problem unproven",
   which is NOT a KEEP.
2. **Still present upstream?** Reproduce on a clean `upstream/main` checkout with the fork's own test (or a new one if
   the fork has none). RED on upstream = problem still exists there. Where a live repro is infeasible, read the upstream
   code path and cite `symbol@file:line` on the upstream snapshot.
3. **Upstream's own fix.** `git log upstream/main -S'<symbol>' -- <path>`, `git log upstream/main --grep`, upstream
   issues/PRs. Cite the upstream commit.
4. **Still fires in production?** Grep the patch's log lines / PHASE markers across `~/.hermes/logs/` and
   `~/.hermes/profiles/*/logs/` (rotated `agent.log*`, `gateway.log*`; report the ACTUAL window covered by mtimes —
   it may be shorter than 30 days, say so). A patch that never fires is a DROP candidate. Read-only on those trees.
5. **Cost.** loc, `conflict_files (conflict_syncs)` from the census, and whether other fork rows depend on it
   (grep the fork for its symbols).
6. **Verdict:** KEEP (still needed, MEASURED, not fixed upstream) · UPSTREAM (needed + generic; output = ready PR
   branch) · DROP (fixed upstream or never fires; output = revert branch, targeted tests green) · SUPERSEDED-BY-UPSTREAM
   (drop ours, take theirs). **Rule: an unmeasured "still needed" is not a KEEP.**

Mechanical sub-steps (test runs, log greps, absorption greps) may be delegated to Opus/Sol via `delegate_task`;
the JUDGMENT stays on Fable.

## 3. Guard rails

- Read-only on live trees (`~/.hermes`, `~/.hermes/runtime/*`, `~/.hermes/hermes-agent`). Work in your own
  clone/worktree under `$HERMES_KANBAN_WORKSPACE` (`hermes kanban clone -q https://github.com/ANG-Ventures/hermes-agent.git fork`,
  then `git remote add upstream https://github.com/NousResearch/hermes-agent.git && git fetch upstream main`).
- Nothing is reverted or merged by the team. Output = proposals (branches pushed to the fork as `audit/<tranche>/...` + cards).
- Every claim is verified by running something, never from a commit message alone.
- PR state reads are REST (`~/.hermes/scripts/gh-pr-rest.py`, `gh api`), never `gh pr view/checks/status` (shared GraphQL budget).
- ONE interpreter for local tests (repo venv); narrow `pytest path::test` only — no local full-suite runs (`local-suite-guard`).

## 4. Deliverables

- `docs/plans/fork-pr-audit/<tranche>.md`: one row per PR — `PR | problem+evidence | upstream RED/GREEN (how) |
  upstream fix (sha) | fire count (window) | cost | verdict | branch`.
- `docs/plans/fork-pr-audit/<tranche>.verdicts.json`: `{key: {verdict, evidence:{...}, branch}}` — the lead
  aggregates from this, so keep the schema.
- Adversary: `<tranche>.adversary.md` — per DROP: `stands | overturned → <new verdict> | needs-lead` with the caller /
  config / incident that still needs the patch.
- Lead: `FINAL.md` (full table incl. auto tranches) + `ROLLUP.md` (fork lines deleted, upstream PRs to open, expected
  reduction in parity-merge conflicts) + one kanban card per DROP / UPSTREAM verdict with the branch attached.
