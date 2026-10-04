# Fork call-site coverage

Inventory of every `docs/sync/fork-features.json` entry whose `parity_census.verdict` is
KEPT-FORK or PARTIAL-UPSTREAM, checked for one property: **does a registered or existing test
drive the real upstream call site the fork threads its input into, and assert the user- or
wire-visible effect?** (t_96049446, 2026-10-04.)

Why this matters: t_829a3079. The run.py→run_turn.py split kept the `build_footer_line` call and
dropped six fork kwargs. Every registered footer test stayed green, because they tested the
producer and the renderer, never the consumer call. A helper that works is not proof that the
upstream function still calls it with the right input.

Verdicts:

- **covered**: a test drives the call site (or the upstream function that contains it) and
  asserts the visible effect.
- **presence-only**: tests prove the fork helper exists and behaves, but nothing proves the
  upstream call site passes it the right input. A merge that drops the call, or re-wires its
  input, stays green. Source-text tests count here (they are also banned by AGENTS.md).
- **none**: no test touches the behaviour.
- **n/a**: no call-site shape. Pure data, registry rows, import-time laziness, or test hygiene.

Lint (D2b, `scripts/hermes_parity/lint_manifest.py::lint_call_sites`): an entry that sets
`call_site` must list `call_site_tests` (nodeids that drive it), each also in `tests` so the
collection lint covers it. The footer and restart-policy entries declare `call_site` today. Each presence-only
row below has a child card of t_96049446. That card adds the e2e tests AND the `call_site` /
`call_site_tests` fields, after which the lint holds the line.

