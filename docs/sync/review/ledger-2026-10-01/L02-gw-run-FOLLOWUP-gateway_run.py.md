# FOLLOWUP manifest — gateway/run.py (lane L02-gw-run, parity 2026-10-01)
Fork tip (:2) = b6c8bfe8ab, upstream (:3) = 612d8e44a2, base (:1) = 26350357d7.
Upstream decomposed GatewayRunner/TurnRunner into mixin siblings (gateway/run_*.py, run_turn_runner.py,
turn_executor.py). Per brief ('upstream structure wins; re-thread fork behaviour'), run.py keeps upstream's
facade; the fork's edits to every method upstream MOVED must be re-threaded into the sibling that now owns
it. Those siblings are outside this lane (added by upstream, no conflict), so nothing below is applied yet.
Recover fork bodies with `git show b6c8bfe8ab:gateway/run.py` (line ranges below are in that file) and
diff against `git show 26350357d7:gateway/run.py` for the fork delta.

## A. Methods upstream MOVED to a sibling that the fork had MODIFIED (re-thread the fork delta there)
Format: `FOLLOWUP <qualname> -> <sibling> (fork lines vs base lines)`. 'ABSENT' = upstream deleted the
method outright; check what replaced it before porting.

```
FOLLOWUP GatewayRunner._handle_message                           -> gateway/run_inbound.py  (fork 1722L vs base 1567L)
FOLLOWUP TurnRunner.run_sync                                     -> gateway/run_turn_runner.py  (fork 1693L vs base 1500L)
FOLLOWUP GatewayRunner.stop                                      -> gateway/run_shutdown.py  (fork 1258L vs base 606L)
FOLLOWUP GatewayRunner.start                                     -> gateway/run_startup.py  (fork 1061L vs base 969L)
FOLLOWUP GatewayRunner._schedule_resume_pending_sessions         -> gateway/run_startup.py  (fork 525L vs base 141L)
FOLLOWUP GatewayRunner._handle_active_session_busy_message       -> gateway/run_busy.py  (fork 477L vs base 445L)
FOLLOWUP GatewayRunner._prepare_inbound_message_text             -> gateway/run_inbound.py  (fork 422L vs base 407L)
FOLLOWUP TurnRunner.send_progress_messages                       -> gateway/run_turn_runner.py  (fork 360L vs base 360L)
FOLLOWUP TurnRunner.progress_callback                            -> gateway/run_turn_runner.py  (fork 319L vs base 285L)
FOLLOWUP GatewayRunner._platform_reconnect_watcher               -> gateway/run_adapters.py  (fork 260L vs base 258L)
FOLLOWUP GatewayRunner._run_process_watcher                      -> gateway/run_notifications.py  (fork 258L vs base 223L)
FOLLOWUP GatewayRunner._resolve_session_agent_runtime            -> gateway/run_turn.py  (fork 228L vs base 168L)
FOLLOWUP GatewayRunner._format_session_info                      -> gateway/run_turn.py  (fork 214L vs base 40L)
FOLLOWUP GatewayRunner._session_expiry_watcher                   -> ABSENT  (fork 198L vs base 190L)
FOLLOWUP GatewayRunner._send_update_notification                 -> gateway/run_notifications.py  (fork 190L vs base 118L)
FOLLOWUP GatewayRunner._interrupt_and_clear_session              -> gateway/run_agent_cache.py  (fork 183L vs base 83L)
FOLLOWUP GatewayRunner._enrich_message_with_transcription        -> gateway/run_inbound.py  (fork 168L vs base 141L)
FOLLOWUP GatewayRunner._inject_watch_notification                -> gateway/run_notifications.py  (fork 161L vs base 145L)
FOLLOWUP GatewayRunner._release_running_agent_state              -> gateway/run_agent_cache.py  (fork 126L vs base 56L)
FOLLOWUP GatewayRunner._drain_startup_restore_queue              -> gateway/run_startup.py  (fork 120L vs base 25L)
FOLLOWUP GatewayRunner._loop_wakeup_watcher                      -> gateway/run_goals.py  (fork 119L vs base 109L)
FOLLOWUP GatewayRunner._run_startup_resume_event                 -> gateway/run_startup.py  (fork 114L vs base 29L)
FOLLOWUP GatewayRunner._await_active_work_before_restart         -> gateway/run_shutdown.py  (fork 111L vs base 97L)
FOLLOWUP GatewayRunner._sweep_agent_cache_under_pressure         -> gateway/run_agent_cache.py  (fork 108L vs base 114L)
FOLLOWUP GatewayRunner._build_process_event_source               -> gateway/run_notifications.py  (fork 104L vs base 93L)
FOLLOWUP GatewayRunner._post_turn_goal_continuation              -> gateway/run_goals.py  (fork 101L vs base 95L)
FOLLOWUP GatewayRunner._run_secondary_profile_reconnect          -> gateway/run_adapters.py  (fork 100L vs base 100L)
FOLLOWUP GatewayRunner._rehydrate_session_model_override         -> gateway/run_agent_cache.py  (fork 99L vs base 64L)
FOLLOWUP TurnRunner._status_callback_sync                        -> gateway/run_turn_runner.py  (fork 98L vs base 35L)
FOLLOWUP GatewayRunner._finish_startup_restore                   -> gateway/run_startup.py  (fork 95L vs base 57L)
FOLLOWUP GatewayRunner._start_secondary_profile_adapters         -> gateway/run_adapters.py  (fork 92L vs base 93L)
FOLLOWUP GatewayRunner._async_delegation_watcher                 -> gateway/run_notifications.py  (fork 84L vs base 63L)
FOLLOWUP GatewayRunner._finalize_shutdown_agents                 -> gateway/run_shutdown.py  (fork 82L vs base 75L)
FOLLOWUP GatewayRunner._handle_voice_channel_input               -> gateway/run_voice.py  (fork 82L vs base 78L)
FOLLOWUP GatewayRunner.request_restart                           -> gateway/run_shutdown.py  (fork 81L vs base 40L)
FOLLOWUP GatewayRunner._run_agent                                -> gateway/run_turn.py  (fork 75L vs base 51L)
FOLLOWUP GatewayRunner._drain_active_agents                      -> gateway/run_shutdown.py  (fork 74L vs base 74L)
FOLLOWUP GatewayRunner._resolve_turn_agent_config                -> gateway/run_turn.py  (fork 69L vs base 63L)
FOLLOWUP GatewayRunner._flush_process_completion_batch           -> gateway/run_notifications.py  (fork 69L vs base 55L)
FOLLOWUP GatewayRunner._evict_cached_agent                       -> gateway/run_agent_cache.py  (fork 61L vs base 70L)
FOLLOWUP GatewayRunner._bounded_adapter_teardown                 -> gateway/run_adapters.py  (fork 58L vs base 51L)
FOLLOWUP GatewayRunner._start_loop_liveness_guards               -> gateway/run_startup.py  (fork 56L vs base 44L)
FOLLOWUP GatewayRunner._finalize_session_off_loop                -> gateway/run_shutdown.py  (fork 50L vs base 50L)
FOLLOWUP GatewayRunner._release_turn_lease                       -> gateway/run_agent_cache.py  (fork 49L vs base 28L)
FOLLOWUP GatewayRunner._suspend_stuck_loop_sessions              -> gateway/run_shutdown.py  (fork 48L vs base 48L)
FOLLOWUP GatewayRunner._pinned_session_context_prompt            -> gateway/run_agent_cache.py  (fork 46L vs base 24L)
FOLLOWUP GatewayRunner._extract_honcho_cache_busting_config      -> ABSENT  (fork 42L vs base 29L)
FOLLOWUP GatewayRunner._echo_pending_stt_transcripts_once        -> gateway/run_inbound.py  (fork 41L vs base 40L)
FOLLOWUP GatewayRunner._cleanup_agent_resources_off_loop         -> gateway/run_shutdown.py  (fork 39L vs base 39L)
FOLLOWUP GatewayRunner._format_coalesced_process_completions     -> gateway/run_notifications.py  (fork 38L vs base 39L)
FOLLOWUP GatewayRunner._apply_fallback_chain_to_agent            -> gateway/run_config_loaders.py  (fork 36L vs base 35L)
FOLLOWUP GatewayRunner._transcribe_pending_audio_event_once      -> gateway/run_inbound.py  (fork 35L vs base 29L)
FOLLOWUP GatewayRunner._set_session_reasoning_override           -> gateway/run_config_loaders.py  (fork 33L vs base 14L)
FOLLOWUP GatewayRunner._reset_notice_session_info                -> gateway/run_turn.py  (fork 33L vs base 19L)
FOLLOWUP GatewayRunner._queue_startup_restore_event              -> gateway/run_startup.py  (fork 31L vs base 15L)
FOLLOWUP GatewayRunner._update_platform_runtime_status           -> gateway/run_shutdown.py  (fork 25L vs base 26L)
FOLLOWUP GatewayRunner._increment_restart_failure_counts         -> gateway/run_shutdown.py  (fork 24L vs base 26L)
FOLLOWUP GatewayRunner._restore_moa_one_shot                     -> ABSENT  (fork 23L vs base 17L)
FOLLOWUP GatewayRunner._completion_delivery_identity             -> gateway/run_notifications.py  (fork 22L vs base 20L)
FOLLOWUP GatewayRunner._handle_message_with_agent                -> gateway/run_turn.py  (fork 20L vs base 2299L)
FOLLOWUP GatewayRunner._should_send_telegram_lobby_reminder      -> gateway/run_topics.py  (fork 19L vs base 19L)
FOLLOWUP GatewayRunner._load_service_tier                        -> gateway/run_config_loaders.py  (fork 19L vs base 18L)
FOLLOWUP GatewayRunner._restore_session_model_override           -> gateway/run_agent_cache.py  (fork 19L vs base 13L)
FOLLOWUP GatewayRunner._should_send_telegram_capability_hint     -> gateway/run_topics.py  (fork 18L vs base 18L)
FOLLOWUP GatewayRunner._async_delegation_group_key               -> ABSENT  (fork 18L vs base 17L)
FOLLOWUP GatewayRunner._deliver_completion_notification          -> gateway/run_notifications.py  (fork 14L vs base 155L)
FOLLOWUP GatewayRunner._format_coalesced_async_delegations       -> ABSENT  (fork 12L vs base 11L)
FOLLOWUP GatewayRunner._run_agent_inner                          -> gateway/run_turn.py  (fork 12L vs base 1851L)
FOLLOWUP GatewayRunner._clear_restart_failure_count              -> gateway/run_shutdown.py  (fork 11L vs base 22L)
FOLLOWUP GatewayRunner._deliver_async_delegation_group           -> gateway/run_notifications.py  (fork 9L vs base 104L)
FOLLOWUP GatewayRunner._active_work_count                        -> gateway/run_shutdown.py  (fork 8L vs base 7L)
```

