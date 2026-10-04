# CI6 ledger — lane M1-gatewayb (parity PR #1624, CI round 6)

Branch `sync/upstream-2026-10-01-ci-M1-gatewayb` off fold head `93556253a7f864a90aec10a224213ba0ad6b5c5e`.
Card t_2eb607e2 (two runs: attempt 1 batches 1–3, attempt 2 batches 4–11). Reds from CI run
37038759229 (38 files / 181 red nodeids in the manifest `tools/lanes-r5/M1-gatewayb.md`).
Every file re-run narrowly (≤3 per call) through `~/.hermes/scripts/test-gate` with a sandboxed
HOME; "green" = the file's narrow run on the branch head. Baseline `fork/main` = c14e059f8f.

Legend: FIXED-CODE (fork behaviour restored onto upstream's structure) · FIXED-TEST (test pinned
a moved symbol / stale shape / upstream contract the merge legitimately adopted) · INHERITED ·
FOLLOWUP → lane.

| red file | disposition | what / why | narrow result |
|---|---|---|---|
| tests/gateway/test_no_agent_construction_on_event_loop.py | FIXED-TEST | merged-class ASTs; KNOWN_ONE_HOP empty, two-hop resume snapshot pinned (batch 2) | 8 passed |
| tests/gateway/test_no_atomic_write_on_loop_platform_plugins.py | FIXED-CODE + FIXED-TEST | buzz `_save_cursors`, photon `_write_runtime_record`, google_chat `load_user_credentials` → `asyncio.to_thread`; baseline 11 → 5 (batch 2) | green (26 passed w/ siblings) |
| tests/gateway/test_no_atomic_write_reachable_from_loop.py | FIXED-CODE + FIXED-TEST | `_resolve` searches the `<base>_*.py` mixin family; REACHABLE_BASELINE re-frozen 7 → 16 → 15 after offloads (batches 2–3) | green |
| tests/gateway/test_no_sync_fs_walk_on_loop.py | FIXED-CODE + FIXED-TEST | `_replay_pending_planned_restart_notification` marker I/O off-loop; walker scans run.py+run_*.py / slash_commands(+_*.py) as one family; READ_BASELINE 12 → 10 (batch 3) | green (55 passed w/ siblings) |
| tests/gateway/test_no_sync_syscalls_on_event_loop.py | FIXED-TEST | PER_MESSAGE_BASELINE re-keyed 20 → 14 (relocated sites, nothing new on the loop) (batch 3) | green |
| tests/gateway/test_platform_lock_acquire_off_loop.py | FIXED-CODE | buzz/irc/line connect `await _acquire_platform_lock_async` (fork form; merge took upstream sync) (batch 2) | green |
| tests/gateway/test_reconnect_attention_profile_scope.py | FIXED-TEST | upstream-only test; `_reconnect_backoff` double takes `(attempt, platform)` (fork Discord 120s cap) | 10 passed (w/ siblings) |
| tests/gateway/test_relay_completion_injection_routing.py | FIXED-TEST | asserts upstream's True/False injection contract (merged contract, ledger F02c; same as L3b's test_relay_injection_egress_priming) | green |
| tests/gateway/test_replace_takeover_grace.py | FIXED-CODE + FIXED-TEST | **`--replace` SIGTERM wait = `resolve_replace_takeover_grace_s(drain, cron)` again** (fork #94759; the merge took upstream's fixed 20×0.5s that SIGKILLed a draining gateway mid-SQLite-write, 2026-09-16 incident); `_terminate` double accepts `expected_start_time` | green |
| tests/gateway/test_restart_drain_waits_for_compaction.py | FIXED-TEST | `_G` double carries upstream's `_active_deferred_agent_worker_count` | 13 passed (w/ sibling) |
| tests/gateway/test_restart_durable_background_process.py | FIXED-CODE + FIXED-TEST | **restart-durable spawn (output_log) whose pid died while no gateway ran is adopted and finished from its recorded exit** in `recover_from_checkpoint` (fork t_1191e078; merge pruned on `gone`/`reused`); **`_write_checkpoint` keeps handed-off children even once this process's reader reaps them** (fork `_handoff_ids` loop over `_finished`; the merge dropped it, so a child finishing in the restart gap vanished from the checkpoint — intermittent 0 == 1); lifecycle sweep source is `gateway_shutdown` (upstream #41225) | 4 passed ×4 |
| tests/gateway/test_restart_failure_counts_rmw.py | FIXED-TEST | scans run.py + run_*.py (batch 2) | 5 passed |
| tests/gateway/test_restart_followups_admission_e2e.py | FIXED-CODE | adapter-level profile-route refusal logs PHASE=restart_followup_lost via `fork_ext/restart_followups.report_refused_followup` (batch 1). The lane runner with HERMES_HOME set trips the home_io_guard on `gateway_voice_mode.json` — runner artefact, CI-shaped run green | 18 passed |
| tests/gateway/test_restart_resume_pending.py | FIXED-CODE + FIXED-TEST | `_run_startup_resume_event` captures the adapter task before the thread hop; `_start_impl` source anchor; raising-replay tests pin fork retain+retry (batch 2) | 55 passed |
| tests/gateway/test_session_context_inheritance.py | FIXED-CODE | fork "Agent executor context mismatch" warning restored ahead of dispatch in `run_turn._run_agent_inner` | 10 passed |
| tests/gateway/test_session_continuity.py | FIXED-TEST | upstream-only; transcript-read double accepts `include_timestamp` (fork F01) | 22 passed (w/ siblings) |
| tests/gateway/test_session_key_producer_contract.py | FIXED-TEST | allowlist drops the two single-producer sites upstream rewrote without a bare `chat_type="channel"` literal (Teams `_CHAT_TYPES` table, Telegram conditional); lint still guards every adapter | green |
| tests/gateway/test_session_list_allowed_sources.py | FIXED-CODE | `SessionListRow.pinned` (fork server-side pinned sessions #186) added to the upstream-new strict contract + regenerated `apps/shared` gateway-contract artifacts | green |
| tests/gateway/test_shutdown_blocking_offload.py | FIXED-TEST | the kill sweep is `GatewayShutdownMixin._stop_kill_tool_subprocesses` (run_shutdown.py); walker finds its `to_thread` wrapper and counts loop-thread call sites of either. Mutation-checked: an inline call at the post-interrupt site reds the gate | 241 passed (w/ siblings) |
| tests/gateway/test_shutdown_pending_flush_off_loop.py | FIXED-CODE | **dropped the `self._pending_messages.clear()` the merge re-added AFTER the `drain=True` flush await** (fork t_8d085477: a message arriving mid-flush was destroyed by that clear) | green |
| tests/gateway/test_slack.py | FIXED-CODE | `_SLACK_VIA_HERMES_ONLY` += boomerang/merge/resume-handoff so the 50-slash cap keeps `/help` (R12-cli-b FOLLOWUP; **byte-identical to lane M2-hermes-clia's hunk** — fold is a no-op) | green |
| tests/gateway/test_slash_command_profile_scope.py | FIXED-TEST | upstream-only; `_Runner` double carries `_submit_with_context` (fork's context-preserving hop routes through the latency-logging submitter) | 1 passed |
| tests/gateway/test_stale_prompt_tokens_readers.py | FIXED-CODE + FIXED-TEST | /status + /context "~" mark = upstream preflight-seed provenance OR fork post-compaction estimate (`live_context_tokens.estimated`, t_64728f32); stored -1 sentinel never rendered; pct carries "~" too (upstream 04767e7aaa); run_sync source contract follows upstream's shared `usage` dict | 8 passed |
| tests/gateway/test_stale_turn_image_buffer_order.py | FIXED-TEST | AST lint reads `gateway/run_turn_runner.py` (the `_run_still_current()` gate is intact there); mutation-checked | 1 passed |
| tests/gateway/test_status_adapter_late_binding.py | FIXED-CODE + FIXED-TEST | **fork `_current_status_adapter` late-bind closure restored** onto the TurnContext in `run_turn._run_agent_bind_turn_callbacks`; `TurnRunner._status_adapter_now` resolver, every side-channel send site (status/interim/bg-review/clarify/approval) routes through it (2026-08-05 incident). Structural gate walks run_turn.py + run_turn_runner.py; RED-proven on the one leftover snapshot read. Blast radius: clarify_delivery_fallback + clarify_card_retire + interim_send_lanes 17 passed, route_notice_redelivery green | 5 passed |
| tests/gateway/test_status_command.py | FIXED-CODE | upstream-new test: when fork rehydrate fails closed on a credential-unresolvable persisted identity, /status still shows the committed /model pin from the persisted route lookup (off-loop via `asyncio.to_thread`, display-only; the turn resolver still fails closed) | 15 passed |
| tests/gateway/test_stderr_log_handler_off_loop.py | FIXED-CODE | -v/-q stderr StreamHandler routed through `hermes_logging._register_queued_handler` again (fork 2026-09-23: a WARNING on the loop was a synchronous disk write into gateway.error.log) | 2 passed |
| tests/gateway/test_stop_during_preflight_refuses_turn.py | FIXED-TEST | `import hermes_yaml as yaml` (batch 1) | green |
| tests/gateway/test_stop_turn_lease_leak.py | FIXED-TEST | tokens keyed by run generation (upstream `TurnState.lease_tokens`); the release-first-in-finally invariant is already in run_inbound | 7 passed |
| tests/gateway/test_switch_announce.py | FIXED-CODE + FIXED-TEST | slash_commands lazy `import yaml` → `hermes_yaml`; test imports hermes_yaml (batch 1) | green |
| tests/gateway/test_telegram_intake_sentinel.py | FIXED-TEST | strips comments as well as the docstring before the `block=False` grep (the source comment discusses it); mutation-checked: `block=False` on the sentinel reds the gate | 8 passed |
| tests/gateway/test_tool_response_drop_recovery.py | FIXED-TEST | `interrupt_for_session` reason is the machine-readable invalidation reason (`stop_command`), upstream's call shape | 7 passed |
| tests/gateway/test_turn_concurrency.py | FIXED-TEST | hermes_yaml import (batch 1); `test_semaphore_wait_does_not_claim_transcript_lease` reds only under the HERMES_HOME lane runner (home_io_guard), CI-shaped run green | 137 passed (w/ siblings) |
| tests/gateway/test_turn_lease_review_1409.py | FIXED-TEST | `lease_tokens[generation]` keying | 42 passed (w/ siblings) |
| tests/gateway/test_turn_pool_sized_to_admission.py | FIXED-TEST | upstream `_UnboundedThreadExecutor` has no `_work_queue` (one thread per item) — the no-queue invariant holds by construction; a pooled executor must still have drained | green |
| tests/gateway/test_unclean_restart_resume_notice.py | FIXED-TEST | liveness probe renamed `lifecycle_ledger._pid_is_sentinel_owner` | green |
| tests/gateway/test_usage_command.py | FIXED-TEST | fork /usage override lives in `gateway.slash_commands`; patch targets follow (`fetch_account_usage` / `render_account_usage_lines`) | 11 passed |
| tests/gateway/test_voice_mode_platform_isolation.py | FIXED-TEST | adapter double answers `get_chat_info` (upstream resolves the voice text channel's chat_type from Discord) | 5 passed |

## FOLLOWUP / cross-lane notes

- FOLLOWUP → lane M5-cron-tui-runagent: `tests/tui_gateway/contracts/test_generated.py::test_catalog_covers_the_whole_wire` is red on the fold head too (`session.redo` has no contract) — unrelated to this lane; `test_generated_files_are_current` is green here after the `SessionListRow.pinned` regeneration (keep the regenerated `apps/shared/src/gateway-contract.*` from this branch when folding).
- INHERITED (L3b lane): `tests/gateway/test_route_change_turn_contract.py` 5 reds identical with and without this lane's TurnRunner diff.
- hotspot: `gateway/slash_commands.py` — lane M1-gatewaya carries a 603-line re-thread; this lane touches only the fork `_handle_status_command` override (status-pin fallback + "~" mark) and the `/usage` nothing. `gateway/run_turn_runner.py`: M1-gatewaya's `present()` → `self._schedule` change is in `_status_callback_sync`; this lane's edit there is the fallback line only.
- `hermes_cli/commands_platforms.py`: hunk identical to M2-hermes-clia.
- Source files touched: gateway/run.py, run_turn.py, run_turn_runner.py, run_shutdown.py, run_inbound.py, run_notifications.py, run_startup.py, slash_commands.py, slash_commands_status.py, platforms/base.py, fork_ext/restart_followups.py, tools/process_registry_checkpoint.py, tui_gateway/contracts/sessions.py, hermes_cli/commands_platforms.py, plugins/platforms/{buzz,irc,line,google_chat}.
