# CI round-6 lane M3-agenta — parity PR #1624 (card t_e3268a99)

Base: fold head `93556253a7` on `sync/upstream-2026-10-01`. Branch `sync/upstream-2026-10-01-ci-M3-agenta`.
Red source: CI run 37038759229. Manifest: 33 files (`tools/lanes-r5/M3-agenta.md`).
All runs narrow, via `scripts/test-gate` with a sandboxed HOME, ≤3 files per call.

Legend: FIXED-CODE (fork behaviour restored on upstream's structure) / FIXED-TEST (test followed a seam the
merge legitimately adopted; why) / PINNED-UPSTREAM-CONTRACT / INHERITED / FOLLOWUP->lane.

| file | verdict | what / why | narrow result |
|---|---|---|---|
| tests/agent/lsp/test_workspace_release.py | FIXED-TEST | upstream-only fixtures register the owning kanban row + clone a pushed-clean repo (fork's fail-closed reclamation refuses an unknown owner / empty `.git`). | 9 passed |
| tests/agent/test_a4_model_event_visibility.py | FIXED-CODE | `context_compressor._derive_trigger/_config_percent_for` read `model_thresholds`/`_config_threshold_percent` via getattr (fork `update_model` tolerated `__new__`-built doubles). | 16 passed |
| tests/agent/test_anthropic_keychain.py | FIXED-TEST | `tests/agent/conftest` fork-only `_block_real_claude_keychain` honours the `allow_macos_keychain` opt-out the root guard already honours. | 100 passed (with bedrock) |
| tests/agent/test_bedrock_adapter.py | FIXED-CODE | `bedrock_adapter.py`: `anthropic.claude-mythos-5` joins the 1M group (fork had Mythos 5 in DEFAULT_CONTEXT_LENGTHS only — the drift upstream's change-detector #74263 catches). | 100 passed |
| tests/agent/test_codex_subscription_proxy_context.py | FIXED-CODE + FOLLOWUP->L1 | `model_metadata.py`: restored the fork's `_resolve_codex_oauth_context_length` wrapper (upstream kept only `_with_source`). 14/15 green here; the last red (`test_cpa_codex_slug_uses_verified_window_under_large[gpt-6-sol-872000]`, 900000 != 872000) is the fork's measured 872K gpt-6 Codex table, which L1 already restored in `d2acfa7f22` (round 5, `sync/upstream-2026-10-01-ci-L1-agent-confab-compaction`) — NOT in fold `93556253a7` nor in `fork/sync/upstream-2026-10-01` at hand-back time. Verified: cherry-picking d2acfa7f22 onto this branch → 15 passed; not landed here (L1 owns `agent/model_metadata.py` 872K table; a second copy would conflict on the fold). | 14 passed, 1 red cleared by L1 d2acfa7f22 |
| tests/agent/test_codex_ttfb_watchdog.py | FIXED-CODE | `chat_completion_helpers.py`/`chat_completion_nonstream.py`: upstream's `_resolve_nonstream_watchdogs` extraction dropped the fork's TTFB-below-stale fast-reconnect arm and the progress-stall keepalive kill; both re-threaded. Watchdog fakes stamp attempt state via `codex_runtime._codex_watchdog_state_var`. | 44 passed (group) |
| tests/agent/test_compaction_announce.py | FIXED-TEST | announce/lint follow the call sites into `agent/turn_*.py` + the `CompressionFacadeMixin` forwarder. | 112 passed, 2 skipped (group) |
| tests/agent/test_compaction_attribution_lint.py | FIXED-CODE | `trigger_reason` labels re-threaded onto the extracted turn_preflight / turn_context_compaction / turn_overflow / turn_recovery / manual / gateway call sites (cherry-pick of L1 cb0f5645c50). | 112 passed, 2 skipped (group) |
| tests/agent/test_compaction_fallback_prompt_identity.py | FIXED-TEST | Platform is no longer a prompt-identity field (#104414, `agent/surface_switch.py`). | group above |
| tests/agent/test_compaction_stats_reconcile.py | FIXED-TEST | feasibility probe reuses the main window on a shared route (#89500): compressor double carries `context_length`/`threshold_tokens`. | group above |
| tests/agent/test_compression_last_assistant_anchor.py | FIXED-TEST | fork gates timestamps behind `include_timestamp` (#107) and rolls back compactions that introduce duplicate active tool results (t_aace5343): fixture opts in and persists tool rounds per-round like the loop. real-lcm.db replay oracles marked `allow_real_home_io` (read-only, skip when absent). | 8 passed |
| tests/agent/test_compression_safeguard_refusal_latch.py | FIXED-CODE | `context_compressor_summary.py`: `_summary_focus_is_auto` latch flag (t_bf18e600) set around the extracted `_generate_summary`, reset in finally — the merged compressor read it but nothing set it. | 311 passed (with context_compressor) |
| tests/agent/test_confab_matrix_bigargs_builtin.py | FIXED-CODE | confab tool-call notice recovery restored on the extracted loop (cherry-pick L1 0813ef705ec: `turn_response_intake`, `turn_final_response`, `turn_context.build_api_messages`). Clears the StopIteration reds. | green (matrix group) |
| tests/agent/test_confab_matrix_twins_lcm.py | FIXED-CODE | same as above. | green (matrix group) |
| tests/agent/test_confab_notice_replay_strip.py | FIXED-TEST | `build_api_messages` moved to `agent/turn_context.py` (strips `PERSISTENCE_ONLY_MESSAGE_FIELDS`); flush row dict moved to `agent/session_persistence.py`; AST pins follow + resolve the contract-set loop (RED-proofed 3 ways). | 16 passed (with streaming_e2e) |
| tests/agent/test_confab_notice_stamp_authority_m43.py | FIXED-CODE | confab recovery cherry-pick (above). | green |
| tests/agent/test_confab_notice_streaming_e2e.py | FIXED-CODE | confab recovery cherry-pick (above). | 16 passed (group) |
| tests/agent/test_context_compressor.py | PINNED-UPSTREAM-CONTRACT | fork's `(rough, real)` projection baseline `last_rough_tokens_when_real_prompt_fit` superseded by upstream 0f4587e336f (L1 round-5 ruling: restoring it reds live upstream `test_switch_waits_for_new_provider_evidence`); last straggler pins the adopted contract. | 311 passed (group) |
| tests/agent/test_credential_pool_singleton_freshness.py | FIXED-TEST | xai auth-store sync lives on consolidated `_sync_entry_from_auth_store`; Nous forced refresh adopts a peer-rotated usable key without redeeming the grant (a6f75130386) while the fork's #670 stale refusal still redeems. | 30 passed |
| tests/agent/test_credential_rotation_route_settings.py | FIXED-TEST | upstream-only; pins the fork's tri-state `SwapOutcome.SWAPPED` instead of `is True`. | 24 passed (group, re-proved this run) |
| tests/agent/test_curator_shared_scope.py | FIXED-CODE | `tools/skill_manager_guards.py`: both fork shared-curation exemptions (`is_shared_curatable_path`) re-threaded into upstream's extracted background-curator write guard. | 374 passed (group, re-proved this run) |
| tests/agent/test_error_classifier.py | FIXED-TEST | frozen enum grows by upstream's five members; strict role-alternation 400s are `role_alternation` (merge-and-retry via `agent/moa_alternation.py`) rather than terminal `malformed_conversation`; `_FALLBACK_REASON_LABELS` keyed by member. | 374 passed (group) |
| tests/agent/test_fallback_policy.py | FIXED-TEST | single-reader guard allowlists the writer's two extracted halves (`_fallback_chain_exhausted`, `_announce_fallback_switch`). | 374 passed (group) |
| tests/agent/test_fallback_reason_threading.py | FIXED-CODE | `turn_api_call.py`/`turn_truncation.py`: three fork failover reasons (rate_limit guard, content-filter stream kill, safety refusal → content_policy_blocked) re-threaded; test follows the 12 call sites into `agent/turn_*.py`. | 24 passed (group) |
| tests/agent/test_fallback_reasoning_override.py | FIXED-TEST | inspects `_apply_fallback_reasoning_override` (helper upstream extracted); pinned structure byte-identical to the fork arm. | 24 passed (group) |
| tests/agent/test_fast_mode_auto.py | FIXED-TEST | upstream-only. `_resolve_turn_agent_config` with ANY tier set returns `{}` not `None` (fork contract `overrides or {}`, same ruling L5 applied to `tests/hermes_cli/test_fast_command.py`; `None` is reserved for no tier). Two `is None` pins → `== {}`. | 5 passed |
| tests/agent/test_glm_context_window_resolution.py | FIXED-TEST | upstream-only. Fork resolves unmapped (aggregator) providers through `lookup_models_dev_context_any_provider` BEFORE the catalog; the hyphenated-slug test pins the catalog, so the network step is patched out. | 30 passed (group) |
| tests/agent/test_gpt61_sol_900k.py | FIXED-CODE | codex ctx facade (above). | 31 passed (group) |
| tests/agent/test_gpt61_sol_prestage.py | FIXED-CODE | codex ctx facade (above). | 31 passed (group) |
| tests/agent/test_grid_sweep_cli_tui_stream_diagnostics.py | FIXED-TEST | upstream-only. Fork renders warnings with the emoji-presentation glyph (U+26A0 U+FE0F, #674); assertion matches base codepoint + text. | 30 passed (group) |
| tests/agent/test_history_injection_turn_bound.py | FIXED-CODE + FIXED-TEST | CODE: `turn_iteration_prep.prepare_iteration` called `_maybe_inject_run_budget_wrapup(agent, messages)` WITHOUT `current_turn_user_idx` — after the standalone steer user row is inserted, the unbounded scan stops at that user row and the run-budget notice is never delivered (fork passed the bound; #1496). TEST: steer delivery is the standalone user row (upstream #110979, already adopted by f32ba10a13 for the sibling test) — asserts read `_steer_after_tool`; placement lint also recognises `steer_user_row` inserts and the drain's new home `turn_iteration_prep._inject_steer_after_newest_tool_result`. | 23 passed (group) |
| tests/agent/test_idle_compaction_engine_duck_typing.py | FIXED-CODE | `turn_context_compaction._idle_compaction`: restored the fork's duck-typed `summary_target_ratio` read (`_config.target_ratio` → 0.20 default) — the merge took a bare attribute read, which raised AttributeError on LCMEngine-shaped compressors (fork/main `turn_context.py:1020`). | 23 passed (group) |
| tests/agent/test_inturn_stats_render_gate.py | FIXED-TEST | source-structure pin follows the announce block into upstream's extracted `_announce_committed_compaction`, where the token figures are the parameters `pre_request_est`/`compressed_est`; the pin now also asserts the announce call passes those exact names as `pre_tokens`/`post_tokens` (the identity the test exists for). | 23 passed (group) |

## Blast radius (this run's src edits)
- `agent/turn_iteration_prep.py` (run-budget bound), `agent/turn_context_compaction.py` (idle ratio):
  tests/agent/test_pre_api_steer_drain_turn_bound.py + test_run_budget.py + test_idle_compaction_lock_and_guards.py
  → 42 passed, 1 failed: `test_idle_compaction_bookkeeping_raise_keeps_turn_state` fails IDENTICALLY on the
  clean base (verified by `git checkout -- .` then re-run); it is in L1's manifest (round 5) → not chased here.

## Final full-manifest re-proof (head 03fbfa506a, 11 calls x <=3 files, scripts/test-gate, sandboxed HOME)
42 / 127+1 / 120 / 112+2sk / 37 / 309 / 57 / 362 / 17 / 38 / 23 passed. The single red across all 33 files is
`test_codex_subscription_proxy_context.py::test_cpa_codex_slug_uses_verified_window_under_large[gpt-6-sol-872000]`
(see its row: cleared by L1 d2acfa7f22 once folded).

## FOLLOWUP
- -> L1 (orchestrator fold): fold `d2acfa7f22` (872K gpt-6 Codex table) — it is not in the round-5 fold head
  93556253a7; it clears the one remaining red in this manifest. No edit made here.