## B. Fork-ADDED GatewayRunner methods kept on the class in run.py but UNWIRED (their callers were in
methods from section A, which upstream moved). They are reachable as `GatewayRunner.<name>` (so fork unit
tests that call them directly still resolve) but nothing invokes them until A is re-threaded.
Fixpoint set (58 methods incl. transitive helpers); HEAD line ranges:

```
_ack_turn_slot_wait HEAD:gateway/run.py L37893-37899
_announce_hygiene_compaction HEAD:gateway/run.py L31878-32042
_arm_deferred_restart_after_release HEAD:gateway/run.py L35493-35607
_cancel_pending_boot_resumes_for_shutdown HEAD:gateway/run.py L15998-16125
_claim_completion_notification HEAD:gateway/run.py L33966-33997
_clear_hygiene_compression_failures HEAD:gateway/run.py L31869-31876
_clear_pending_boot_resume HEAD:gateway/run.py L15982-15989
_deliver_async_delegation_group_in_scope HEAD:gateway/run.py L34637-34754
_deliver_completion_notification_in_scope HEAD:gateway/run.py L34014-34190
_describe_restart_requester HEAD:gateway/run.py L15586-15596
_effective_stop_drain_timeout HEAD:gateway/run.py L12470-12479
_emit_abandoned_turn_session_ends HEAD:gateway/run.py L14111-14182
_footer_reasoning_label HEAD:gateway/run.py L11896-11932
_handle_message_with_agent_admitted HEAD:gateway/run.py L26196-28902
_interrupt_restart_intent_cap HEAD:gateway/run.py L15558-15584
_is_telegram_boot_redelivered_duplicate HEAD:gateway/run.py L26028-26106
_launch_systemd_restart_shortcut HEAD:gateway/run.py L15307-15400
_live_prompt_tokens_for_session HEAD:gateway/run.py L26150-26173
_load_restart_followups HEAD:gateway/run.py L16567-16612
_log_drain_admission HEAD:gateway/run.py L16127-16152
_mark_user_stopped HEAD:gateway/run.py L36073-36128
_maybe_ack_startup_restore_queue HEAD:gateway/run.py L16665-16704
_maybe_notify_unclean_restart HEAD:gateway/run.py L15746-15823
_migrate_discord_session_keys HEAD:gateway/run.py L9622-9664
_notify_restart_loop_suspended HEAD:gateway/run.py L13902-13948
_notify_session_key_conflict HEAD:gateway/run.py L9599-9620
_persist_telegram_aggregate_constituents HEAD:gateway/run.py L26108-26148
_prepare_auto_resume_decisions HEAD:gateway/run.py L16325-16397
_prepare_boot_resume_work_check HEAD:gateway/run.py L16211-16323
_preserve_followup_across_restart HEAD:gateway/run.py L16399-16499
_prior_life_verdict HEAD:gateway/run.py L15703-15744
_reap_dead_running_agents HEAD:gateway/run.py L11401-11444
_reap_dead_running_agents_loop HEAD:gateway/run.py L11446-11461
_reap_interval_secs HEAD:gateway/run.py L11463-11469
_reconcile_deferred_restarts_at_boot HEAD:gateway/run.py L16164-16209
_record_hygiene_compression_failure HEAD:gateway/run.py L31854-31867
_recover_async_delegations_once HEAD:gateway/run.py L34417-34593
_register_deferred_restart_delivery_barrier HEAD:gateway/run.py L35663-35699
_register_pending_boot_resume HEAD:gateway/run.py L15956-15980
_rehydrate_session_overrides HEAD:gateway/run.py L9017-9063
_release_agent_off_loop HEAD:gateway/run.py L36731-36761
_report_refused_restart_followup HEAD:gateway/run.py L16614-16631
_reset_stuck_loop_counts HEAD:gateway/run.py L14444-14472
_restore_resume_pending_sessions_at_startup HEAD:gateway/run.py L17066-17071
_run_agent_admitted HEAD:gateway/run.py L37914-39850
_running_event_loop HEAD:gateway/run.py L35609-35614
_schedule_armed_deferred_restart HEAD:gateway/run.py L35616-35661
_schedule_startup_restore_queue_drain HEAD:gateway/run.py L16854-16940
_session_has_pending_boot_resume HEAD:gateway/run.py L15991-15996
_session_in_startup_resume HEAD:gateway/run.py L15940-15946
_spool_adapter_pending_for_restart HEAD:gateway/run.py L16501-16526
_spool_one_adapter_pending HEAD:gateway/run.py L16528-16565
_stale_lease_notice_text HEAD:gateway/run.py L35828-35844
_stamp_idle_gap_anchor HEAD:gateway/run.py L36799-36834
_stamp_reaper_heartbeat HEAD:gateway/run.py L11471-11476
_startup_restore_gate_watchdog HEAD:gateway/run.py L16942-16968
_sweep_restart_initiated_breadcrumbs HEAD:gateway/run.py L15054-15106
_sweep_resume_requests HEAD:gateway/run.py L17073-17138
```

