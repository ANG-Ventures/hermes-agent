# F02c-notify_cache_misc — round-4 re-thread ledger (card t_e36192c9, parity sync 2026-10-01)

Lane targets: `gateway/run_notifications.py`, `run_agent_cache.py`, `run_goals.py`, `run_adapters.py`,
`run_voice.py`, `run_config_loaders.py`, `run_startup.py`. Fork deltas from `/tmp/e45-F02/diffs_<qualname>.diff`
(base→fork of the old god-file `gateway/run.py`, fork tip `b6c8bfe8ab`). Upstream structure kept; fork
behaviour re-threaded. Nothing committed; files staged with `git add`.

## Items

| target::symbol | status | evidence | test(s) |
|---|---|---|---|
| run_goals.py::`_loop_wakeup_watcher` | PORTED(partial) | `list_active_loops`/`complete_tick` were already off-loop upstream (`_run_in_executor_with_context`); re-threaded the remaining on-loop call: `_build_process_event_source` now runs via `_run_in_executor_with_context` in `_loop_wakeup_fire_one` | `test_loop_command.py`, `test_goal_continuation_drain.py` green. `test_no_sync_db_on_loop.py` is a source scan of `gateway/run.py` only (does not cover the sibling) |
| run_goals.py::`_post_turn_goal_continuation` | PORTED | continuation event built with `internal=True` (shared-multi-user sender prefix must not stamp `[<user>]` on a judge-authored prompt) | `test_goal_continuation_drain.py` green |
| run_adapters.py::`_platform_reconnect_watcher` | PORTED | `_bump_reconnect_backoff` → `_reconnect_backoff(attempt, platform)` (Discord 120s cap); `_install_reconnected_adapter` awaits `_prepare_auto_resume_decisions(platform=)` before `_schedule_resume_pending_sessions(platform=)` (fork 20645). Docstring/second watcher (`_run_secondary_profile_reconnect`) already carried both upstream | `test_platform_reconnect_backoff.py` green; `test_platform_reconnect.py` 46/46 green with the fixed harness (see Harness note) |
| run_adapters.py::`_bounded_adapter_teardown` | PORTED | installs `adapter._shutdown_pending_sink = self._spool_one_adapter_pending` before `cancel_background_tasks()` (t_e253d9d5 boot-replay spool); `_spool_one_adapter_pending` lives in run.py and is consumed by `platforms/base.py:5696` | `test_bounded_adapter_teardown.py` green |
| run_voice.py::`_handle_voice_channel_input` | PORTED | `_voice_input_source` is now async and resolves the bound text channel's `chat_type` through `adapter.get_chat_info()` (drops the input with a warning on `error`); hard-coded `"channel"` removed | `test_voice_command.py` 64 passed |
| run_config_loaders.py::`_apply_fallback_chain_to_agent` | PORTED | cooldown check replaced by the single restore predicate `agent.fallback_wiring.restore_allowed(agent, probe=False).allowed` (fallback spec §4.2); unused `time` import dropped | `test_fallback_chain_reload.py` green |
| run_config_loaders.py::`_set_session_reasoning_override` | PORTED | write-through to `session_store.entry_for(key).reasoning_override` + `store.persist()` (P3a restart survival), best-effort | `test_reasoning_override_persistence.py` green |
| run_config_loaders.py::`_load_service_tier` | NO-PORT-NEEDED | upstream delegates to `hermes_cli.cli_config_load._parse_service_tier_config`, which already maps `ultrafast` and warns on unknown words (verified: `['priority','priority','priority','ultrafast',None,None,None,None]` for fast/priority/on/ultrafast/normal/off/''/bogus) | — |
| run_startup.py::`_start_loop_liveness_guards` | PORTED | passes `starvation_load_factor` / `starvation_max_hold_s` from `GatewayConfig.liveness_starvation_*` into `start_loop_liveness_watchdog` (the watchdog already accepted them) | `test_liveness_starvation_hold.py` 1 inherited red, see XFOLLOWUP |
| run_notifications.py::`_build_process_event_source` | PORTED | persisted origin / cached source reused only when its profile matches the event's `profile` (default when unset); synthetic source takes `evt["profile"]` first | `test_async_delegation_restart_recovery.py::test_process_event_source_does_not_reuse_wrong_profile_origin` green |
| run_notifications.py::`_inject_watch_notification` | NO-PORT-NEEDED (vocabulary) | upstream's True/False/None contract + `admit_internal_event` receipt is the merged contract; fork's `"delivered"/"temporary"/"dropped"` strings and the inline pinned-parent check are superseded by `_preflight_completion_delivery` → `_classify_completion_target` (same `_USER_BOUNDARY_END_REASONS` policy) | fork tests asserting strings → XFOLLOWUP-TEST |
| run_notifications.py::`_deliver_completion_notification` | PORTED | (a) producer scope: `_producer_completion_scope` binds `evt["_registry_profile_home"]` (falls back to source-derived `_completion_event_scope`); (b) after admission writes BOTH receipts via `complete_event_delivery_with_retry` for primary + siblings, off-loop, `asyncio.shield`ed so cancelling the awaiter cannot undo an admitted delivery; `admitted` out-param tells group callers an admitted batch from an abandoned one; (c) `_preflight_completion_delivery` claims through `_claim_completion_notification` (cancellation-recovering, reconciles terminal receipts), covers `async_delegation_restarted`, writes the JSON outbox `dropped` receipt before a terminal drop (receipt failure ⇒ retry), retargets a deliverable pinned parent at its compression tip, and releases a just-taken claim if target verification is cancelled | adapted fork tests (see Verification) 126/128; `test_completion_admission.py`, `test_completion_claim_cancellation.py` (25/25), `test_coalesced_receipt_cancellation.py` green |
| run_notifications.py::`_deliver_async_delegation_group` | PORTED | producer scope; sibling claims via `_claim_completion_notification`; claims released when the group is cancelled/raises before the primary is admitted | `test_completion_claim_cancellation.py`, `test_coalesced_receipt_cancellation.py` |
| run_notifications.py::`_async_delegation_watcher` | PORTED | `async_delegation_restarted` drained as an async event; `False` results requeued with a bounded `_temporary_retries` counter (cap 900 ≈ 30 min, then parked for the durable outbox) | `test_completion_delivery.py::test_unroutable_async_event_remains_retryable` green |
| run_notifications.py::`_flush_process_completion_batch` | NO-PORT-NEEDED | fork delta was the string→bool vocabulary seam; merged tree is bool end-to-end | — |
| run_notifications.py::`_async_delegation_group_key` | PORTED | `_ASYNC_GROUP_KEY_FIELDS` now leads with `_registry_profile_home`; static `_async_delegation_group_key` restored over `_event_route_key` | `test_completion_delivery.py::test_async_completion_batch_key_separates_producer_profiles` green |
| run_notifications.py::`_format_coalesced_process_completions` | PORTED | trailing hint is `tools.process_registry.COMPLETION_SILENCE_HINT` | `test_completion_silence_hint.py` cannot collect, see XFOLLOWUP |
| run_notifications.py::`_run_process_watcher` | PORTED | `display.background_process_agent_notify` suppression (`off` / `empty-success` on healthy silent exit) decided atomically via `process_registry.suppress_completion(session, replay)`; mode resolved from watcher → session → `_load_background_agent_notify_mode()` | `test_completion_session_boundary.py`, `test_background_process_notifications.py` (2 inherited reds, 2 stale doubles → XFOLLOWUP-TEST) |
| run_notifications.py::`_send_update_notification` | PORTED | bounded retry: configured platform 24h / unconfigured 5 min (`_update_notify_platform_is_configured`, `_update_marker_age_seconds` from run.py); still-running marker abandoned after 24h; deferral log throttled per target via `_update_notify_deferred_key`, reset on delivery. Replaces upstream's single 1h `_UPDATE_NOTIFY_MAX_ADAPTER_WAIT_SECONDS` | `test_update_command.py` 35 passed, 1 stale upstream-only test → XFOLLOWUP-TEST |
| run_agent_cache.py::`_release_turn_lease` | PORTED | `registry.release_when_idle(token)` (deferred while the worker thread drains, FleetReview #1409); WARNING when a popped-miss finds an older generation still the registry holder; release failure logs WARNING | `test_turn_lease.py`, `test_stop_zombie_turn_lease.py`, `test_reaped_eviction_interrupts_run.py` 26 passed |
| run_agent_cache.py::`_interrupt_and_clear_session` | PORTED | `running_agent._persist_superseded = True`; `/stop` (`invalidation_reason` starts with `stop_command`) awaits `_mark_user_stopped`; pending clarify prompts cleared via `tools.clarify_gateway.clear_session`; draining task captured into `_draining_turns` before `_drop_turn_slot` | `test_stop_clears_pending_clarify.py`, `test_undo_drain_guard.py`, `test_stop_chat_scope.py`, `test_stop_during_preflight_refuses_turn.py`, `test_agent_loop_stopped_hook.py`, `test_interrupt_keeps_parked_internal_wake.py` green |
| run_agent_cache.py::`_evict_cached_agent`, `_sweep_agent_cache_under_pressure` | NO-PORT-NEEDED | upstream `_spawn_release_thread` (profile-scoped, inline fallback) is the superset of fork's `_release_agent_off_loop` | — |
| run_agent_cache.py::`_pinned_session_context_prompt` | NO-PORT-NEEDED | upstream already carries `internal=` pin reuse + redact_pii-mode guard (lines 759-785) | — |
| run_agent_cache.py::`_restore_session_model_override` | NO-PORT-NEEDED | fork delta was a comment (one-turn restore stays raw, not through `_set_session_model_override`); upstream body is already raw | — |

Counts: PORTED 17 (1 partial), NO-PORT-NEEDED 7, DROPPED 0.

## XFOLLOWUP (outside this lane)

| item | evidence |
|---|---|
| XFOLLOWUP `gateway/run.py` `_hermes_home = get_process_hermes_home()` (fork: `get_hermes_home()`) | merged-only, present with my diffs reversed. `_planned_restart_notification_path()`/`_reconcile_deferred_restarts_at_boot` resolve the import-time launch home, so under a monkeypatched `HERMES_HOME` (and any served-profile scope) the restart markers are read from the wrong home. Symptom: `test_platform_reconnect.py` reds when the suite sandbox HOME==`$HOME/.hermes`-shaped (see Harness) |
| XFOLLOWUP `gateway/config_loader.py` `_FLAT_GATEWAY_KEYS` | `liveness_starvation_load_factor` / `liveness_starvation_max_hold_s` are not bridged from the nested `gateway:` block (only the `loop_watchdog*` keys are, line 98-99) → `test_liveness_starvation_hold.py::test_load_gateway_config_bridges_the_knobs` red on base-identical grounds (fails identically with my diff reversed); fork bridged them in `GatewayConfig.from_dict` |
| XFOLLOWUP `gateway/response_filters.py` `SILENT_REPLY_TOKEN` | fork constant dropped by the merge; `tests/gateway/test_completion_silence_hint.py` fails collection (`ImportError`) so the `_run_process_watcher` suppression canaries cannot run |
| XFOLLOWUP `gateway/slash_commands*.py` `/stop` reply text | `test_stop_ends_background_delegations.py::[idle]` expects `Stopped` in the reply; merged reply is `⚡ Stopping — finishing the current step…`; identical red with my diff reversed |
| XFOLLOWUP `tools/process_registry_notifications.py` | `format_process_notification` does not render `async_delegation_restarted` (`test_async_delegation_restart_recovery.py::test_restart_event_uses_existing_process_notification_formatter` expects `RESTARTED`); fork formatter has the branch |

## XFOLLOWUP-TEST (stale tests; not edited here)

| test | proposed change |
|---|---|
| `test_completion_delivery.py` (35), `test_coalesced_receipt_cancellation.py` (4), `test_completion_claim_cancellation.py` (9), `test_async_delegation_restart_recovery.py::test_async_injection_ack_outcome_distinguishes_delivery_from_ended_session`, `test_background_process_notifications.py::test_inject_watch_notification_{raw_session_key_self_posts,origin_session_id_wins}` | three mechanical axes, all merged-contract: (1) `"delivered"/"temporary"/"dropped"` → `True/False/None`; (2) `handle_message=AsyncMock()` doubles must set `event._gateway_accepted = True` (upstream `admit_internal_event` receipt; `AdmittingHandler` already exists in `test_completion_delivery.py`); (3) `_runner()` stubs `session_store` without `_db`, and upstream #98573 makes the runner BORROW `session_store._db`, so `runner._session_db` resolves to None → every pre-flight returns `retry`. A `_db` property returning `hermes_state_registry.acquire()` fixes it. Proof: `/tmp/e45-F02c-notify_cache_misc/make_scratch_tests.py` applies exactly these three rewrites to copies of the four files → 126/128 pass against this tree (remaining 2 = `test_json_outbox_drop_requires_proven_terminal_target[False-*]`, which asserts an unroutable event (no `session_key`) is `dropped`; upstream's `_completion_delivery_ready` deliberately keeps it retryable — contract conflict, orchestrator decision) |
| `test_background_process_notifications.py::test_agent_notify_receipt_only_while_launching_turn_is_busy[*]` | `_FakeRegistry` double needs `suppress_completion(session, replay) -> False` (production `ProcessRegistry` API the ported watcher calls) |
| `test_update_command.py::TestSendUpdateNotification::test_drops_stale_marker_when_the_platform_never_connects` (upstream-only) | asserts the 1h/`adapter never connected` policy the fork replaced; with the fork policy a configured platform keeps a 2h-old marker. Either drop the test or set `runner.config` to a config where telegram is NOT enabled (then the 5-min grace abandons it) |

## Verification

Host gate, file-scoped (`scripts/test-gate` via the orchestrator's `e45-pt.sh`). Fork canaries green after this lane:
test_voice_command (64), test_fallback_chain_reload, test_reasoning_override_persistence, test_goal_continuation_drain,
test_loop_command, test_platform_reconnect_backoff, test_bounded_adapter_teardown, test_platform_reconnect (46/46, fixed
harness), test_completion_admission (3), test_completion_claim_cancellation (25), test_coalesced_receipt_cancellation
(scratch-adapted), test_turn_lease + test_stop_zombie_turn_lease + test_reaped_eviction_interrupts_run (26),
test_stop_clears_pending_clarify + test_undo_drain_guard (16), test_stop_chat_scope + test_stop_during_preflight_refuses_turn
(18), test_agent_loop_stopped_hook + test_interrupt_keeps_parked_internal_wake, test_update_command (35/36),
test_completion_session_boundary + test_background_process_notifications (35/40; 4 stale doubles, 1 XFOLLOWUP).
Differential against this lane's diff reversed: every remaining red is present without the lane's changes (no lane-introduced reds).

**Harness note (orchestrator):** `e45-pt.sh` sets `HOME=$SB` and `HERMES_HOME=$SB/.hermes`, and `tests/conftest.py`
`_hermes_home_points_at_production` treats `$HOME/.hermes` as PRODUCTION, so the session sandbox is re-rooted and the
`home_io_guard` refuses I/O under the gate's own home (`TEST BUG: file I/O against the REAL hermes home` on
`.restart_pending.json`, `gateway_voice_mode.json`). `test_platform_reconnect.py` / `test_completion_admission.py` go
7 reds → 0 with `HERMES_HOME=$SB/hermes-home HERMES_TEST_SANDBOX_HOME=$SB/hermes-home` (`/tmp/e45-F02c-notify_cache_misc/e45-pt-fix.sh`).
Re-baseline earlier lanes' "inherited" reds under the fixed wrapper before trusting them.

Scratch: `/tmp/e45-F02c-notify_cache_misc/` (port script, adapted test copies, logs). Heartbeat `/tmp/e45-hb-F02c-notify_cache_misc.txt`.
