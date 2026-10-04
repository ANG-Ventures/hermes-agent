# CI round-5 lane L4-kanban — ledger (PR #1624, base 117c1c249d)

Branch `sync/upstream-2026-10-01-ci-L4-kanban`. Method: BRIEF.md Phase-2 binary classification
(fork/main baseline in `$R/base` @ c14e059f8f2; `git show 612d8e44a2:<path>` = upstream side).
Narrow runs through `~/.hermes/scripts/test-gate` with a sandboxed HOME, ≤3 files per call.

Legend: FIXED-CODE = merge regression, code restored/reconciled · FIXED-TEST = upstream-only
fixture adapted to a fork contract, or a stale patch target repointed · INHERITED = red on
fork/main too · OPEN = not fixed.

## Source changes (what the reds led to)

| module | change | class |
|---|---|---|
| `hermes_cli/kanban_db_dispatch.py` `enforce_max_runtime` | auto-merge duplicate: `timed_out` event appended twice per timeout (fork emits once) | FIXED-CODE |
| `hermes_cli/kanban_db.py` `_resolve_birth_session` / `create_task` | `creator_task_id` is lineage: after `parents`, before the worker's own card; explicit session still wins | FIXED-CODE |
| `hermes_cli/kanban_db_workspace.py` `_has_active_children` | upstream's extracted copy dropped the fork's `conn=None` (own connection) + `sqlite3.Error` fail-closed path; every direct `_cleanup_worktree_workspace(task, path)` caller raised inside the best-effort `except` → clean pushed worktrees were never removed | FIXED-CODE |
| `hermes_cli/kanban_db.py` `_recorded_worker_alive` | consult upstream's spawn fingerprint (`tasks.worker_started_at`) before the fork's owner window: a live PID whose fingerprint no longer matches is a post-reboot recycle (never extended/signalled). UNVERIFIED / NULL fall through to the owner window as before | FIXED-CODE |
| `hermes_cli/kanban_db_dispatch.py` `_terminate_reclaimed_worker` | UNVERIFIED fingerprint + dead pid → `terminated` (reclaim proceeds, still never signalled); live → `signal_refused`+`liveness_unprovable` (unchanged) | FIXED-CODE |
| `hermes_cli/kanban_db_dispatch.py` (3 call sites) | call `_terminate_reclaimed_worker` through the `kanban_db` facade: fork tests patch `kb._terminate_reclaimed_worker` (patch seam, same as `_pid_alive` in 78571abfb3) | FIXED-CODE |
| `hermes_cli/kanban_db_dispatch.py` `_reap_terminal_worker_row` | pass the fork's REQUIRED `owner_window` + `conn/task_id/run_id`; the merged call raised `TypeError`, the per-row guard swallowed it, and `reap_terminal_workers` never reaped anything | FIXED-CODE |
| `hermes_cli/kanban_db_dispatch.py` `detect_crashed_workers` | carry the worker's last output (`_worker_final_output`, upstream #88603/#46593) onto the clean-exit/crash error text + event payload; not for rate-limited/infra/cohort releases | FIXED-CODE (upstream feature the fork-side loop dropped) |
| `hermes_cli/kanban_db.py` `request_review` | skip the prose-artifact re-scan when the fork's auto-route already staged them (`routed_artifacts`): the double scan copied `readout_1.md` beside `readout.md` and attached both | FIXED-CODE |
| `gateway/kanban_watchers.py` `_is_corrupt_board_db_error` | read `KanbanDbCorruptError` from `kanban_db_connect` (the facade no longer re-exports it) — the corrupt-board quarantine was silently disarmed | FIXED-CODE |
| `tools/kanban_tools_schemas.py` | `kanban_request_review.reviewer` and `kanban_request_changes` descriptions restored from the fork (argus/human/`kanban.review_assignee`; lens list from `kanban_review_schema`) | FIXED-CODE |

## Per red file

