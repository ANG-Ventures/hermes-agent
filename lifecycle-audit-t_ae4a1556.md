Kanban lifecycle audit — t_ae4a1556

Runtime PR: ANG-Ventures/hermes-agent#126. Predicate consumption is the independent
hermes-home sibling t_df047ff5, per operator ruling. No live board writes by worker.

A — review provenance
Persistent SQLite INSERT and status-UPDATE triggers repair otherwise missing
review_requested implementer data before the statement returns, including raw
SQL clients after installation. Current run identity precedes historical successful
runs, prior nonhuman assignee, then human:operator. The normal _append_event writer
finalizes a provisional trigger event in place in the same transaction, preserving
recovered fields omitted by the final payload. This prevents double notifications.

Writer inventory (direct SQL and dynamic resume targets):
- kanban_db._request_review_txn: canonical handoff, finalizes provisional event.
- kanban_superseded_open.park: canonical handoff, same _append_event seam.
- kanban_db.reopen_task: recovered provenance before terminal run voiding.
- kanban_db.release_unbound_review_claim: returns a claimed reviewer to its lane.
- kanban_db.recompute_ready / unblock_task / promote / requeue / schedule wake:
  resume prior review provenance rather than invent an implementation handoff.
- kanban_db_dispatch recovery/reclaim/failure retry targets: preserve prior handoff;
  persistent trigger fills missing legacy provenance when restoring review.
- kanban_continuation, kanban_priority_door, kanban_handback_new_head,
  kanban_quota_repair: dynamic review retry destinations covered by the same trigger.
- dashboard: review goes through request_review, not a raw status write.
External closer/merge-pass paths writing raw SQL are protected by the persistent
trigger; no changes to the external hermes-home repo in this card.

B — block contract
block_task rejects a bare block only when a transition could enter blocked;
missing/terminal/already-blocked/stale-run no-op behavior stays intact. A kind or
explicit unblock_when predicate is required. Existing dashboard human actions use
needs_input; old test fixtures now explicitly name that same human hold. Automatic
breaker blocks remain untyped by design (existing supervisor classification path).
The one-shot classifies legacy NULL-kind rows as needs_input, preserving the prior
review resume phase and creation-hold dependency-release provenance (including
legacy reason-only creation events). Explicit blocks remain explicit and sticky.
The separate sweep owns durable predicate recovery.

C — human-ready contract
Persistent INSERT/status/assignee triggers move ready+human to todo and emit one
needs_operator event. recompute_ready does not promote human implementation work,
but does resume a dependency-gated human review back to review. Assigning a real
worker makes the todo row promotable. Dry-run per-card manifest names every move.
The prior census was C218, not the original task's 215; counts are not constants.

D — scheduling contract
scheduled requires next_eligible_at or an explicit unblock_when: event:<name> line.
The create-until INSERT carries its validated wake in the initial statement.
schedule_task writes deadline atomically with the status/run close; CLI and API
pass wake_at; dashboard single/bulk moves collect a deadline before sending.
Raw SQL cannot clear a schedule's only exit. Repair adds event:parts-arrived to
named card t_d394f63b; other legacy schedule defects become needs_input holds.

E — root cause correction
Both named children have after-merge edges, not ordinary blocks. Done or archived
parent status deliberately does not release a recorded-PR prerequisite.
Read-only _fleet_refs on production shows t_a9544cd6 (parent of t_88ae92c4) records
BOTH hermes-home#8851 and #8993. REST: #8851 CLOSED/unmerged/draft, #8993 MERGED.
#8851 was described as superseded only in prose; no routed_pr_superseded event
replaced it. The all-recorded-PR gate therefore correctly never released this row.
Its owner must explicitly replace #8851 with #8993 or waive; worker did neither.
The other child's parent t_bbe87605 records #5971 CLOSED/unmerged.

F — terminal prerequisite escalation
Closed-unmerged recorded prerequisites triage todo/ready children with an explicit
replacement-or-waiver reason and one after_merge_prerequisite_closed event. Edges
remain after-merge. Open/unreadable prerequisites still wait; all merged refs still
release to derived-from and normal recompute promotes. Both named live E rows
currently meet F, not only t_a75816c1. This correction was posted to the operator.

Repair operations (operator AFTER guard deployment)
python -m hermes_cli.kanban_lifecycle_repair --db <board-path>
Inspect the JSON per-card changes; default is dry-run with no schema changes.
Repeat with --apply only after approval. Plans are regenerated inside a write lock;
apply logs lifecycle_repaired per card and reports before/after. Idempotence and
review-resume preservation tested on isolated SQLite. No worker live apply.

Observed verification
- Original D red: 2 failed; A/C red: 2 failed (prior run receipt on card).
- F red: todo instead of triage; B red: DID NOT RAISE (prior run on card).
- Fresh remote canonical runner: lifecycle/atomic schedule/after-merge 29 passed.
- Normal-review double-event regression: offbox head ca2e589f notifier 2 failed,
  home-route 1 failed; after fix those complete files 56 passed, 0 failed.
- Lifecycle including current-run recovery + timed mint: 30 passed, 0 failed.
- Tool/API/lifecycle group at b49b970e: 116 passed, 0 failed.
- Exact-head full offbox receipts and any base attribution are posted on PR #126;
  a running receipt is not a green result. No CI success claimed here.

Deviations: none. Runtime deploy and live repair are operator-owned.
