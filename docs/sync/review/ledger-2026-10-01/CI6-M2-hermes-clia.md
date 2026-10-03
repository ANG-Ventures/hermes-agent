# CI round-6 lane M2-hermes-clia — ledger (card t_6064eae7)

Base: fold head 93556253a7 on `sync/upstream-2026-10-01`. Branch
`sync/upstream-2026-10-01-ci-M2-hermes-clia`. CI evidence: run 37038759229.
Every file proved green narrowly via `e45-pt-M2a.sh` (test-gate, sandboxed HOME,
<=3 files per call) on the lane head. Verdicts: FIXED-CODE (merge regression,
restored onto upstream's structure) / FIXED-TEST (test pinned a shape the fork
legitimately diverges from, or an upstream-only test adapted to the fork
contract) / FOLLOWUP -> lane.

| file | verdict | what |
|---|---|---|
| test_agent_host_shell_hook_coverage | FIXED-TEST | patch `discover_mcp_tools` at `tools.mcp_tool_discovery` (compat re-export went in a5bd246865b); map upstream's extracted `gateway/run_turn*.py` + `slash_commands_session.py` to the GATEWAY host |
| test_anon_auth_core | FIXED-TEST (upstream-only) | a successful `_swap_credential` returns the fork's `SwapOutcome.SWAPPED`, not bare True |
| test_auth_codex_self_heal | FIXED-TEST (upstream-only #73667 cases) | the fork's Codex owner store (#673) serves the singleton before the singleton read; exercise recovery through `_refresh_codex_auth_tokens` with the same CAS/workspace assertions |
| test_auth_kimi_oauth_provider | FIXED-TEST | drop the `load_pool->None` stub; merged `_add_kimi_oauth_credential` reads the re-seeded pool entry |
| test_auth_pool_operations | FIXED-TEST (upstream-only) | rows start unbenched; a non-429 failure is the owner's receipt fence (dead / codex_refresh_uncertain), not upstream's exhausted/dead split |
| test_auth_profile_fallback | FIXED-CODE | `agent/codex_owner._current` compares the hydrated `PooledCredential.source`, not the raw row column (rows without `source` load as SOURCE_MANUAL on both sides); `auth_refresh_command` maps the owner store's AuthError to the "Could not renew" exit |
| test_backup | FIXED-CODE + FIXED-TEST | `hermes_cli/backup.py`: restore the fork's `.sparseimage/.sparsebundle/.dmg` staging exclusion and `cache/claude-usage` keep-dir onto upstream's suffix tuple; root `cache/*.MOVED.txt` breadcrumb falls under upstream's regenerable-cache rule |
| test_backup_excludes_disk_images | FIXED-CODE | same backup.py restoration |
| test_cli_async_delegation_delivery | FIXED-TEST | completions arrive wrapped in `ProcessNotificationBatch`; unwrap before asserting |
| test_cli_force_redraw | FIXED-TEST (upstream-only) | patch the `shutil` binding where `_install_resize_recovery` now lives (cli facade + mixin) |
| test_cli_goal_parked_resume | FIXED-CODE | `cli_agent_setup_mixin._resolve_fallback_runtime`: read requested provider/model via getattr; a bare mixin no longer raises inside the per-rung try and resolves every fallback to None |
| test_cli_provider_resolution | FIXED-CODE | same `_resolve_fallback_runtime` fix |
| test_codex_account_id | FIXED-CODE | Codex account identity single-sourced in `hermes_cli.auth.get_codex_account_id`; `codex_headers`, `credential_pool`, `auth_oauth_grants` read through it; `_extract_chatgpt_account_id` wrappers in `agent/model_metadata` + `hermes_cli/codex_models` restored as delegates |
| test_commands | FIXED-CODE | `commands_platforms._SLACK_VIA_HERMES_ONLY`: re-add the fork-only boomerang/merge/redo/resume-handoff demotions so the 50-slash clamp keeps help/restart; `auth_codex._codex_http_client` reads httpx through the module so a whole-module swap still intercepts |
| test_config_check_diagnostics | FIXED-TEST (upstream-only) | `messaging` is a real fork toolset; the built-in case uses a name neither side defines |
| test_copilot_auth | FIXED-CODE + FIXED-TEST | `tools/skills_hub_github.GitHubAuth._try_gh_cli`: restore the fork's gh-shim guard upstream's hub split dropped; test imports `skills_hub_github` (GitHubAuth's new home) |
| test_cron_status_next_run | FIXED-TEST (upstream-only) | stub the fork's vanished-job guard; assert next-run ordering only |
| test_desktop_slash_registry | FIXED-CODE | `apps/desktop/src/lib/desktop-slash-registry.json` regenerated via the fork's script (boomerang / merge / redo / resume-handoff) |
| test_finite_chat_delegation | FIXED-TEST (upstream-only) | background batches dispatch through `tools.async_delegation.dispatch_async_delegation_batch` (ledger R07 policy) |
| test_hooks_cli | FIXED-TEST (upstream-only in a fork file) | a MISSING hook is an inventory row (`agent/shell_hooks_missing`), not a "failed closed" verdict |
| test_inventory | FIXED-TEST | upstream's strict MoA opt-in (#63353, raw-config preset): the two fork tests expecting the virtual row seed an enabled preset |
| test_kanban_block_kinds | FIXED-TEST | build the blocks edge before the child runs; a parent-less dependency block is needs_input (upstream 42a778ab4b) |
| test_kanban_blocked_sticky | FIXED-TEST (upstream-only in a fork file) | fork #803 sticky hold backed by a terminal blocks parent; sticky-forever asserted alongside |
| test_kanban_c7_slice_a | FIXED-TEST | `validate_requested_model` moved to `hermes_cli.models`; repoint the patch |
| test_kanban_cli | FIXED-TEST (upstream-only case) | fork's `edit --priority` records `priority_set {old,new,actor}` (pinned by test_kanban_priority_verb), not upstream's bare `reprioritized` |
| test_kanban_cohort_death | FIXED-TEST | the single-query SIGTERM handler moved to `hermes_cli/cli_single_query.py` and exits via `_kill_foreground_and_exit`; receipt/last-words ordering asserted against that (behaviour intact) |
| test_kanban_complete_live_claim_guard | FIXED-CODE + FIXED-TEST (upstream-only) | `_request_review_txn` keys the live-claim refusal on `_claim_is_live` (pid + start fingerprint), the same fence as `complete_task` (#111764), instead of `claim_lock` alone; fixture disarms the fork receipt gate (t_e21aa11c), not the subject |
| test_kanban_create_body_file | FIXED-TEST (upstream-only) | the fork prepends an `origin:` birth line (`stamp_origin_body`) and trims outer newlines; assert the --body-file bytes after the stamp |
| test_kanban_dispatch_claim_allowlist | FIXED-CODE (upstream-only test) | `dispatch_once` resolves `kanban.default_assignee` through `_profile_exists_fn()` (dispatch_profiles-gated, #110995) instead of raw `profile_exists`, so a default this home may not claim is never written onto a shared-board card |
| test_kanban_empty_completion | green on base | no change needed (passed in the batch) |
| test_kanban_goal_judge_affinity | FIXED-CODE + FIXED-TEST (upstream-only) | `_goal_mode_handoff_rejection` binds the affinity scope to `task_id or task.id` (was `kanban:None` for task-only callers); test adapted to the fork's reason/None return via `goals.kanban_handoff_rejection` |
| test_kanban_home_index | FIXED-TEST | `write_txn` moved to `kanban_db_connect` and calls the mirror as `_kb._sync_home_index` (Attribute); AST check accepts both forms |
| test_kanban_host_cap | FIXED-CODE (upstream-only test) | review-lane reservation decided by the module-level `_any_spawnable_review` (respawn guard + per-profile cap aware) with the per-profile cap hoisted before it; a review row the lane loop would refuse no longer pins `ready_budget` to 0 |
| test_kanban_lane_review_regressions | FIXED-CODE | `kanban_db` facade re-exports `dispatch_once` / `detect_crashed_workers` / `enforce_max_runtime` / `has_spawnable_ready` / `check_respawn_guard` from `kanban_db_dispatch` |
| test_kanban_notify | FIXED-CODE | notifier uploads handoff artifacts on `review_requested` as well as `completed` (upstream f3357b5031's notifier half; the merge kept the fork's completed-only gate in `gateway/kanban_watchers.py`) |
| test_kanban_operator_reclaim_dead_claimer | FIXED-CODE | `main._set_process_title` reads `HERMES_KANBAN_TASK` (what the dispatcher exports); the merge carried `HERMES_KANBAN_TASK_ID`, set nowhere |
| test_kanban_parent_reopen_invalidation | FIXED-CODE | `_PendingTermination` carries `worker_started_at` inside the tuple (upstream's 3-tuple contract) while keeping the fork's `owner_window`; drain sites index [0]/[1] (kanban_db + dashboard plugin_api) |
| test_kanban_pr_gate | FIXED-TEST | `test_pr_already_resolved_for_this_card_is_not_resolved_again`: a parent-less dependency block re-kinds to needs_input and counts as a same-cause re-block -> triage under BLOCK_RECURRENCE_LIMIT=2; use kind=capability so the spent-PR subject is what is tested |

## FOLLOWUP -> other lanes (observed while proving blast radius; not edited here)

- FOLLOWUP -> M2-hermes-clib: `test_kanban_route_race_regressions::test_lane_route_banned_after_it_was_set_is_refused_on_the_review_path` patches `kb.review_dispatch_enabled`; the facade lacks it (lives in `kanban_db_dispatch`). Same re-export shape as batch 12.
- FOLLOWUP -> M2-hermes-clib: `test_kanban_review_lifecycle` (2 reds) + `test_kanban_survivor_binding` (3 reds) fail identically with and without this lane's `kanban_db` change.
- Pre-existing, order-dependent (identical on base 93556253a7): `test_kanban_per_profile_cap.py` followed by `test_kanban_review_load_gates.py` in one process -> 2 reds (`hermes_cli.profiles` attribute pollution). Each file is green alone.
- Pre-existing, order-dependent (identical on base 93556253a7, and PASSED in CI run 37038759229 slice 15/16): `test_backup.py::TestImportHonorsHermesHomeOverride::test_import_targets_named_profile_home` trips `tests/home_io_guard` (`run_import`'s `_get_platform_default_hermes_home()` marker probe) only when run in the 3-file batch with `test_backup_excludes_disk_images.py` + `test_cli_async_delegation_delivery.py`; `test_backup.py` alone -> 118 passed. Not merge damage from this lane.

## Final re-proof on head (3 files per call, e45-pt-M2a.sh)

52 / 55 / 127 (+1 pre-existing order-dependent red above) / 53 / 78 / 31 / 36 / 26 / 26 / 16 / 36 / 62 / 105 passed; 0 reds attributable to the lane.

## Operational note

Lane worktrees share `.git` with every other lane: `git stash` is ONE stack. Two accidental stashes this run swapped edits with lane M3-agentb (its stash content was already in its HEAD f478db127b; restored and verified with `git apply --check -R`). Baseline comparisons were done with `git diff > patch; git checkout -- .; ...; git apply`.
