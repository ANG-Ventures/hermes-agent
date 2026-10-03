# CI5 ledger — lane L3b-gateway-n-z-tui (parity PR #1624, CI round 5)

Branch `sync/upstream-2026-10-01-ci-L3b-gateway-n-z-tui` off `117c1c249d60f49b57c476a3d8ec049e2c2d78b3`.
Card t_5f5d90aa. Reds from CI run 36967435601 (31 files / 142 reds); manifest
`tools/lanes-r5/L3b-gateway-n-z-tui.md`. Verified narrowly per file through
`~/.hermes/scripts/test-gate` with a sandboxed HOME (`e45-pt-L3b.sh`, never the full suite);
"green" below is the file's narrow run after the fix. Fork side = `fork/main` (c14e059f8f2 in
`$R/base`). Three worker runs (18372, 18410, 18432); commits in order:
d74bc69737d, 54ae32f943f, a3e0385057f, 14723356427, 49c886b1882, f1a13160b95, 68f5e25940e,
b5457dbfdae, 1e533bd90ae, 7bb08d1df1c, 5c31e35c974, 030d15b9614, 580310cf7f7, 53100958e0f,
+ the no_network/ledger commit.

Legend: FIXED-CODE (fork behaviour restored onto upstream's structure) · FIXED-TEST (test pinned a
moved symbol / stale shape) · INHERITED (fails on fork/main too) · OPEN.

