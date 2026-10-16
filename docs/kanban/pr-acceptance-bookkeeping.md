# Kanban PR acceptance bookkeeping: measured cause and repair (t_b5b407c8)

## Symptom

Card t_f9dc8aad (contract bound to `https://github.com/Kyzcreig/hermes-agent/pull/1984`)
could not complete. Events recorded `missing` (no `published_pr`), then `infra`
("GitHub acceptance evidence unavailable or incomplete"). The CLI printed
`refused while status is ... (state changed concurrently?)`.

## Phase 0 measurement (read-only, 2026-10-14, ambient `gh` = gh-shim, App `ang-fleet-workers[bot]`)

| Hypothesis | Result |
|---|---|
| Repository rename `Kyzcreig` -> `ANG-Ventures` | **False.** Old and canonical URLs fail identically at the same call. GitHub resolves the old owner to the same repository, PR and head `1eb5c146...`. |
| Failing stage | `gh api graphql` for `repository.pullRequest.baseRef.branchProtectionRule`. GitHub returns HTTP 200 with `errors[type=FORBIDDEN] "Resource not accessible by integration"`: the App token lacks Administration:read. `gh` exits 1 with no `HTTP 403` in stderr, so the gate classified it `infra`. |
| Same data over REST | `GET repos/ANG-Ventures/hermes-agent/branches/main` returns `protection.required_status_checks.checks` to the same token: `sast`, `secret_scan`, `All required checks pass` (app 15368), `fleet/attribution` (unpinned). |
| Would the old gate pass with readable protection? | **No.** Head `1eb5c146` has 0 check-runs; only `fleet/attribution` (status, success). Three required contexts are absent. The truthful verdict is `missing`, not success. `MERGED` does not imply CI green. |
| How it landed | `fleet-merge --break-glass` by Kyzcreig. Attribution comment 6161812345 names `offbox-security-receipt: /Users/<user>/.hermes/var/ci-box/offbox-hermes-agent-1984-1eb5c14676.receipt` (absolute) and `red-ci allowed [All required checks pass (required, ABSENT on head),sast (...),secret_scan (...)]`. Receipt: `head=1eb5c146...`, `tree=hermes-agent-1984-1eb5c146762a`, ruff/pytest/semgrep/gitleaks rc=0. |
| CLI concurrency text | `explain_complete_refusal` never looked at the `pr_acceptance` receipt that had just refused the card. |

Board-wide: of 158 `pr_acceptance` events, only 2 succeeded, both on hermes-home
(whose only required context is `fleet/attribution`, readable without GraphQL protection).

## Repair (smallest change in `hermes_cli/kanban_pr_acceptance.py`, `kanban_db.py`)

1. Required checks come from REST branch protection `checks` (context + `app_id`) and
   rulesets. GraphQL now only reads head/base/state. A context listed in legacy `contexts`
   with no `checks` entry has an unknown app binding: the read fails closed (`infra` at
   `read branch protection`), never degrades to unpinned.
2. GraphQL `FORBIDDEN` errors and `Resource not accessible by integration` are `auth`, not `infra`.
3. Every refusal records `stage`; `infra` also records `error_type`. No gh stderr is persisted.
4. A `missing` verdict names the absent contexts and the two ways forward.
5. `explain_complete_refusal` returns the newest refusing `pr_acceptance` receipt.
6. Break-glass waiver (`_break_glass_waiver`): applies only when the PR is MERGED at the
   evidenced head, all non-success required contexts are *absent* (never failed, pending
   or stale), and the account GitHub reports as `merged_by` authored a `🤖 merged-by:`
   comment that names the receipt and lists every waived context as a whole
   `<ctx> (required, ABSENT on head)` element of `red-ci allowed [...]` (the list ends at the
   first `)]: `; an element starts the list or follows `),`, so a comma or bracket inside a
   context name cannot list another context, and the free-text reason cannot list anything).
   The receipt path is `~`-expanded, then must resolve under `<hermes root>/var/ci-box/`, name
   this head and `<repo-name>-<pr>-<head12>`, and have every rung `rc=0`.
   The receipt file carries only the repo basename, so the owner is bound by fleet-merge's own
   landing record: a `merge` row in `<hermes root>/state/merge-ledger.jsonl` for the canonical
   OWNER/REPO, this PR number and head, whose `receipt_path` resolves to the same receipt. A fork
   (`other/repo`) sharing name, number and head has no such row and gets no waiver.
   The receipt records `waiver` with comment id, receipt sha256 and the ledger row's repo/attempt id.
7. Transferred repos: the final PR re-read records `canonical_repo` when GitHub's base repo
   differs from the contract. The bound contract is never rewritten; a different PR URL is refused.

No DB migration, no config, no gateway restart (CLI and worker-tool code only).

## Live proof (after the patch, same read-only probe)

Both URLs: `classification=success`, `fleet/attribution` success, three contexts `waived`,
`waiver.comment_id=6161812345`, `receipt_sha256=c727b07d...`,
`ledger_repo=ANG-Ventures/hermes-agent`, `ledger_attempt_id=e5251af6...`. The old URL also records
`canonical_repo=ANG-Ventures/hermes-agent`.

## Parent recovery (Apollo, after merge + deploy to the runtime tree)

```
hermes kanban complete t_f9dc8aad --summary "<final summary>" \
  --metadata '{"published_pr": "https://github.com/Kyzcreig/hermes-agent/pull/1984"}'
hermes kanban show t_f9dc8aad --json   # status done; newest pr_acceptance has waiver
```

The run 26332 is operator-claimed by Apollo's session; complete from that session.

## Rollback

Revert the commit. No persisted state changes shape; existing receipts stay readable.