| file | result | disposition |
|---|---|---|
| tests/hermes_cli/test_kanban_block_classify.py | 2/2 green | FIXED-CODE (duplicate `timed_out`) |
| tests/hermes_cli/test_kanban_claim_review_orphan.py | 14/14 | FIXED-CODE (`_kb._terminate_reclaimed_worker` seam in `detect_stale_running`) |
| tests/hermes_cli/test_kanban_core_functionality.py | 24/24 (+2 skipped) | FIXED-CODE (worker last output; `timed_out` dup) + FIXED-TEST (`corrupt_board`: patch BOTH `kanban_db_connect.connect` and the `kanban_db` facade re-export the gateway tick uses) |
| tests/hermes_cli/test_kanban_creator_origin.py | 3/3 | FIXED-CODE (creator lineage) + FIXED-TEST (`session_explicit=True` for the "named session wins" case: the fork homes a worker's fan-out on its human lineage by design, t_09fea045) |
| tests/hermes_cli/test_kanban_dispatch_lock_observability.py | 11/11 | FIXED-TEST (stale subprocess code string still called `kb.dispatch_once`; repointed to `kbd`/`kbc`) |
| tests/hermes_cli/test_kanban_negative_handoff.py | 64/64 | FIXED-CODE (double artifact promotion) |
| tests/hermes_cli/test_kanban_reclaim_unprovable_liveness.py | 36/36 | FIXED-CODE (`_recorded_worker_alive`, `_kb.` seam) + FIXED-TEST (`timeout_survivor`: synthetic pid 424242 gets UNVERIFIED from upstream's fingerprint capture, which is never signalled by contract #99558; pin the legacy NULL row so the survivor guard is what's tested). The `[ttl]` orphan red seen once under load 60 (foreign suites) passed 3× after; not a manifest item |
| tests/hermes_cli/test_kanban_review_coverage_gate.py | 70/70 | FIXED-CODE (request_changes description) + FIXED-TEST (`human_only_board`: drop the fixture's worker scope — upstream's `_worker_run_id_for` now REFUSES cross-card instead of returning None — and home the card on the reviewing session, else the fork's home guard refuses an UNHOMED card) |
| tests/hermes_cli/test_kanban_review_policy_home.py | 1e → green | collection error was the `_kb.` facade import chain; no edit needed |
| tests/hermes_cli/test_kanban_review_reviewer_gate.py | 28/28 | FIXED-CODE (`reviewer` schema description) |
| tests/hermes_cli/test_kanban_schedule_wake.py | 13/13 | FIXED-TEST (`promote_task(force=)` retired by upstream #106195: a forced promotion only reported a success the first claim reverted; the scheduled-exit hint is what the test pins) |
| tests/hermes_cli/test_kanban_second_claim_class.py | 54/54 | FIXED-TEST (`[complete]`: upstream #111764 `LiveClaimError` — an operator close under a live worker claim is `force=True`, never implicit) |
| tests/hermes_cli/test_kanban_survivor.py | 37/37 | FIXED-TEST (upstream #117483 `EmptyCompletionError`: completions carry `result=`) |
| tests/hermes_cli/test_kanban_terminal_worker_reaper.py | 4/4 | FIXED-CODE (owner_window TypeError) + FIXED-TEST (fork receipt gate: structured `metadata`) |
| tests/hermes_cli/test_kanban_termination_identity.py | 27/27 | FIXED-TEST (#117483 `result="done"` on the parent completions) |
| tests/hermes_cli/test_kanban_worker_pid_fingerprint.py | 4/4 | FIXED-CODE (`_recorded_worker_alive`, UNVERIFIED+dead) + FIXED-TEST (operator reclaim of an UNVERIFIED live pid is REFUSED on the fork — #921 fail-closed, `reclaim_refused`/`liveness_unprovable`; upstream released it. Fork guard kept) |
| tests/hermes_cli/test_kanban_workspace_admission_spelling.py | 1e → green | facade import chain; no edit |
| tests/hermes_cli/test_kanban_workspace_retired_root.py | 1e → green | facade import chain; no edit |
| tests/hermes_cli/test_kanban_worktree_teardown.py | 14/14 | FIXED-CODE (`_has_active_children`) + FIXED-TEST (`retries_once`: the fork removes through `kanban_survivor.remove_workspace_dir`, which keeps upstream's single retry but issues git via `kanban_survivor._git`; patch that) |
| tests/tools/test_kanban_tools.py | 91/91 | FIXED-TEST (upstream-only test: this process stands in for the worker so it predates its own claim — use the designed `_pid_started_in_claim` seam; the "late orphan" release needs a provably dead claimer past the launch bound, #921/t_09180e10) |
| tests/tools/test_kanban_unknown_arguments.py | 3/3 | FIXED-TEST (upstream-only: act as the owning worker — fork refuses a review handoff of a live-claimed card without `expected_run_id`/`force`) |
| tests/tools/test_kanban_worker_authority_isolation.py | 7/7 | FIXED-TEST (fixture pinned `HERMES_KANBAN_RUN_ID` to the TASK's `claim.id`, not the run id — upstream's `_worker_guard` now refuses; plus the fork receipt gate wants structured metadata) |
| tests/hermes_cli/test_kanban_db.py (3 upstream-only reds) | 3/3 (file: all green in the w2 batch) | FIXED-TEST: `stale_claim_reclaim_without_spawn` — fork releases a pid-less claim only with a dead claimer past the launch bound (modelled with a real exited pid); `infrastructure_spawn_refusal` — `restart_safe_gateway_child_argv` returns `in_process` before any probe off Linux, so marked `platforms("linux")` (CI runs it; a Mac cannot); `archive_running_task_terminates_worker` — the fork's event is `archive_worker_terminated` (parallel invention of upstream's `archive_worker_termination`, with the owner-window verdict + `workspace_kept`) |

OPEN: none. INHERITED: none (every fork-origin file passes on fork/main @ c14e059f8f2).

Lane overlap note: `hermes_cli/kanban_db.py` / `kanban_db_dispatch.py` / `tools/kanban_tools_schemas.py`
are edited here only at the seams named above; no other lane's manifest names them per BRIEF, but
the orchestrator should expect textual adjacency in `kanban_db.py` if another lane touches it.