| red file | disposition | what / why | narrow result |
|---|---|---|---|
| tests/gateway/test_manual_reset_sticky_route.py | FIXED-TEST | SQLite test pins its handle through `store._db` (store resolves via `hermes_state_registry.acquire()`, patching `hermes_state.SessionDB` no longer reaches it); parse-warning test reads upstream's `config_read_errors` warning | 25 passed |
| tests/gateway/test_model_command_async_offload.py | FIXED-CODE | `/model` picker handler imports `list_picker_providers` from `model_switch_providers` and passes upstream #74003 read-path flags (cache-only catalogs) | green (16 passed batch) |
| tests/gateway/test_multiplex_routing_authz.py | FIXED-TEST | fork hermetic conftest pins `hermes_state.DEFAULT_DB_PATH`; restore the import-time sentinel (test_housekeeping_profile_scope idiom) | 19 passed (batch) |
| tests/gateway/test_no_network_reachable_from_loop.py | FIXED-TEST | (1) `validate_requested_model` moved to `hermes_cli.models_validate`; spy repointed. (2) `_compress_context` now lives in `agent/compression_facade.py` (AIAgent mixin), non-vacuity probe repointed. (3) `REACHABLE_BASELINE` re-frozen: upstream split run.py/run_agent.py so the (module, coroutine) keys moved and the one-sink-per-coroutine walker terminates the same pre-existing chains at `requests.get` (`resolve_runtime_provider>_get_model_config>_auto_detect_local_model`, present on fork/main at runtime_provider.py:340-386) instead of `urlopen`. Measured with the test's own walker: fork/main 17 sites, lane head 18; every head chain is a renamed/relocated pre-existing one, none newly reachable. The /model, /reset doors stay absent (FIXED_COROUTINES test green). | see commit; 15 passed |
| tests/gateway/test_no_sync_work_per_inbound_message.py | FIXED-CODE + FIXED-TEST | CODE: `gateway/status._RUNTIME_STATUS_LANE` + `_fence_runtime_status_lane` restored (fork #817); `submit_runtime_status_write` queues on the lane again and `write_runtime_status` fences it first (direct terminal write can never be overtaken by an older deferred one); `write_runtime_status_locked` alias restored. Upstream's `_RuntimeStatusWriter` still does the persistence behind it. TEST: the realpath counter ignores the frames `tests/home_io_guard.py` (fork-only harness, 2b98a469d39) issues from inside `Path.resolve()`'s single walk — the hot path still resolves once. | 13 passed |
| tests/gateway/test_runtime_status_write_off_loop.py (not in manifest; same module) | FIXED-TEST | spies `_prepare_runtime_status_update` (the merge step every writer passes through) instead of the removed `_write_runtime_status_unlocked` | 9 passed |
| tests/gateway/test_notify_sub_chat_type_write_canonicalization.py | FIXED-TEST | writer reads fields via `_field()`; metadata branch gained `and chat_type`; behaviour unchanged | green |
| tests/gateway/test_platform_adapter_i18n.py | FIXED-CODE | discord native slash option descriptions are catalog keys (`platform.discord.command.<cmd>.arg_*`), added to en.yaml and every locale (key parity) | 67 passed (with test_i18n) |
| tests/gateway/test_pre_agent_fallback_notice.py | FIXED-TEST | MagicMock runner returns `PersistedSessionRouteLookup('absent')` so the fork's fail-closed precheck sees no pin | 2 passed |
| tests/gateway/test_queued_followup_turn_clock_and_leftover_steer.py | FIXED-CODE | run_turn.py: queued follow-up recursing on the parent's slot re-stamps `turn.started_ts`/`busy_ack_ts`; a leftover `pending_steer` joins the `/queue` overflow instead of being dropped (fork/main run.py 39217-39260, 39547-39552) | 7 passed |
| tests/gateway/test_relay_injection_egress_priming.py | FIXED-TEST | `_inject_watch_notification` returns upstream's True on adapter acceptance (merged contract, ledger F02c) | 4 passed |
| tests/gateway/test_restart_cascade.py | FIXED-CODE + FIXED-TEST | run_turn.py finally block consumes the restart-initiated breadcrumb again (D-6, fork/main run.py:28913); the three source-scan guards concatenate run.py + run_turn.py + run_turn_runner.py | 3 passed (targeted) |
| tests/gateway/test_restart_followups_boot_replay_e2e.py | FIXED-CODE | run_startup `_drain_startup_restore_queue` uses the fork seam `_adapter_for_source` (upstream's `_intake_adapter_for` fails closed on spooled restart follow-ups under multiplexing → retried forever) | 3 passed |
| tests/gateway/test_restart_interrupt_intent_followups.py | FIXED-CODE + FIXED-TEST | run_turn `_run_agent_drain_pending` restores the draining-gateway spool (`_preserve_followup_across_restart`) — merge had re-created the 2026-09-23 follow-up data loss; anchor moved | 115 passed (batch) |
| tests/gateway/test_resume_flag_stale_clear.py | FIXED-CODE + FIXED-TEST | run_watchers `_session_housekeeping` re-threads the hourly `_clear_stale_resume_pending_flags` call dropped in upstream's move; AST guard reads the renamed block; `SessionResetPolicy` retired upstream | 6 passed |
| tests/gateway/test_resume_inflight_guidance.py | FIXED-TEST | AST wiring guard reads gateway/run_turn_runner.py (dispatch site moved) | green |
| tests/gateway/test_rich_sent_store_off_loop.py | FIXED-TEST | waits for the fork's dedicated writer thread instead of racing it (`record_async` only enqueues) | 2 passed ×2 |
| tests/gateway/test_route_change_turn_contract.py | FIXED-TEST | retry backoff lives in `agent/turn_recovery`; patch `agent.retry_utils.jittered_backoff` / `capacity_retry_wait` instead of the conversation_loop facade (67 reds → green; last 4 were the real-home I/O canary cluster, fixed with the hermetic sentinel) | 69 passed |
| tests/gateway/test_session_hygiene.py | FIXED-CODE + FIXED-TEST | `_HygieneSettings` keeps the fork's 400-message hard limit (upstream 5000) and `failure_alert_after` escalation (repeated-failure alert, cleared on a landed compaction); wording assertions read the i18n catalog | 45 passed |
| tests/gateway/test_session_model_override_persistence.py | FIXED-TEST | upstream's three new tests patch the identity-less path; on the fork a persisted identity is re-resolved via `_reresolve_model_override_credentials → switch_model`, doubles moved there; codex still-unavailable arm asserts `SessionRouteUnavailableError` (fork contract) | 9 passed |
| tests/gateway/test_session_store_never_persisted_stub.py | FIXED-CODE | `session_persistence._routing_db` honours a re-pointed `hermes_state.DEFAULT_DB_PATH` like `_db` does (index rewrite and row reads were landing in two files) | 27 passed (batch) |
| tests/gateway/test_stop_ends_background_delegations.py | FIXED-TEST | reply compared to `t('gateway.stop.stopped')` (fork wording, test_stop_honest_wording) | 7 passed (batch) |
| tests/gateway/test_telegram_noise_filter.py | FIXED-CODE + FIXED-TEST | `_GATEWAY_AUTH_ERROR_RE` regains the fork's credential-resolution / Codex-OAuth / pool-exhausted / provider-not-configured markers (fork/main run.py:754-764); one test asserts upstream's plain-language auth reply | 168 passed |
| tests/gateway/test_telegram_restart_parity.py | FIXED-TEST | AST guards treat `_start_polling_mode` as bootstrap, accept `_cold_boot_drop_pending(is_reconnect=...)`, resolve a single-assignment alias; mutation-checked (flipping the ladder to drop_pending_updates=True still fails) | 6 passed |
| tests/gateway/test_update_command.py | FIXED-CODE | run_notifications: fork stale-notice policy kept (24h configured / 5min unconfigured); runner with NO config gets upstream's flat 1h cap + wording | 115 passed (batch) |
| tests/gateway/test_warning_wiring_conservation.py | FIXED-TEST | context carries a live status adapter + run predicate; status lane patched at `safe_schedule_threadsafe` | 1 passed |
| tests/gateway/test_weixin_state_write_off_loop.py | FIXED-CODE | `ContextTokenStore.set` is sync again and dispatches through `_dispatch_weixin_json_write` (single FIFO lane, ordered, fenced at disconnect/exit) instead of upstream's per-call `to_thread` + `asyncio.Lock`; `_sync_buf_path` restored; `gateway.run._exit_after_graceful_shutdown` calls `fence_lanes_for_hard_exit` (os._exit skips the atexit fences — queued cursor/credential writes were dying with the process). tests/gateway/test_weixin.py repointed at the lane contract (fence to observe). | 14 passed; test_weixin + test_weixin_typing green; shutdown_pending_flush exit-funnel tests green |
| tests/tui_gateway/test_desktop_runtime_footer.py | FIXED-CODE + FIXED-TEST | `/footer` config.set branch (`display.runtime_footer.enabled`) restored as a `_CONFIG_SETTERS` entry; payload anchor moved to `prompt_turn._complete_turn_payload` | 10 passed |
| tests/tui_gateway/test_gui_surface_toolsets.py | FIXED-TEST | upstream renamed todo → todo_list and defers it behind the tool_search bridge; denylist asserted on the uncollapsed catalog and the assembled schema | 19 passed (batch) |
| tests/tui_gateway/test_server_no_duplicate_defs.py | FIXED-TEST | `_Supervisor` double accepts `on_late_ack`; RPC-route uniqueness scan covers every tui_gateway/*.py; compress wait is the config-derived budget (#97948) | green |
| tests/tui_gateway/test_undo_redo.py | FIXED-CODE + FIXED-TEST | `session.redo` RPC route restored beside `session.undo`; `importlib.reload(server)` trips upstream's bind_module collision guard → restore `_methods` in place; double accepts display_kind/display_metadata | 4 passed |
| tests/tui_gateway/test_ws_orphan_races.py | FIXED-TEST | eager-resume ctx carries `params={}` (fork build reads the client's declared source) | 29 passed |

## Notes for the merger

- Shared source modules touched (coordinate with L3a / other lanes): `gateway/run.py`
  (auth-error regex, `_exit_after_graceful_shutdown`, hygiene settings), `gateway/run_turn.py`,
  `gateway/run_startup.py`, `gateway/run_watchers.py`, `gateway/run_notifications.py`,
  `gateway/status.py`, `gateway/session_persistence.py`, `gateway/platforms/weixin.py`,
  `gateway/slash_commands.py`, `tui_gateway/methods_config_set.py`, `tui_gateway/methods_session.py`,
  `plugins/platforms/discord/adapter.py`, `locales/*.yaml`.
- Observed red, NOT in this manifest, pre-existing at base (reverted to confirm):
  `tests/gateway/test_no_atomic_write_reachable_from_loop.py` (2; kanban_watchers `_push_wake` chain,
  flagged by L8 too), `tests/gateway/test_shutdown_pending_flush_off_loop.py::test_no_caller_may_clear_the_pending_slot_after_the_flush_await`
  (1; the 3 exit-funnel tests in that file went green with the weixin commit),
  `tests/hermes_cli/test_update_receipt.py::test_cmd_update_boundary_finalizes_on_early_exit` (L5 territory),
  `tests/gateway/test_discord_slash_commands*.py` (14; L3a).
- No test deleted, skipped or xfailed.