| # | Feature | Call site (upstream function the fork threads into) | Test that drives it | Verdict |
|---|---|---|---|---|
| 0 | chat model pins + cross-session reply mismatch | `GatewayRunner` wake/route path → `gateway/chat_model_pins.py` | `tests/gateway/test_route_change_turn_contract.py::test_cross_session_pin_mismatch_announces_on_emitted_turn` (real `TurnRunner`) | covered |
| 2 | systemd restart exits 0, launchd 75 | `GatewayRunner.stop(restart=, service_restart=)` | `tests/gateway/test_gateway_shutdown.py::test_gateway_stop_systemd_service_restart_exits_cleanly` (asserts `_exit_code == 0`) | covered (darwin variant: see entry `coverage_gap`) |
| 3 | hygiene compaction announces in-chat | gateway hygiene path in the turn → `_emit_compaction_announce` | `tests/gateway/test_session_hygiene.py::test_hygiene_msgcount_announces_limit_not_count` (drives the gateway, asserts the delivered 🗜️ text) | covered |
| 4 | messaging + moa toolsets present | `toolsets.py` data | `tests/agent/test_fork_custom_toolsets.py` | n/a |
| 6 | cron per-job reasoning + script timeout | `cron/scheduler.py::_job_script_kwargs` → `_run_job_script(timeout_seconds=…)` on the `script` path | monitor path only: `tests/cron/test_cron_script_job_timeout.py::test_monitor_script_passes_job_ceiling` (not registered). `script` path: `test_cron_workdir.py` swallows `**_fork_kwargs` without asserting them; `TestRunJobScript` asserts output only | **presence-only** |
| 7 | relay-pool session affinity + lane headers | `agent/chat_completion_helpers.py::_build_anthropic_kwargs` (merges `_pool_affinity_headers` into the request, ~L2190) | `tests/agent/test_pool_affinity_call_site.py` (real `AIAgent._build_api_kwargs` on `anthropic_messages`; wiring + wire test per header, non-pool scope; t_67fc3115) | covered |
| 8 | restart failure count codec | `gateway/fork_ext/restart_codec.py` (pure codec + goldens) | golden + codec tests | n/a |
| 9 | restart policy, config bridge, initiator breadcrumb | `gateway/run.py::_bridge_config_to_env` (startup, calls `_bridge_agent_config_to_env`, ~L2178/2220) | `tests/gateway/test_restart_config_bridge_call_site.py`: `test_wiring[<key>]` + `test_effect[<key>]` for each of the 7 fork-only keys (drives the real startup function, reads the live reader, regresses to the stale preset with the call dropped); `test_restart_breadcrumb_frozen_contract_is_consumed` replaces the source-text check | covered |
| 10 | configured + persisted route identity helpers | `GatewayRunner` session route lookup → `PersistedSessionRouteLookup` | `tests/gateway/test_session_model_reset.py` (drives `GatewayRunner`) | covered |
| 12 | state helpers: denorm gate + platform session search | `SessionDB` list / search paths | `tests/hermes_state/test_session_list_denorm_reland.py::test_flag_on_denorm_path_matches_cte_oracle_byte_for_byte`, `tests/test_hermes_state_core.py::TestSearchSessionsByTitle` | covered |
| 14 | /undo, /redo | gateway slash dispatch → half-turn rewind | `tests/gateway/test_undo_redo_half_turn.py` (drives `GatewayRunner` with a real `SessionDB`) | covered |
| 15 | /branch (alias /fork) + Discord thread spawn | `gateway/slash_commands_session.py::_handle_branch_command` (fork residuals: `_branch_point_len`, `_branch_post_thread_intro`) | `tests/gateway/test_discord_branch_thread_merge.py::TestBranchDiscordThread` | covered |
| 16 | /merge | gateway slash dispatch → merge handler | `tests/gateway/test_discord_branch_thread_merge.py::TestMergeCommand` | covered |
| 17 | /fast | turn request build → `resolve_fast_mode_capability` | `tests/gateway/test_turn_request_overrides.py::test_provider_request_overrides_merged_under_fast_mode` (not registered); `tests/gateway/test_fast_command.py` | covered (shadow-handler defect tracked by t_0b4d7394) |
| 18 | discord free-response: scalar coercion + quoted-mention exemption | `plugins/platforms/discord/adapter.py` message gate (`other_bots_mentioned` block, ~L2093–2113) | coercion: `_discord_free_response_channels()` is called directly (covered). Quoted-mention: the canary *replicates* the gate's logic instead of driving the adapter's message path | **presence-only** (quoted-mention half) |
| 19 | telegram intake sentinel | `TelegramAdapter` app setup: `app.add_handler(TypeHandler(Update, self._observe_intake_update), group=-1)` (~L3124) | `_observe_intake_update` is driven directly (covered). Registration and group=-1 are checked only by reading source text (`test_telegram_intake_sentinel.py` ~L225, canary ~L110) | **presence-only** (registration half) |
| 20 | cron refuses cross-vendor model/provider pairs | `tools/cronjob_tools.py::cronjob(action="create"/"update")` | `tests/tools/test_cronjob_tools.py::TestModelProviderVendorConsistency::test_create_rejected_on_cross_vendor_pair`, `…::test_update_rejected_when_only_provider_changes` | covered |
| 21 | test hygiene: evict MagicMock telegram modules | test-only | — | n/a |
| 22 | runtime footer fork fields | `gateway/run_turn.py::GatewayTurnMixin._hmwa_runtime_footer_line` → `build_footer_line` | `tests/gateway/test_footer_consumer_in_turn.py` (end-to-end render) + `tests/gateway/test_footer_consumer_kwargs_isolated.py` (per-kwarg wiring + render, each red under its own mutation) | covered (`call_site` declared, lint-enforced) |
| 23 | model/provider transitions announce once | `TurnRunner` → `_emit_fallback_announce` | `tests/gateway/test_route_change_turn_contract.py::test_every_cause_announces_route_change_in_same_turn` (real `TurnRunner`) | covered |
| 24 | effort-only changes announce symmetrically | `TurnRunner` re-init / fallback → announce | `tests/gateway/test_route_change_turn_contract.py::test_reinit_route_or_effort_change_is_delivered_by_real_turn_runner` | covered |
| 25 | Claude CLI usage caps classify as quota | `agent/error_classifier.py::classify_api_error` (upstream classifier, fork branch) | `tests/agent/test_claude_cli_usage_cap_classification.py` (calls `classify_api_error`, asserts `FailoverReason` + announce text) | covered |
| 26 | provider plugin discovery never at import | import-time laziness | `tests/providers/test_fork_canary_plugin_discovery_not_at_import.py` | n/a |
| 27 | provider-registry generation seam | `refresh()` triggers inside recognition paths | `tests/test_provider_registry_seam.py::test_refresh_triggers_run_before_recognition` | covered |

Rows 1, 5, 11, 13 and 28 are ABSORBED-UPSTREAM, EQUIVALENT-UPSTREAM or have no census verdict,
so they are out of scope.

Method: AST import scan of each registered test file, then a grep for the call-site function in
`tests/`, then a read of the deciding test bodies. "Covered" rows are judged on the test named in
the Test column, which may not be in the entry's `tests` list (rows 6, 17). Those can be added when
their entries next change.
