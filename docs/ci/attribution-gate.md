# fleet/attribution required check

`fleet/attribution` is a REQUIRED status check on `main` (classic branch
protection, `app_id: -1` = any source) in the repos below. The PR-head status
is posted only by `~/.hermes/scripts/fleet-merge.sh`, after its ledger intent
row. A bare `gh pr merge --auto`, a UI merge, or a UI enqueue never gets it, so
the PR stays `BLOCKED`.

| Repo | Mechanism | Rollback |
|---|---|---|
| `ANG-Ventures/hermes-agent` | merge queue + relay: head status gates enqueue; the relay job re-checks every queue entry on the `merge_group` candidate | DELETE the context (see Rollback) |
| `Kyzcreig/pipecat-house-voice` | head status only (private, no merge queue, no relay). Added to the existing protection next to `unit-tests`, `import-sanity`, `test-deletion-guard` (strict) | DELETE the context (see Rollback) |
| `Kyzcreig/fleet-ops-scripts` | head status only (private, no merge queue, no relay). New protection whose only required check is `fleet/attribution`; `enforce_admins=false`, no force-push, no deletion | DELETE the whole protection (see Rollback) |
| `ANG-Ventures/hermes-home` | NOT gated, deliberately. See "hermes-home exclusion" | n/a |

On hermes-agent the two posters are:

- PR head: the fleet-merge.sh commit status above.
- merge_group candidate: the `fleet/attribution` job in
  `.github/workflows/attribution-merge-group.yml`, which runs
  `scripts/ci/attribution_merge_group.py` from `base_sha` and fails unless
  every queue entry in the candidate has a success `fleet/attribution` status
  on its PR head.

A push to the PR head invalidates the status (per-SHA). Re-run fleet-merge.sh.

## Rollback

Remove only this context. The other required contexts stay in place.
Replace `R` for the other gated repos.

```
R=ANG-Ventures/hermes-agent
gh api --method DELETE \
  repos/$R/branches/main/protection/required_status_checks/contexts \
  --input - <<<'["fleet/attribution"]'
gh api repos/$R/branches/main/protection/required_status_checks \
  --jq '[.checks[].context]'
```

`Kyzcreig/pipecat-house-voice` uses the same two commands with
`R=Kyzcreig/pipecat-house-voice`; its other required contexts stay.

`Kyzcreig/fleet-ops-scripts` was unprotected before wave 2
(`state/attribution-wave2/fos-protection-before.json` is `{"protected":false}`),
and `fleet/attribution` is its only required check. Removing just the context
would leave an empty protection behind, so its rollback deletes the protection:

```
R=Kyzcreig/fleet-ops-scripts
gh api --method DELETE repos/$R/branches/main/protection
gh api repos/$R/branches/main --jq .protected   # must print false
```

Pre-rollout backups live in `~/.hermes/state/attribution-wave2/`
(`phv-protection-before.json`, `fos-protection-before.json`).

The read-back must no longer list `fleet/attribution`. Once it doesn't, a
bare enqueue works again. Drilled on 2026-09-24 (t_e0852f5e arm A): after the
DELETE, throwaway #991 was enqueued with a bare `--auto` and hit
`added_to_merge_queue` at 22:54:38Z. It was dequeued at 22:54:49Z.

## Re-enable

The read-back is eventually consistent. In the drill, the first read
immediately after the POST did not show the context, and a read 10 s later
did. Poll for up to 30 s. If the context never appears, page and treat the
gate as OFF:

```
R=ANG-Ventures/hermes-agent
gh api --method POST \
  repos/$R/branches/main/protection/required_status_checks/contexts \
  --input - <<<'["fleet/attribution"]'
ok=0
for i in 1 2 3 4 5 6; do
  sleep 5
  if gh api repos/$R/branches/main/protection/required_status_checks \
       --jq '[.checks[].context]' | grep -q '"fleet/attribution"'; then
    ok=1; break
  fi
done
[ "$ok" = 1 ] || ~/.hermes/scripts/notify --severity critical \
  --source attribution-gate \
  --body "fleet/attribution re-add on $R NOT visible after 30s; gate is OFF"
```

After the re-add, #991 went back to `BLOCKED` with no queue entry and no
auto-merge.

## Caveats (wave 2, t_e0852f5e)

- Multi-entry groups: with this ruleset (min/max entries 3), GitHub chains
  queue candidates. Each candidate's `base_sha` is the previous entry's
  candidate, so every relay run checks exactly one PR. Each queue entry gets
  its own relay run, and a missing status fails that run: the #986 red run
  36030579038, plus 7+ one-PR green runs under a 10-deep queue. If the
  ruleset ever batches several PRs from `main`, the relay's every-constituent
  loop covers it. The recorded fixtures in `tests/ci/fixtures/attribution_relay`
  pin that loop.
- Stale-candidate reds: a relay run can fail closed with "group head absent
  from merge queue" when the queue rebuilds between the `merge_group` event
  and the lookup. This is fail-closed by design; do not neutral-skip it.
  It does not count as a false red if the same PR goes green on a rebuilt
  candidate within 5 min.

## Wave 2 rollout (2026-09-25, t_e0852f5e)

- Soak on hermes-agent, 2026-09-24T22:54Z to 2026-09-25T16:41Z (verdicted on
  data at ~18h): 65/65 merged PRs had a fleet-merge ledger row (0 bare).
  Relay runs were 89 success / 5 failure. All 5 failed closed and none was a
  policy miss: 3x GitHub 5xx/502 (36161234010, 36159203271, 36074017382) and
  2x stale-candidate "group head absent" (36141194300, 36086701925).
- pipecat-house-voice and fleet-ops-scripts were enabled 2026-09-25 10:55 PT.
  Proof in both directions: the last 8 merged PRs in each repo carry
  `fleet/attribution=success` on their head, and the throwaway PRs with no
  status (phv#419, fos#58) read `mergeable_state=blocked`. Both were then
  closed and their branches deleted.

## hermes-home exclusion

`ANG-Ventures/hermes-home` `main` is unprotected by design, and it stays that
way. The autocommit lane pushes straight to `main` without a PR (26 such
pushes in the 48h before rollout), and classic protection rejects those
pushes with GH006. That would kill the lane. Attribution on hermes-home is
enforced by:

- the `merge_attribution_policy` pre_tool_call hook
  (`hooks/merge_attribution_policy.py`), which refuses unattributed merges and
  direct pushes to `main` from agent tool calls unless they go through
  fleet-merge.sh or a logged per-merge override, and
- the `merge-attribution-watch` cron (`scripts/merge-attribution-watch.sh`),
  which pages #alerts once for each merge that has no attribution comment and
  no ledger row. It is the backstop for anything outside the hook.

This is a scope decision, not a gap. Do not add the required check here
unless the autocommit lane moves to PRs first.

Rollout cards: t_ebcec034 (wave 1; parent t_e3dc871c, class card t_c36ee2f7),
t_e0852f5e (wave 2).
