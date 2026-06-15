# Native Slimmer + LCM Composition Report

Generated: 2026-06-15 02:40:08 UTC
Task: C1 native-slimmer + LCM composition/eval adapter

## Verdict

Live rollout: **NO-GO today**.

Reason: the targeted unit/script path proves native-slimmer + LCM composition and PRD #3 adapter wiring, but PRD #3 requires a real Hermes-session recovery gate before any recovery-mode GO/NARROW-GO can authorize live rollout. That gate must show the real marker plus real expand tool in a live agent loop, with the model not explicitly told to expand, and record model-initiated expand-rate > 0. This task did not run that live-session gate.

Eval-stage decision: **NARROW-GO to proceed with isolated PRD #3 battery runs** for `native-slimmer` and `native-slimmer+lcm` modes. The adapter is in place and the deterministic composition checks are green; keep it fenced to isolated eval/profile runs until the full battery and real-session recovery gate pass.

## What was proven

1. Native slimmer active-lossless mode emits a parseable `HERMES_ARTIFACT_COMPACTED` marker with an artifact ID, stores the raw payload, and `expand_artifact` recovers the exact original raw text.
2. The staged hermes-lcm engine at `staging/lcm-profile/plugins/hermes-lcm` compacts the marker through a deterministic offline summary seam; the active LCM summary preserves both the marker token and artifact ID while excluding the buried raw decision line.
3. LCM raw-message retrieval still finds the marker row: `lcm_grep` finds the artifact ID and `lcm_expand` returns content containing that ID.
4. The PRD #3 adapter seam is test-local at `tests/compression/adapters/native_slimmer_lcm.py` and supports both modes:
   - `native-slimmer`
   - `native-slimmer+lcm`
5. The adapter imports the external battery oracle from `/Users/alexgierczyk/.hermes/projects/context-compression-eval/battery/` using the real repo path, not the Hermes profile `$HOME`, and scores recovery answers against the frozen raw source. A plausible non-raw/lying expand citation is rejected.
6. The adapter's rollout helper returns `NO-GO` when the real-session recovery gate has not been run.

## Verification

Command run from `/Users/alexgierczyk/.hermes/worktrees/prd2v2-native-slimmer`:

```text
pytest tests/compression/test_native_slimmer_lcm_composition.py -q
```

Observed output:

```text
Pytest: 3 passed
```

## Files changed

Allowed write scope only:

- `tests/compression/test_native_slimmer_lcm_composition.py`
- `tests/compression/adapters/native_slimmer_lcm.py`
- `docs/reports/native-slimmer-lcm-composition.md`

Paths touched outside scope: none.

## Residual risks / reviewer notes

- The LCM summary call is deterministic/offline for the unit check; it proves DAG compaction preserves the marker handle, not that a live model will choose to expand.
- No real-session recovery gate was run; live rollout remains blocked until model-initiated expand-rate is observed in a live Hermes loop.
- No provider `usage`/savings data was collected here; the full PRD #3 battery still needs to measure correctness and savings before rollout.
- No live profile config, live plugin directory, or external battery files were modified.