## C. TurnRunner fork-added methods NOT carried (class moved to gateway/run_turn_runner.py)
Feature: chat model pins + cross-session reply mismatch announcements (MUST-survive list).

```
_announce_chat_pin_mismatch (method:TurnRunner, in_base=False, L5438-5457)
_flush_route_notice_outbox (method:TurnRunner, in_base=False, L6632-6682)
_queue_undelivered_route_notice (method:TurnRunner, in_base=False, L6619-6630)
_route_notice_chat_key (method:TurnRunner, in_base=False, L6612-6617)
```

## D. In-base symbols upstream DELETED from run.py that the fork had modified or that fork tests reference
(no sibling defines them; port into the sibling that owns the replacement, or retire the fork test)

```
_async_delegation_group_key (method:GatewayRunner, in_base=True, L34596-34612)
_clear_planned_restart_notification (top, in_base=True, L2746-2747)
_completion_notification_batch_key (method:GatewayRunner, in_base=True, L34193-34202)
_empty_honcho_cache_busting_config (method:GatewayRunner, in_base=True, L35145-35146)
_ensure_ssl_certs (top, in_base=True, L2661-2710)
_extract_honcho_cache_busting_config (method:GatewayRunner, in_base=True, L35149-35189)
_format_coalesced_async_delegations (method:GatewayRunner, in_base=True, L34615-34625)
_has_setup_skill (method:GatewayRunner, in_base=True, L9235-9241)
_iter_gateway_adapters (method:GatewayRunner, in_base=True, L20082-20101)
_log_background_boot_send_result (method:GatewayRunner, in_base=True, L17232-17236)
_log_background_resume_result (method:GatewayRunner, in_base=True, L17141-17150)
_log_late_background_failure (method:GatewayRunner, in_base=True, L17153-17168)
_reply_anchor_for_event (method:GatewayRunner, in_base=True, L32150-32152)
_restore_moa_one_shot (method:GatewayRunner, in_base=True, L25260-25282)
_session_expiry_watcher (method:GatewayRunner, in_base=True, L19879-20076)
_try_resolve_fallback_provider (top, in_base=True, L3895-3942)
exit_code (method:GatewayRunner, in_base=True, L9596-9597)
exit_reason (method:GatewayRunner, in_base=True, L9592-9593)
should_exit_cleanly (method:GatewayRunner, in_base=True, L9584-9585)
should_exit_with_failure (method:GatewayRunner, in_base=True, L9588-9589)
```

