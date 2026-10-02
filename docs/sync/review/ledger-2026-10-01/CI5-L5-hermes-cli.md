# CI round-5 lane L5-hermes-cli — parity PR #1624 (card t_8a42c8f2)

Base 117c1c249d60f49b57c476a3d8ec049e2c2d78b3; branch `sync/upstream-2026-10-01-ci-L5-hermes-cli`
(9 commits: 5e3c62f8cfd 7221dd80279 7474a904f03 3b97049d925 f06d878ee17 48e41edc9fd e163498fc0f
76929da3137 5201167ef71). Manifest: 31 files / 66 reds (run 36967435601). Narrow runs through
`~/.hermes/scripts/test-gate` with a sandboxed HOME, max 3 files per call; "fork/main" = `$R/base`.

Result: 30/31 files green; 1 file (test_model_alias_entry_points) carries 2 INHERITED reds that
fail identically on fork/main. 64/66 reds fixed.

| red file | class | what / why | narrow result (head 5201167ef71) |
|---|---|---|---|
| tests/cli/test_chrome_launcher_keychain_guard.py | FIXED-CODE | tools/bot_desktop/browser.py dock_argv carries `--password-store=basic --use-mock-keychain` (fork guard: every detached launch). | 3 passed (33 w/ group) |
| tests/cli/test_fast_route_capability.py | FIXED-CODE | upstream's parallel /fast (agent/fast_mode.py, tui_gateway/server.py, methods_config_set.py, gateway/slash_commands_model.py) called the model-only wrapper; each now asks the fork's route-aware `resolve_fast_mode_capability` (+ optional `base_url` live-endpoint gate). fast_mode_contracts: anthropic_fast = opus-5-5/opus-5/opus-4-8, dotted spellings normalised. | green |
| tests/hermes_cli/test_auth_codex_provider.py | FIXED-TEST | pool force-refresh goes through the fork owner transaction (`refresh_codex_oauth_pure`), not `load_pool().try_refresh_matching`; test double repointed. | green |
| tests/hermes_cli/test_auth_codex_quota_probe.py | FIXED-CODE | agent/codex_owner.py: after the probe refreshes an expired stored token the pool row holds the fresh pair; cooldown cleared on that row, not the stale snapshot (which wrote the expired token back). | 152 passed (group) |
| tests/hermes_cli/test_cli_hint.py | FIXED-CODE+TEST | hermes_cli/main_tui_launch.py: `-c "<title>"` resume hint via `cli_hint.hint_value` (titles with `$HOME`/`` `id` `` must not expand). Test: patch site moved to main_tui_launch, repair helpers now in hermes_state_repair, `SessionDB(read_only=True)` kwarg on the fake. | green |
| tests/hermes_cli/test_config_set_preserves_comments.py | FIXED-CODE | hermes_cli/config.py: the merge dropped the fork `config set` writer for upstream's full-state ruamel replace; restored targeted edit -> roundtrip -> block dump, refuse-with-diff on comment loss unless --force, `config.yaml.bak-configset-*` sibling. hermes_yaml.compose() added (PyYAML gone upstream). | green |
| tests/hermes_cli/test_cron_list_json.py | FIXED-CODE | hermes_cli/cron.py: `cron list` asks the store for `include_disabled=show_all` (fork --json contract) and tops the human table up with paused jobs (upstream contract kept). | 18 passed (group) |
| tests/hermes_cli/test_dashboard_state_db_log.py | FIXED-CODE | hermes_cli/web_server.py: module-level `get_hermes_home` facade symbol (test patches it). | green |
| tests/hermes_cli/test_desktop_cron_ticker_gateway_standdown.py | FIXED-TEST | upstream-only test; fake admits the fork `can_dispatch` kwarg (#1373). | green |
| tests/hermes_cli/test_fast_command.py | FIXED-CODE+TEST | see fast_route row; proxy branch pins `{}` (fork `{}`-when-tier-set contract, same as sibling tests in the class). | 45 passed (group) |
| tests/hermes_cli/test_gpt6_tiers_registration.py | FIXED-TEST | upstream-only test; gpt-6.1-sol IS -900k eligible on the fork (#1550, measured 922k): assertion flipped to the fork contract. | green |
| tests/hermes_cli/test_lazy_canonical_providers.py | FIXED-CODE | hermes_cli/models_catalog_static.py: dropped the import-time `sync_plugin_provider_catalog()` (list_providers imports plugins that import hermes_cli.models -> partially initialised module); the post-discovery sync hook still runs it. | green |
| tests/hermes_cli/test_lazy_command_exports.py | FIXED-TEST | tests/hermes_cli/conftest `_no_stale_module_purge` honours `@real_concurrent_gate` (frozen updater surface test needs the real purge). | 36 passed (group) |
| tests/hermes_cli/test_managed_scope_test_context.py | FIXED-CODE | hermes_state_guard.py re-exports the test-context predicate from the leaf `hermes_test_context` (one definition; hermes_state / managed_scope bind the same function). | green |
| tests/hermes_cli/test_model_alias_entry_points.py | FIXED-CODE (2) + INHERITED (2) | tools/delegate_tool_config.py: `delegation.model` resolves model.aliases / provider prefix at read time (`resolve_model_pair_for_storage`) -> 2 delegation reds fixed. `test_kanban_stored_alias_reaches_worker_argv_resolved` + `test_kanban_edit_then_spawn_never_emits_a_mismatched_pair` fail on fork/main too (`$R/base`: 2 failed, 25 passed) -> INHERITED, not chased. | 2 failed (inherited), rest green |
| tests/hermes_cli/test_model_prefix_routing.py | FIXED-TEST | upstream-only tests unpack the fork's `(merged, ok)` return of `_load_dict_model_aliases`. | 96 passed (group) |
| tests/hermes_cli/test_model_switch_configured_unlisted.py | FIXED-CODE | hermes_cli/models.py re-exports `validate_requested_model` (fork patch seam) and `_validate_requested_model_seam()` honours a patch on either `hermes_cli.models` (fork tests) or `hermes_cli.models_validate` (upstream tests); models_validate: a live-listing miss carries `not_listed=True` so a configured alias reports "exists but unavailable" instead of a typo. | green |
| tests/hermes_cli/test_models.py | FIXED-CODE+TEST | models_catalog_static: `anthropic/claude-opus-5.5-fast` (fork fast SKU) in the OpenRouter snapshot + `_OPENROUTER_ONLY`; curated-list assertions use the dotted id upstream verified live. | green |
| tests/hermes_cli/test_models_catalog_late_plugin_provider.py | FIXED-TEST | upstream-only test; teardown `monkeypatch.setitem(_REGISTRY)` undo = pop = TypeError on the fork's additive provider_seam. Fixture restores the pre-test generation (`provider_seam._restore`) instead. | 13 passed (group) |
| tests/hermes_cli/test_oauth_status_pool_observation.py | FIXED-CODE+TEST | hermes_cli/auth_codex.py: owner transaction raises `codex_auth_missing_refresh_token` for a singleton mirrored into the pool without a refresh token = the #68004 recovery shape; resolver falls through to the legacy read whose Codex CLI import ladder repairs it (other verdicts still raise). Test tolerates the empty `auth.lock` sidecar the owner-store load creates while pinning auth.json byte-identical. | green |
| tests/hermes_cli/test_oneshot_chat_identity.py | FIXED-TEST | upstream `_run_agent` forwards `conversation_history=` to `run_conversation`; FakeAgent accepts it (asserts None for a fresh one-shot). | 10 passed (group) |
| tests/hermes_cli/test_plugin_load_sys_modules_race.py | FIXED-CODE | fork class guard (iterate `list(sys.modules)`): two upstream-only evals scripts (evals/openrouter_pkce_ab/harness.py, evals/dashboard_auth/refresh_singleflight_live_e2e.py) iterated the live dict; snapshotted. | 15 passed (group) |
| tests/hermes_cli/test_provider_auth_seam.py | FIXED-TEST | upstream-only; fixture teardown popped `_REGISTRY`/`_ALIASES` items (additive on the fork) -> `provider_seam._restore(generation)`. | green |
| tests/hermes_cli/test_session_schema_history.py | FIXED-CODE | hermes_cli/session_schema_history.py: events 31-35 (sessions) / 03 (session_model_usage) record the fork columns (effective_last_active, last_turn_*, redo_count, compression_skew_history, *_unknown) with their fork commit labels, appended at the END per the module contract, so the salvage replay ends at the fork SCHEMA_SQL. | green |
| tests/hermes_cli/test_setup_provider_catalog.py | FIXED-TEST | upstream-only; five `monkeypatch.setitem(providers._REGISTRY)` / one `_PROVIDER_MODELS` undo = TypeError on the additive seam. Autouse fixture registers directly and restores the generation (snapshot taken after hermes_cli.models/providers are imported so every seam container is in it). | 6 passed |
| tests/hermes_cli/test_shared_metrics_loop.py | FIXED-CODE | the fork's whole-function `delegate_task` (tools/delegate_tool.py) never opened upstream's `hermes.delegation.run` accounting: `begin_delegation_run(task_list, …)` after child construction + `finish_delegation_unit` in `_execute_and_aggregate`, matching upstream `_run_batch`. tests/tools/test_delegate_depth_prompt.py: 4 reds identical with and without the hunk (pre-existing, not L5). | 15 passed |
| tests/hermes_cli/test_state_db_guard.py | FIXED-TEST | fixture purges via `purged_modules` (restored fork helper). | green |
| tests/hermes_cli/test_terminal_limits_config.py | FIXED-TEST | upstream 7cfc90bae84 split cap enforcement out of `terminal_tool()` into `_plan_execution()`; the source contract now reads both (accessor call present, no `> FOREGROUND_MAX_TIMEOUT`). | green |
| tests/hermes_cli/test_warning_callback_conservation.py | FIXED-TEST | Agent stub `_touch_activity` accepts the fork `progress=False` kwarg `_emit_wait_notice` passes. | 3 passed |
| tests/hermes_cli/test_web_server_session_search.py | FIXED-TEST | `search_sessions` lives on `hermes_cli.web_routers.sessions` (the sibling test in the file already calls it there). | 8 passed (group) |
| tests/hermes_cli/test_web_server_sessiondb_eventloop.py | FIXED-TEST | `_ReadDB` stub grows the fork title lane `search_sessions_by_title` (fork-parity in web_routers/sessions.py). | green |

## Source modules touched outside tests (overlap notes for the orchestrator)
agent/codex_owner.py, agent/fast_mode.py, evals/dashboard_auth/refresh_singleflight_live_e2e.py,
evals/openrouter_pkce_ab/harness.py, gateway/slash_commands_model.py, hermes_cli/auth_codex.py,
hermes_cli/config.py, hermes_cli/cron.py, hermes_cli/fast_mode_contracts.py,
hermes_cli/main_tui_launch.py, hermes_cli/model_switch.py, hermes_cli/models.py,
hermes_cli/models_catalog_static.py, hermes_cli/models_validate.py,
hermes_cli/session_schema_history.py, hermes_cli/web_server.py, hermes_state_guard.py,
hermes_yaml.py, tools/bot_desktop/browser.py, tools/delegate_tool.py (7-line metrics hunk; L6
owns this module), tools/delegate_tool_config.py, tui_gateway/methods_config_set.py,
tui_gateway/server.py, tests/hermes_cli/conftest.py.

## Not chased (outside the manifest, noted while verifying)
- tests/agent/test_codex_owner_*: unchanged red set (needs L2's hermes_cli/auth.py `_auth_lock_path`).
- tests/hermes_cli/test_oneshot_reasoning_and_tier.py, tests/agent/test_fast_mode_auto.py,
  tests/cli/test_fast_mode_overrides.py, tests/gateway/test_fast_command.py: same red set before/after
  the fast-mode commit.
- tests/hermes_cli/test_session_recovery_lost_and_found.py::test_unreadable_schema_without_cli_names_the_sqlite3_requirement:
  red (message no longer says `.recover`); not in any L5 manifest line, unrelated to the schema-history edit.
- tests/tools/test_delegate_depth_prompt.py: 4 reds pre-existing (verified with the delegate_tool hunk removed).
