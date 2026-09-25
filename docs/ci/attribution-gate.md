# fleet/attribution required check (merge queue)

`fleet/attribution` is a REQUIRED status check on `main` of this repo
(branch protection, `app_id: -1` = any source). Two posters satisfy it:

- PR head: a commit status posted only by `~/.hermes/scripts/fleet-merge.sh`
  (after its ledger intent row). A bare `gh pr merge --auto` or UI enqueue
  never gets it, so the PR never enters the queue.
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

Rollout card: t_ebcec034 (parent t_e3dc871c, class card t_c36ee2f7).
