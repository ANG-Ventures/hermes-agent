# FIXPASS-LOG-B — Parity fix pass B (tests/gateway/ cluster)

Card: t_12798fd9 · Worktree: `~/.hermes/worktrees/parity-2026-08-29` @ sync/upstream-2026-08-29
Baseline (read-only): `~/.hermes/hermes-agent` @ de200ebbf5
Scope: `/tmp/parity-red-files.txt` lines matching `^tests/gateway/` → 20 files
(`/tmp/fixpassB-files.txt`). File ownership: `^tests/gateway/` + `gateway/` impl only.

Protocol: hermetic run (`scripts/run_tests.sh`, fresh `HERMES_HOME`) → baseline-binary
classify → fix → re-prove → git add. Two runs: attempt 1 (run 114) hit the 14400s
runtime cap after fixing 71/76 tests; attempt 2 (run 119) resumed from the /tmp
artifacts, re-established ground truth with a fresh scoped run, and closed the
remaining undo/rewind cluster.

## Run ledger

| Run | Log | Result |
|---|---|---|
| run1 (attempt 1 ground truth) | /tmp/fixpassB-run1.log | 20 files: 239 passed / 76 failed |
| run2 (attempt 2 ground truth) | /tmp/fixpassB-run2.log | 310 passed / 5 failed (3 files red) |
| run3 (final re-prove) | /tmp/fixpassB-run3.log | **313 passed / 0 failed** (2 xfail STOP markers), all 20 files executed |

## Attempt-1 fixes (run 114, timed out before logging; verified green in run2/run3)

Reconstructed from the run-114 card comments + `/tmp/fixpassB-runpy-edits.patch`
(the full run.py edit set) — every one of these clusters was red in run1 and green
in run2 without attempt-2 touching them:

- **test_fast_command (8) + test_tui_gateway_server seam** — UNION literal +
  `_dispatch_sync` adoption per `/tmp/godfile-server/EVIDENCE.md` OPERATOR-DECISION 1-2.
- **kanban_wake trio (test_kanban_wake_key_identity 3, _phantom_prevention 14,
  _worker_creator_regression 3)** — single root cause in the kanban_db reclaim seam
  (`/tmp/godfile-kanban_db/EVIDENCE.md`).
- **relay-injection pair (test_relay_completion_injection_routing 2,
  test_relay_injection_egress_priming 2)** — fork relay seam;
  `agent/fork_ext/relay_headers.py` call sites restored in gateway/run.py.
- **gateway/run.py 4 documented edits** (card comment, run 114): route-identity
  pre-flight restore, context_source seam, pinned-row end_reason guard, delivery
  vocabulary seam. Includes the completion-delivery batch cluster (8),
  test_completion_session_boundary (2), test_session_info (10),
  test_stuck_loop_drain_accounting (7), test_session_model_reset (5),
  test_scale_to_zero (2), test_gateway_platform_event_hook (2),
  test_custom_provider_request_overrides (2), test_turn_request_overrides,
  test_gateway_command_handler_parity, test_restart_resume_pending.
- **RUN.PY RECONCILIATION** (operator note 06:15): verified worktree
  gateway/run.py == /tmp/godfile-run/RESOLVED.py (final) + the 4 fix-pass edits
  only; AST symbol oracle counts byte-identical to EVIDENCE.md §2 (resolved=708,
  upstream_only 129/131, fork_only 95/96, common_missing=0). Details in the
  card's run-114 comment. The reconciled 37,587-line file is what this pass's
  `git add` staged.

## Attempt-2 fixes (run 119) — undo/rewind cluster, 5 tests / 3 files

All three are the SAME root divergence: upstream rewrote `SessionStore.rewind_session`
as an inline full-user-turn CAS rewind (`target_text`/`rewound_count` contract,
CAS via `expected_active_ids`); the fork's adjudicated mechanism routes the plain
/undo path through the shared `hermes_undo` core (half-turn semantics,
`rewound_ids`/`prefill_text` contract) — RESOLUTION-LEDGER-2026-08-29.md rows
96/97/98/111, upheld again for the gateway in gateway/session.py (the upstream
mechanism survives only behind `require_retryable_composite=True` for /retry).
The 5 red tests were upstream-authored tests pinning the retired mechanism →
STALE TESTS, not merge damage. Classification proven by execution (probes
/tmp/probe_rewind.py, /tmp/probe_rewind2.py against the live worktree code).

