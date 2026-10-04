# Ledger — lane L07-delegate (parity sync 2026-10-01, card t_3bf08f7e)

Lane files: tools/process_registry.py, tools/async_delegation.py, tools/delegate_tool.py.
All three were DECOMPOSED upstream (facade + `<stem>_<topic>.py` siblings, staged `A`,
outside this lane) while the fork added behaviour inline. Method per file: upstream body
wins; every fork delta (per-commit, vs merge-base 26350357d7) re-threaded into upstream's
structure by a scripted edit with unique anchors (`/tmp/e45-L07-delegate/resolve_*.py`).

| path | hunks | mode | why | residual risk |
|---|---|---|---|---|
| tools/process_registry.py | 28 | UP+F (upstream body, 8 fork commits re-threaded) | Upstream: 119 commits — decomposition into `process_registry_{checkpoint,notifications,results}.py`, `ProcessCheckpointMixin`, `_CHECKPOINT_FIELDS` table, `mark_exited`/`append_output`/`_finish_reader`/`_ingest_output` helpers, `persist_on_release`, heartbeats, handoff, retained results. Fork features preserved: (1) `display.background_process_agent_notify` knob — `AGENT_NOTIFY_MODES`, `normalize_agent_notify_mode`, `resolve_agent_notify_mode`, `COMPLETION_SILENCE_HINT`, `completion_required`/`agent_notify_mode` session fields + checkpoint fields, `require_completion`/`suppress_completion`/`_replay_completion`, watch-promotion sets `completion_required`, wait() timeout note (#1533 #1514 #1330; consumers gateway/run.py, goals.py, terminal_tool.py, bot_mode_dm.py); (2) restart-durable `notify_on_complete` children (t_1191e078 #1104): `durable_output=` on `spawn_local`, `/bin/sh` exit-code wrapper, `output_log`/`exit_path` fields+checkpoint, `_file_reader_loop`, `_load_durable_output`, `_read_durable_exit_code`, `_refresh_detached_session` finishes a durable spawn from its recorded exit (re-threaded onto upstream's fate model: `running`→re-attach, else recorded exit wins over prune), `restart_durable_ids`, `hand_off_to_next_boot`, `_handoff_ids`, `_discard_durable_files` in prune; (3) `poll()` not `select()` in `_reader_loop` (#553 FD_SETSIZE blackout) + `errno.EINTR`; (4) bounded orphan-pipe drain `_DRAIN_MAX_BYTES`/`_DRAIN_DEADLINE_SECONDS` in `_reconcile_local_exit` + durable sessions skip it; (5) `_prepend_shell_init(…, _resolve_shell_init_files(explicit_only=True))` in `spawn_local` (#1276); (6) `_SYSTEMD_SCOPE_PROBED_AT = float("-inf")` (#1488 zero-seed lint: run clean on merged file); (7) #788 "stdout EOF ≠ exit": upstream's `_finish_reader` (wait() with no timeout, unknown status stays tracked) is the parallel invention → converged on upstream; kept fork's `stdout_closed` flag, `-signalstatus` PTY exit (`_pty_exit_status`), and the `_move_to_finished` guard that relabels an unknown "exited" as `handle-lost`. Gate AC3 lists 9 base symbols: `_write_checkpoint`/`recover_from_checkpoint` → `process_registry_checkpoint.py`; `format_process_notification`/`_format_async_delegation`/`_format_age`/`_delegation_attribution_line` → `process_registry_notifications.py`; `_append_text`→`ProcessSession.append_output`, `_emit_lifetime_watch_disabled`→`_emit_watch_disabled`, `_is_interrupted`→ local import in `wait()` (upstream renames). | Import smoke blocked by sibling lane (`tools/environments/base.py` unresolved); py_compile + AST + gate pass. **FOLLOWUP: tools/process_registry_checkpoint.py** needs fork t_1191e078 deltas (fork 05bfb486e2): `_write_checkpoint` also writes `self._finished` sessions whose id ∈ `self._handoff_ids` (skip the `exited` filter for them); `recover_from_checkpoint`: when fate != "running" and `entry.get("output_log")`, still adopt the session (detached) then call `self._refresh_detached_session(session)` so the recorded exit finishes it + queues the completion; `pending_watchers` entry gains `"agent_notify_mode": session.agent_notify_mode`. **FOLLOWUP: tools/process_registry_notifications.py** needs fork 3e82011cf5/85de7901b6/61f927beed/24eea22e6e deltas: `format_process_notification` appends `COMPLETION_SILENCE_HINT` (late-import from `tools.process_registry`) before the closing `]` of the completion text; `_reason == "handle-lost"` → "lost its process handle; actual exit is unknown"; new `evt_type == "async_delegation_restarted"` block (fork ours.py L3561-3579). **CROSS-LANE: tools/environments/local.py** must keep `_resolve_shell_init_files(explicit_only: bool = False)` (fork #1276); upstream's signature has no kwarg. |

## UNRESOLVED (ceiling-stop, markers intact, unstaged): tools/async_delegation.py, tools/delegate_tool.py

Both are god-file seams where BOTH sides built live, independently-called machinery. Stages for
each are already extracted under `/tmp/e45-L07-delegate/tools_<name>.py/{base,ours,theirs,conflicted}.py`
(re-extract with `git show :1/:2/:3:` if /tmp was cleaned). Scripted-edit method + checks that
worked for process_registry: `/tmp/e45-L07-delegate/{resolve_process_registry,inventory,verify_file}.py`.

### tools/async_delegation.py — 34 hunks (base 1611 / fork 2576 / upstream 1211 lines)
- Upstream (28 commits): decomposition sibling `async_delegation_recovery_hints.py`; simplify refactor
  d80c689ff9 (unified finalize/stall: `_dispatch`, `_dispatch_admitted`, `_single_crash`, `_batch_crash`,
  `_batch_status`, `_interrupt_records`, `_stalled_*`); NEW durable-completion features with LIVE callers:
  `sweep_orphaned_completions`/`maybe_sweep_orphaned_completions` (gateway/run_notifications.py,
  tui_gateway/session_notifications.py), `defer_completion_delivery`/`return_completion_offer`
  (gateway/run_notifications.py, tui_gateway/session_notifications.py), `failed_delegations_for_session`
  (tui_gateway/methods_subagents.py + openrpc contract), `push_task_failure_notice`/`record_unit_child`
  (tools/delegate_tool_dispatch.py), `is_interim_delegation_event`, `_LIVE_STATES`/`_ACTIVE_STATES`,
  retirement hold (98745df3b9), SQLite layer rework 576accd92b/3966e5de94/a6d65cdd09.
- Fork (10 commits; fork-only sibling `tools/async_delegation_store.py` auto-merged, NOT in conflict):
  durable JSON registry canonical + SQLite mirror protocol — `_locked_registry`, `_canonical_terminal`,
  `_same_completion_event`, `_sync_completion`, `_producer_scoped` (profile-home override around
  consumers), `note_event_delivery_attempt`, `_reconcile_terminal_event_receipt`,
  `acknowledge_event_outbox`, `complete_event_delivery_with_retry`, `_publish_restartable_completion`,
  `recover_async_delegations`, `enqueue_pending_outbox`, `acknowledge_outbox_event`, `_mark_recoverable`,
  `_begin_finalization`/`_finish_finalization` (attempt_id/generation guards), `current_boot_id`,
  `observability_counters`, `_MAX_DELIVERY_ATTEMPTS` 8→10 + 'parked' delivery_state. LIVE callers:
  gateway/run.py (`_recover_async_delegations_once` → `recover_async_delegations`, `enqueue_pending_outbox`;
  `acknowledge_event_outbox`; `complete_event_delivery_with_retry`), cli.py, tui_gateway/server.py
  (`note_event_delivery_attempt`, `complete_event_delivery_with_retry`), tools/delegate_tool.py
  (`acknowledge_outbox_event`, `current_boot_id=`).
- Both-changed core (ours/theirs lines): `dispatch_async_delegation` 221/26, `dispatch_async_delegation_batch`
  235/34, `_finalize` 42/20, `list_async_delegations` 103/39, `interrupt_all` 61/7, `interrupt_for_session`
  83/8, `restore_undelivered_completions` 72/24, `recover_abandoned_delegations` 8(+48 helper)/48,
  `claim_completion_delivery` 18/14, `claim_event_delivery`, `complete_event_delivery`, `get_durable_delegation`,
  `_persist_completion`, `_reset_for_tests`. Fork dropped base `mark_completion_delivered` (replaced by claims).
- Recommended method: upstream body; keep BOTH APIs. Re-thread the fork registry protocol into upstream's
  `_dispatch`/`_dispatch_admitted`/`_finalize`/`_single_crash`/`_batch_crash` (persist_dispatch at admit,
  mark_submitted at runner start, append_terminal/_publish_restartable_completion at finalize, cancel_matching
  in interrupt paths, `_producer_scoped` on every consumer entry point incl. upstream's new
  defer/return/sweep). Verify every `_store.` call site count vs fork (`grep -c '_store\.'`). Fork tests:
  tests/tools/test_async_delegation*.py (registry/outbox/recovery contracts). NEVER take a side wholesale.

### tools/delegate_tool.py — 41 hunks (base 5071 / fork 7525 / upstream 751 lines)
- Upstream (67 commits): facade + 8 siblings `delegate_tool_{child_run,config,dispatch,progress,registry,
  results,tasks,toolsets}.py` (staged A; imported by hermes_cli/cli_subagent_monitor.py, tui_gateway/contracts,
  tools/process_registry.py, 29 test files). Facade new: `_ROLES`, `_open_child_session_db`,
  `_apply_child_cache_ttl`, `_child_compression_cap_tokens`, `_apply_child_compression_cap`, `_build_children`,
  `_oneshot_spawn_budget`, `_DESCRIPTION_HEAD/_TAIL`. `registry.register(` stays in the facade (L737).
- Fork (19 commits) adds 64 NEW symbols inline (none exist upstream): child-lifecycle supervisor —
  `_ChildLifecycle`/`IllegalTransition`, `_SteerLedger`/`_steer_ledger_of`/`_with_missed_steer`/
  `_close_and_read_missed`, `TIMED_OUT_RUNNING`/`_timed_out_running_entry`/`_classify_child_outcome`,
  `_live_subtree_records`/`_reap_subtree`, `_CHILD_TIMEOUT_FLOOR_S` (60s), `DEFAULT_CHILD_MAX_WALL_MULTIPLIER`/
  `_get_child_max_wall_seconds`, `DEFAULT_HUNG_CHILD_SECONDS`/`_get_hung_child_seconds`, late results
  (`_LateCompletion`, `_record_late_result`, `_owned_late_results`, `_late_results_dir`, `_start_late_completion`,
  `_join_late_result`, `_LATE_*`), `_deferred_teardowns`, `_wait_child_turn`, `_supervise_child_future`,
  `_apply_output_schema`, `_release_child_*`, `_close_child_persistence`, `_door_lock`/`_door_slot`/`_hold_run`/
  `_release_hold`/`_submit_turn`/`_inline_turn`/`_teardown`/`_attach_owner_teardown`, `_regate_inherited_child_tier`
  (#1523), `_build_durable_background_spec`/`build_recovered_delegation_runner` (restart recovery),
  `_INHERIT_CONTEXT_*`/`_fold_conversation_history_to_context`, `_bind/_clear_child_send_origin`,
  `_bind/_clear_child_cron_session`, `_ROLE_PARAM_DESCRIPTION`/`_build_role_param_description`.
- Fork-CHANGED base fns now owned by siblings (carry fork delta → FOLLOWUP per sibling, or override):
  `_close_subagent_steering`, `steer_subagent`, `list_active_subagents`, `_handle_control_action` →
  delegate_tool_registry.py; `_get_child_timeout`, `_resolve_child_credential_pool`,
  `_resolve_delegation_credentials` → delegate_tool_config.py; `_strip_blocked_tools` (code_execution
  inheritance exemption + `agent.fork_ext.tool_gate.strip_blocked_delegate_toolsets` import — FORK FEATURE,
  must survive) → delegate_tool_toolsets.py.
- Both-changed in facade (hand merge): `_build_child_agent`, `_run_single_child`, `delegate_task`,
  `_build_top_level_description`, `DELEGATE_TASK_SCHEMA`.
- FORK FEATURES TO PRESERVE: leaf-default role at every depth (#1541) — upstream facade L191 still
  auto-promotes `effective_role = "orchestrator" if _get_orchestrator_enabled() and child_depth < max_spawn`
  (NousResearch#129821 NOT absorbed in 612d8e44); `code_execution` inheritance exemption + tool_gate
  (fork-features.json entry; tests/tools/test_delegate_toolset_scope.py); `_regate_inherited_child_tier`;
  timed_out_running / late results / hung ceiling (#1535 #1542 #1549 #1573 #1585 #1598 #1599).
- Recommended method: upstream facade + siblings as structure; fork-NEW symbols land in a NEW fork sibling
  `tools/delegate_tool_lifecycle.py` (orchestrator decision — outside a lane worker's list) or, inside the
  lane, appended to the facade behind the sibling imports; re-thread `_run_single_child`/`delegate_task`
  by whole-function replacement from ours.py adapted to upstream's `_build_children`/`_oneshot_spawn_budget`
  seams. Expect a multi-hour, full-context worker; do not start it near a ceiling.