## E. Renames applied in run.py that the siblings/other lanes must mirror
- `_adapter_for_source` was split upstream (c70565ef30) into `_intake_adapter_for` / `_delivery_adapter_for`
  and the fork's `gateway/authz_mixin.py` definition is gone in the merged tree. 30 fork call sites in run.py
  (all sends/typing/progress/pending-merge → delivery seam) were renamed to `_delivery_adapter_for`.
  HOTSPOT: `gateway/slash_commands.py` still carries 4 `getattr(self, "_adapter_for_source")(...)` sites
  (L3242, L5137, L7001, L7325 in the merged working tree) and fork TurnRunner bodies use
  `self._runner._adapter_for_source(ctx.source)` — both dangle until renamed.
- `_CronDispatchGate` (fork-only; shared-checkout admission for cron ticks via `can_dispatch.admit`) was
  restored in run.py together with its `start_gateway` call site; `cron/scheduler.py` (lane-owned elsewhere)
  must keep the fork's `getattr(can_dispatch, "admit", None)` hook.
- `SecondaryPortBindingConfigError` (fork-only) restored in run.py; its raise/except sites live in
  `_start_secondary_profile_adapters` / `_start_one_profile_adapters` → run_adapters.py (section A).
- fork_ext call sites: :2 had 19 `fork_ext` lines (3 imports, 3 comments, 13 call sites); merged run.py has
  11 (3 imports + 8 call sites inside fork-added methods of section B). The 5 call sites that lived in
  `request_restart` (record_in_band_restart), `_drain_startup_restore_queue` (acknowledge_followup),
  `_handle_active_session_busy_message` (comment) and the drain_resume/restart_followups imports inside
  `start`/`stop` bodies move with section A.