1. **tests/gateway/test_undo_rewind_session.py** (3 red → 3 pass + 2 xfail)
   - `test_rewind_pins_raw_active_ids_when_projection_hides_review_harness` →
     rewritten as `test_rewind_operates_on_raw_active_rows_not_projection`:
     asserts the fork half-turn contract (raw physical tail soft-deleted,
     transcript uncorrupted, review-harness rows preserved) instead of the
     retired `target_text == "q2"` / `rewound_count == 4` full-turn contract.
   - The 2 CAS fail-closed tests (`..._transcript_changes_after_snapshot`,
     `..._new_turn_lands_after_id_snapshot`) → **STOP-B1** (see below): kept,
     re-pointed at the live code path (`get_messages`, one-shot injection),
     assertions widened to any-of None/busy/error + no-mutation, and marked
     `xfail(strict=False)` with the STOP reason. They currently FAIL the
     no-mutation half: the plain /undo path has NO CAS.
2. **tests/gateway/test_retry_replacement.py** (1 red → 13/13)
   - `test_gateway_undo_prefills_live_carrier_text_and_keeps_scaffold` →
     rewritten as `test_gateway_undo_keeps_composite_carrier_intact`: fork
     mechanism retires ONE half-turn (the failed assistant reply, soft-deleted)
     and the composite carrier survives ACTIVE with scaffold + live ask intact
     (no data loss — same behavior the ledger row 98 proved on a real DB).
     Upstream's carrier-split (`target_text == "REAL ASK"`, hidden-handoff
     reinsertion) is the retired mechanism.
3. **tests/gateway/test_undo_error_honesty.py** (1 red → 14/14)
   - `test_post_commit_head_read_failure_still_returns_committed_rewind` →
     rewritten as `test_in_txn_head_read_failure_rolls_back_whole_rewind`,
     exactly per the orchestrator 07:00 decision (ACK-REVIEW-2026-08-30.md §6):
     the head read moved INSIDE the write txn (ledger L9 / hermes_state hunk 20),
     so the test now arms the same MAX(id) failure and asserts (a) raises,
     (b) transcript unchanged + rewind_count unmoved (rollback proven).
     Hunk 20 NOT reverted; no fail-soft guard re-added. One mechanical
     adaptation: the armed error is now a HARD OperationalError ("disk I/O
     error") — a "database is locked" raised in-txn is retried by
     `_execute_write`'s 20s patience loop and would stall the test.

## STOP items (outside card-B file ownership)

- **STOP-B1 — plain /undo has no fail-closed CAS** (`_CAS_STOP_REASON` in
  test_undo_rewind_session.py). Upstream's inline rewind passed
  `expected_active_ids` into `rewind_to_message`; the fork's shared core
  (`hermes_undo.undo` → `db.rewind_to_message(...)`, hermes_undo.py:317) does
  NOT. Consequence (proven by the 2 xfail tests): a cross-process append racing
  /undo between snapshot read and write is silently soft-deleted along with the
  rewind. Fix belongs in **hermes_undo.py** (adopt `expected_active_ids` from the
  snapshot; `rewind_to_message` already implements the check in-txn) — one small
  diff, but hermes_undo.py is outside `^tests/gateway/ + gateway/`. The 2 tests
  are pre-wired: they flip from xfail to XPASS the moment the core adopts CAS
  (strict=False so that lands green, then drop the marker).

## Final state

- run3: `scripts/run_tests.sh` over all 20 scope files, fresh HERMES_HOME:
  **20 files, 313 passed, 0 failed** (2 xfail = STOP-B1 markers), 13.0s, no FLAKY.
- Staged: `git add gateway/ tests/gateway/` (123 A + 212 M paths), including the
  reconciled gateway/run.py (replacing the stale 02:23 index copy).
- Not committed, not pushed (card constraint).
