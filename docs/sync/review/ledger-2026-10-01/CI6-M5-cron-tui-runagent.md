# CI6 ledger — lane M5-cron-tui-runagent (parity PR #1624, CI round 6)

Branch `sync/upstream-2026-10-01-ci-M5-cron-tui-runagent` off fold head
`93556253a7f864a90aec10a224213ba0ad6b5c5e`. Card t_cb0e8557 (3 runs; runs 1–2 hit the 300-iteration
cap, run 3 finished). Reds from CI run 37038759229; manifest `tools/lanes-r5/M5-cron-tui-runagent.md`
(43 files). Verified narrowly per file through `~/.hermes/scripts/test-gate` with a sandboxed HOME,
≤3 files per call, never the full suite. Commits a492d49e7e … 886750eaea (16).

Legend: FIXED-CODE (fork behaviour restored onto upstream's structure) · FIXED-TEST (test pinned a
moved symbol / stale shape / merged copy) · PINNED-UPSTREAM-CONTRACT (upstream-only test adapted to a
fork rule the fork rejected on purpose) · RETIRED · FOLLOWUP->lane.

| red file | disposition | what / why | final narrow result (head 886750eaea) |
|---|---|---|---|
| tests/acp_adapter/test_server.py | FIXED-CODE | `compress_now` passes `trigger_reason="manual_compress_command"` (same bytes as L1/M3) | 83 passed (batch with test_session + test_evaluate_needs) |
| tests/acp_adapter/test_session.py | FIXED-TEST | `get_messages_as_conversation(include_timestamp=True)` — fork F01 keeps the default projection byte-stable | ↑ |
| tests/ci/test_evaluate_needs.py | FIXED-TEST | upstream dropped pyyaml (284dbaf5370); read ci.yaml via `hermes_yaml` | ↑ |
| tests/ci/test_no_new_source_proxy_asserts.py | FIXED-TEST | `source_proxy_baseline.json` re-baselined on the merged test set (114 -> 67; deleted/rewritten entries removed, relocated key re-keyed, 3 upstream-authored + 1 L2 proxy frozen, not endorsed) | 27 passed (batch) |
| tests/context_engine/test_lcm_adoption_smoke.py | FIXED-CODE | `scripts/probe_hermes_lcm_isolated.py` compares live roots lexically; the merged `tests/home_io_guard` refuses `realpath()` under the real home | 6 passed under a non-temp HOME (`t_cb0e8557/fakehome`); the 2 `refuses_live_*` cases are vacuous under a `mktemp` HOME because the probe's own `tmp` marker whitelists it — same on CI? no: CI HOME is `/home/runner` (not temp), so green there |
| tests/cron/test_cron_no_agent.py | FIXED-TEST | wording from the merged copy table (`cron/scheduler_failure_copy`) | ↑ batch 27 passed |
| tests/cron/test_cron_pinned_job_fallback.py | FIXED-CODE | `_resolve_job_fallback_chain`: model/endpoint-only pins never borrow the global chain (upstream #100437) while the fork same-provider filter still governs provider pins | 51 passed (batch) |
| tests/cron/test_cron_script_exit_house_page.py | FIXED-TEST | merged copy table wording | ↑ |
| tests/cron/test_cron_script_job_timeout.py | FIXED-TEST | `monitor.py` reads `_run_job_script` from `cron.scheduler_script`; patch there | ↑ |
| tests/cron/test_cron_shared_scripts_and_stuck_page.py | FIXED-CODE + FIXED-TEST | `_upsert_incident_for_failure(escalation=)`: the fork one-time stuck page (t_04822736) is not swallowed by upstream's repeat-alert cooldown; tick-2 counts follow the merged default cooldown | 35 passed (batch) |
| tests/cron/test_cron_transient_failure_suppression.py | FIXED-CODE | transient-failure page suppression skips agent-declared `[CRON_FAILURE]` evidence (fork rule) | ↑ |
| tests/cron/test_init_fallback_job_chain.py | FIXED-TEST | patch targets moved with upstream's split (`cron.scheduler_delivery._resolve_origin`, `tools.mcp_tool_discovery`) | ↑ |
| tests/cron/test_lifecycle_guard_heredoc_walk.py | PINNED-UPSTREAM-CONTRACT | fork rule pc-fb1bd018: a non-executable oversized file at command position is data; fixture chmod +x so the walk is still proven | 25 passed (batch) |
| tests/cron/test_oneshot_restart_catchup.py | FIXED-TEST | the tick's `claim_job_for_fire` between scans restores an offered-but-unclaimed slot by design (#107485) | ↑ |
| tests/cron/test_parallel_pool.py | FIXED-TEST | `mark_execution_running` stub returns a row; None now means lost ownership (upstream) | ↑ |
| tests/cron/test_run_one_job.py | FIXED-TEST | merged copy / split patch targets (commit 560c4ad4ef) | 34 passed (batch) |
| tests/discord/test_restart_backfill.py | FIXED-TEST | doubles accept `channel_ids=None` like the real `_is_allowed_user` (upstream routed the re-inject through `_discord_message_admission`) | ↑ |
| tests/discord/test_restart_backfill_e2e.py | FIXED-TEST | same | ↑ |
| tests/install/test_dev_sandbox_fetch_retry.py | RETIRED | pins the INSTALL_REF/UPSTREAM_URL fetch block of dev-sandbox.sh that upstream ea4cd375f8 retired with the bubblewrap sandbox (L12 ruling: FOLLOWUP delete); file deleted | n/a |
| tests/providers/test_fork_canary_plugin_discovery_not_at_import.py | FIXED-CODE | `hermes_cli/models_catalog_static.py` no longer runs `sync_plugin_provider_catalog()` at import (fork #816 canary: import-order partial-init registered 0 providers) | 19 passed (batch) |
| tests/run_agent/test_local_relay_restart_wait.py | FIXED-CODE | `turn_recovery.route_classified_error`: loopback relay restart wait restored (fork 2026-09-28) | ↑ |
| tests/run_agent/test_malformed_provider_stream_failover.py | FIXED-CODE | `settle_unrecovered_error` client-error fallback gated on `should_fallback` alone (fork #195, no fallback for request-build errors); upstream's `test_malformed_tool_args_no_fallback` adapted | ↑ |
| tests/run_agent/test_relay_deploy_drain.py | FIXED-CODE | relay deploy-drain 503 wait (t_826861ab) restored; `stream_parse` joins the transport-failure reasons (#605) | 124 passed (batch) |
| tests/run_agent/test_run_agent_conversation.py | FIXED-TEST | merged `turn_failure_copy` wording; #111761 structured reasoning on clean stop; legacy 3-retry ladders set `_empty_guard_enabled=False`; `jittered_backoff` patched at `agent.retry_utils`; `tests/run_agent/conftest.py` names the underscore autouse fixtures `import *` dropped | ↑ |
| tests/run_agent/test_run_agent_providers.py | FIXED-TEST | `resolve_anthropic_token` moved to `agent.anthropic_credentials` | ↑ |
| tests/run_agent/test_run_agent_streaming.py | FIXED-TEST | request bodies moved to `_NonStreamRequest` / `_StreamingCall` | 105 passed (batch) |
| tests/run_agent/test_run_agent_tool_exec.py | FIXED-CODE + FIXED-TEST | `run_agent.py` re-exports `_extract_parallel_scope_path` / `_paths_overlap` / `_should_parallelize_tool_batch` (fork tests import off the facade); todo -> todo_list; ownership test on `INLINE_TOOL_EXECUTORS` | ↑ |
| tests/run_agent/test_session_persistence_cause.py | FIXED-TEST | F04 ruling: upstream i18n default when no agent evidence | ↑ |
| tests/scripts/desktop_update/test_desktop_update_windows_cwd.py | WINDOWS-ONLY (skip here) | nothing changed in this lane; red was the shared Windows-lane bytecode/env shrapnel (see test_mint_launchers) — CI Windows lane is the arbiter | 8 skipped (darwin) |
| tests/scripts/desktop_update/test_desktop_update_windows_timestamp.py | WINDOWS-ONLY (skip here) | same | ↑ |
| tests/scripts/test_mint_launchers.py | FIXED-TEST | the fork runner exports `PYTHONDONTWRITEBYTECODE=1` to test subprocesses; the relocated-launcher test (asserts WHERE bytecode lands) now also drops it from the child env | ↑ (Windows-only; CI arbiter) |
| tests/scripts/test_run_tests_parallel_stdio.py | FIXED-TEST | the fork runner drains `proc.stdout` by fd and polls `wait()`; double gets a real EOF fd + `wait()` | 12 passed (batch) |
| tests/state/test_writer_conn_thread_safety.py | FIXED-CODE + FIXED-TEST | `hermes_state._halt_db_corrupt` takes `self._lock` around `_disable_close_time_checkpoint()` (cross-thread setconfig race); the lexical audit exempts upstream's `__init__`-only / lock-held-by-caller helpers and adds a caller check (RED-proof: reverting the fix reports exactly `(1556, '_halt_db_corrupt', '_disable_close_time_checkpoint')`) | ↑ |
| tests/tui_gateway/contracts/test_generated.py | FIXED-CODE | fork wire rows added to upstream's extracted contracts tree: `session.redo` params/result, `prompt.submit.system_context`, `SessionActiveItem.pinned`, `SessionUndoResult.rewound_ids/prefill_text`; `apps/shared/src/gateway-contract.*` regenerated (additive) | ↑ |
| tests/tui_gateway/test_compute_host.py | FIXED-CODE | `methods_prompt` busy path threads `system_context=` into `_handle_busy_submit` (queued submits keep their own context) | 20 passed, 1 skipped (batch) |
| tests/tui_gateway/test_compute_host_late_compress_ack.py | FIXED-TEST | compress-wait ceiling asserts the fork reconciler's value (`resolve_context_compression_timeouts`), >= upstream's pinned 330 | ↑ |
| tests/tui_gateway/test_foreground_notification_snapshot.py | FIXED-CODE | `_kb_poll_board` keeps the fork done-unsubscribes rule (`kanban_db.notify_sub_is_final`, t_6d6e9467); `cli_chat_turn_mixin` redo clear reads `session_id` via getattr | ↑ |
| tests/tui_gateway/test_kanban_notify_poller.py | FIXED-CODE | `_notif_submit` returns the submit verdict with the fork tri-state (only an explicit `False` is a refusal; `None` = submitted) so a refused turn re-buffers the cursor-claimed kanban batch and a delivered one is never duplicated | 89 passed (batch) |
| tests/tui_gateway/test_protocol.py | FIXED-CODE | contracts rows above | ↑ |
| tests/tui_gateway/test_seeded_session_create.py | FIXED-CODE | `SessionActiveItem.pinned` (#186) | ↑ |
| tests/tui_gateway/test_tui_gateway_server.py | FIXED-CODE + FIXED-TEST | 46 reds -> 0. Code: refused notification turns keep their durable receipts (`release_event_delivery`) and accepted ones use `complete_event_delivery_with_retry`; orphan delegation drops advance `delivery_attempts` (Greptile P2 #408); `config.set fast` restored to the fork's route-gated `resolve_fast_mode_capability` (provider + api_mode, config-inferred pre-build, reason text); `config.set model` waits for an in-flight build before the failed-build recovery; live `/prompt` slash read; manual compress no-op returns the caller's history object and the commit skips the swap/version bump on identity. Tests: hand-seeded `completion_queue` tests pin `_completions_restored=True` (upstream's startup replay age-drops the epoch-old fixture row); compress ceiling asserts the fork reconciler's value | 752 passed (batch with turn_system_context + warning_callback_conservation) |
| tests/tui_gateway/test_turn_system_context.py | FIXED-CODE | `prompt.submit.system_context` contract row + busy-path threading | ↑ |
| tests/tui_gateway/test_warning_callback_conservation.py | FIXED-TEST | upstream-only Agent double's `_touch_activity(*args)` accepts the fork `progress=False` kwarg | ↑ |

## Notes for the fold

- Local-runner trap found this run: exporting `HERMES_HOME=<mktemp>/.hermes` makes `tests/conftest.py`
  register that dir as a guarded "custom real root" (`_PRE_SANDBOX_HERMES_HOME`), and the home-io guard
  then 5072s every `prompt.submit` that opens `state.db` — 4 order-dependent reds in
  test_tui_gateway_server that CI never saw. Sandbox `HOME` only (`e45-pt-M5b.sh`).
- Blast radius noted, not this lane's: `tests/hermes_state/test_wal_checkpoint_strategy.py::test_matching_error_inside_callback_is_not_replayed`
  (red on the fold head before this lane) and `test_plugin_provider_picker_admission` /
  `test_models_catalog_late_plugin_provider` errors ("additive container") — both reproduce with this
  lane's edits reverted. FOLLOWUP->L5-hermes-cli / L8 owner.
- `agent/conversation_compression_manual.py` `trigger_reason` hunk is byte-identical to M3-agenta's
  06ab9f2d3f; folds clean either order.
