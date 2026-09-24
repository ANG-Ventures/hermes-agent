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

Remove only this context; the other required contexts stay:

```
gh api --method DELETE \
  repos/ANG-Ventures/hermes-agent/branches/main/protection/required_status_checks/contexts \
  --input - <<<'["fleet/attribution"]'
gh api repos/ANG-Ventures/hermes-agent/branches/main/protection/required_status_checks \
  --jq '.checks'
```

The read-back must no longer list `fleet/attribution`. To re-enable:

```
gh api --method POST \
  repos/ANG-Ventures/hermes-agent/branches/main/protection/required_status_checks/contexts \
  --input - <<<'["fleet/attribution"]'
```

Rollout card: t_ebcec034 (parent t_e3dc871c, class card t_c36ee2f7).
