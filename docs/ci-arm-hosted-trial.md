# GitHub-hosted ARM slice trial

`CI_ARM_HOSTED_SLICES` is an optional repository variable. Unset, empty or 0
preserves the existing routing; malformed or negative values disable ARM with a
stderr warning. Set it to 3 for the initial trial, and to 0 to roll back ARM
independently of `CI_SELF_HOSTED_SLOTS` and `CI_RUNNER_LABELS`.

The generator first applies existing pool/hosted routing, then selects the N
lightest eligible slices by the sum of cached per-file durations (2 seconds for
an unknown file). Ties use ascending slice index. Selection overrides either
existing venue without renumbering slices or changing their file lists. N is
clamped to eligible slices. Empty slices and slices containing any pinned
`_CORE_SMOKE_TESTS` file are excluded; the scoped `core smoke` slice is also
excluded. The scoped plugin slice is eligible. This is the definition of
non-core for this trial, not exclusion of all files under `tests/`.

Selected slices use `ubuntu-24.04-arm`. E2E stays on the fleet's
`self-hosted,hermes-ci,X64` pool when configured for self-hosted, otherwise on
`ubuntu-latest` (hosted x64). E2E never uses the ARM count.

## Acceptance after reviewed landing

This change enables measurement; it does not establish ARM performance or
compatibility. Do not declare the trial successful from local routing tests.

1. Preserve the preceding 10 completed **merge_group** runs of the CI workflow,
   including head SHA, run ID/attempt, and all paginated jobs records.
2. Set `CI_ARM_HOSTED_SLICES=3`, verify its value by reading it back, and collect
   at least 10 new completed merge-group runs that actually contain ARM slices.
3. For each slice record job created/started/completed timestamps, runner labels,
   conclusion, wall seconds, and queue seconds (started minus created). Preserve
   cancellations and infrastructure errors separately; do not silently drop them.
4. Compare matching **file memberships**, not just slice numbers: duration-cache
   changes can move files between identically named slices. Retain generated
   matrices/logs to establish comparability. Report missing matches honestly.
5. Check retry/FLAKY output and failed-test artifacts even when a job concludes
   success. A green retried job is not a zero-flake sample. Investigate candidate
   ARM-only failures against the same head/file set on x64 before attribution.
6. Report sample counts, per-slice wall/queue distributions and failures on each
   venue; recommend N=0 if slower/flaky. Update Obsidian
   `AI/Infrastructure/CI Capacity` venue row only with measured outcomes.

No deploy or runtime sync is needed or authorized by this trial.
