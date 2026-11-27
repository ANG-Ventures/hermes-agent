# Inert failure authority core

S2 task t_5e76dccb. No producer is enrolled or enabled by this change; authority defaults to preserve. No production deployment, activation, or historical hydration is included.

Internal API lives in hermes_cli.kanban_failure_ownership. Observation is a frozen typed verifier result, not a public request. publish_evidence accepts only an internal trusted configured verifier; no tool/CLI exposes this callable, Observation, enrollment, or adoption. EvidenceRef contains an opaque persisted token and digest, checked against durable content-addressed capture before mutation. Python callers/direct SQL are trusted code, not an OS security boundary.

prepare_card validates the restricted scratch/local-only card before locking. observe_failure inserts observation, episode, pending binding, task and events under the existing write_txn. Ordinary create_task uses the same SQL row primitive and retains its other checks/semantics. Pending validation refusal returns EVIDENCE_PENDING, never a card id pretending ownership. reconcile_pending is a bounded internal primitive; scheduling is not wired yet.

All exact keys are compact canonical S1 v1 13-field JSON. Episodes are separate generations with predecessor links, unique active episode and binding indexes. Immutable observations and append-only membership/transition history survive replay and late pass boundaries. UNKNOWN/waived/skipped cannot heal. Same-SHA overlapping executions and force-pushed/incomparable evidence preserve active work. Existing repair ownership remains claimable when its producer is disabled.

Terminal task updates detach the binding and leave a pending replacement inside the same SQLite transaction. Referenced tasks are delete-restricted. Reopening an old detector card cannot reclaim a replacement binding. Adoption is an internal operator action requiring exact failure-linked evidence and revision CAS; no public adoption endpoint ships here.

Compatibility floor uses a transient uncommitted guard row inserted and removed by trusted core under the SQLite writer lock. Other writers never see a committed marker, and crash rollback removes it. This is intentionally not a UDF-based trigger: unupgraded sqlite connections must still execute ordinary non-enrolled inserts. Supported registered creator/signature namespaces are refused after enrollment without this marker. Complete real call-site/provenance census and compatibility request capture remain activation prerequisites.

Verification

    python3 -m pytest tests/hermes_cli/test_kanban_failure_ownership.py -o addopts= -q
    python3 scripts/failure-ownership-proof.py --arm atomic --processes 32 --producers 4 --scratch <new-empty-directory>
    python3 scripts/failure-ownership-proof.py --arm crash --all-boundaries --scratch <new-empty-directory>

Use fleet test-gate on Studio. The proof has a 180-second subprocess/process-group limit and records command, pid, duration, exit and log below scratch. Tests assert disposable DB containment before opening it. Frozen synthetic verifier results are not evidence that provider qualification is complete.

Explicitly unimplemented integration / not activation-ready

- Trusted deployed provider reader configuration and S3 source mapper qualification; no historical1811 coverage claim.
- Actual four home producer entry points/fallbacks, independent producer-enable config checks, durable compatibility ingress capture and complete legacy caller namespace census.
- Dispatcher pending tick hookup, trusted per-task head/watermark/binding-generation tickets and refresh; current shared claim checks locally accepted authority state and optionally checks episode revision, not remote freshness.
- Launch-commit fence, reservation refund and pre-workspace integration. Current proof covers actual shared claim/run insertion and null workspace metadata, not whole dispatcher reservations/spawn.
- Operator enrollment/process-protocol census, rollback CLI, production adoption authorization endpoint, automatic detector-only card closure and diagnostic cluster/member refinement.
- Full concurrent lifecycle/adoption barrier matrix, blob-publication SIGKILL boundaries, full independent mutation battery. SQL SIGKILL cases and one claim-bypass mutation were exercised; they are not the whole S1-S5 proof.

Do not enable any producer based on this PR. Rollback preserves tables, identities, receipt blobs, pending obligations and tombstones; no drop-table path exists. Parent owns signing, landing, S4/S5 integration and release proof.
