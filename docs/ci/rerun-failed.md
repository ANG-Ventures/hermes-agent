# Re-running failed CI jobs

Use the helper, not a bare `gh run rerun --failed`:

```
python3 scripts/ci/rerun_failed.py --repo ANG-Ventures/hermes-agent <RUN_ID>
```

It runs the failed-jobs re-run, waits up to 3 min for the new attempt to show
jobs, and if the attempt is `startup_failure` with 0 jobs it starts ONE full
re-run. `label-rerun.yml` (the `ci-reviewed` label hook) calls it.

## The failure

A failed-jobs re-run of `ci.yaml` sometimes creates an attempt that concludes
`startup_failure` with `jobs.total_count == 0`. The run page shows only
GitHub's annotation "An unexpected error has occurred and we've been
automatically notified", with a request ID. The attempt has no
`referenced_workflows`, so it fails before GitHub resolves the called
workflows. A full `gh run rerun <id>` on the same run then starts normally.

No workflow step can detect or refuse this: no job starts, so there is nowhere
to print an `::error`. The caller of the re-run has to handle it.

## Measured (`ci.yaml`, REST attempts + jobs API, pulled 2026-09-25)

| outcome of a failed-jobs re-run | attempts |
|---|---|
| started (typically 30 jobs copied + 16 re-run, incl. slice matrix), last 300 runs = 09-24 19:52Z .. 09-25 | 18 |
| `startup_failure`, 0 jobs, same window | 2 |
| `startup_failure`, 0 jobs, all `status=startup_failure` runs since 09-20 | 6, on 5 runs |

`startup_failure` attempts: 35547640551 #2 and #3 (twice in a row on the same
run), 35898441649 #2, 36070097433 #2, 36145913460 #2 (PR #1086),
36170775010 #2 (PR #1083). The 36170775010 full re-run (#3) started 46 jobs.

What it is NOT, from the same data:

- Not the dynamic slice matrix or Placement outputs. The 18 successful
  selective re-runs copied `generate`/`Placement` from attempt 1 and re-ran
  `Run tests slice k/16` from that copied matrix. The `plan_attempt` guard in
  `tests.yml` already covers the placement plan.
- Not the failed-job set. 36145913460 (failed: Review label gate + gate) was
  `startup_failure`; 36152861124 with the same failed set started normally.
- Not the wait time. It happened 3 and 8 min after attempt 1 ended. Successful
  re-runs started 3 s to 2.7 h after.
- Not a repo-variable change. No `CI_*` variable was updated between attempt 1
  and the re-run for 36145913460 or 36170775010 (`actions/variables`
  `updated_at`).

It is also not limited to re-runs. On 2026-09-25 the first attempt of run
36175286613 (the push that added this doc, PR #1102) failed the same way: 0
jobs, the same annotation, and the run was named after the file path instead of
`CI`. `gh run rerun` then refuses with "This workflow run cannot be retried".
The only fix for an attempt-1 failure is a new event: push a commit, or close
and reopen the PR. The helper handles only the re-run case (attempt >= 2),
where a full re-run does work.

The cause is inside GitHub. The only evidence is the "unexpected error"
annotation. If it keeps happening, file a support ticket with the request IDs
from the attempt pages.

## merge_group runs

Do not use a failed-jobs re-run on a `merge_group` run. It copies attempt 1's
`detect` (lane set) and `generate` (slice plan) outputs, so the retry tests
the old plan (#1006). The helper always does a full re-run for `merge_group`.
Re-queuing the PR is also fine.
