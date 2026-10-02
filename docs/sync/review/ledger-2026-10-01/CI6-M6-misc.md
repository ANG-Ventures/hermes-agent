# CI round-6 lane M6-misc — ledger (card t_0c0acf57)

Base: fold head 93556253a7 on `sync/upstream-2026-10-01`. Branch `sync/upstream-2026-10-01-ci-M6-misc`.
Evidence: CI run 37038759229 (PR #1624). Narrow proofs via `~/.hermes/scripts/test-gate`, sandboxed HOME, <=3 files per call.
Classification per upstream-parity-merge Phase 2: fork/main baseline worktree = `$R/base`; upstream = 612d8e44a2.

Legend: FIXED-CODE (src fix, fork behaviour restored onto upstream structure) / FIXED-TEST (test or harness stale against a contract the merge legitimately adopted) / INHERITED (fails on fork/main too) / FOLLOWUP->lane.

## tests/cli
- tests/cli/test_fast_mode_overrides.py — FIXED-CODE (attempt 1, 161dc6b373: service_tier loader reads the runtime config). 84 passed with the two below.
- tests/cli/test_kanban_exit_receipt.py — FIXED-TEST (attempt 1: slash doubles carry the merged kwargs).
- tests/cli/test_kanban_signal_blackbox_turn.py — FIXED-TEST (attempt 1).
- tests/cli/test_service_tier_parse.py — FIXED-CODE (attempt 1, 161dc6b373).
- tests/cli/test_undo_redo_half_turn.py — FIXED-TEST (attempt 1: doubles match the merged undo/redo seam). 73 passed, 12 skipped with the chaos file.

## tests/e2e
- tests/e2e/core/chaos/test_gateway_turn_liveness.py — green on base at attempt 2 (skips on this host: tmux-gated); no change.
- tests/e2e/core/delivery/test_cron_virtual_clock_soak.py — green on base at attempt 2; no change.
- tests/e2e/core/history/test_transcript_ledger.py — FIXED-CODE + FIXED-TEST. `[stream_faults]` billed 6 api_calls / +100 input for a provider that billed 4. (a) CODE: upstream's streaming-5xx unmask probe (1965ea81ce) obtains its response through `interruptible_api_call` (which records it) and hands the SAME object back as the streaming result, where `_StreamingCall.run` records it AGAIN; the fork's `_note_billed_response` bills every registration, so the probe call was double-counted. `_record_successful_api_call` is now idempotent per response object (`_api_call_success_recorded` stamp). (b) TEST: the remaining +1 call is the fork's documented contract (agent/turn_usage.py "count every completed provider attempt, including providers that omit usage"): a `DropMidStream` 200 is one api call with UNKNOWN usage, not a non-call; `billed_usage()` in `tests/e2e/core/history/_helpers.py` now counts such a served-then-dropped stream as a call with no tokens. 9 scenarios green.
- tests/e2e/core/kanban/test_kanban_decompose_billing.py — FIXED-TEST (harness). `_helpers.Board.env()` strips every `HERMES_*` var, so the suite conftest's opt-ins never reach the child CLI/gateway: the fork's `kanban create` unhomed-card refusal (t_09fea045) rc=2'd every create, and the fork's shadow-cwd gateway boot guard (t_4853212d) refused `gateway run` from a `/Volumes/fleet-scratch` checkout. Added `HERMES_KANBAN_ALLOW_UNHOMED_CREATE=1` + `HERMES_ALLOW_SHADOW_CWD=1` to the harness env (same shape as 117c1c249d's upgrade e2e `_scenario.py`). Sibling files in the dir (dispatcher_restart, rate_limit_review, worker_contract) share the harness.
- tests/e2e/core/kanban/test_kanban_dispatcher_restart.py — skipped on this host (9 skipped); shares the harness fix above.
- tests/e2e/core/kanban/test_kanban_rate_limit_review.py — skipped on this host; shares the harness fix above.
- tests/e2e/core/kanban/test_kanban_worker_contract.py — skipped on this host; shares the harness fix above.
- tests/e2e/core/providers/test_openai_codex_pool.py — skipped on this host (10 skipped with the two git files); no change.
- tests/e2e/core/upgrade/git/test_partial_clone.py — skipped on this host; no change.
- tests/e2e/core/upgrade/git/test_shallow_install.py — skipped on this host; no change.
- tests/e2e/core/upgrade/handoff/test_update_network_failure.py — green at attempt 2 (fixtures from 117c1c249d); no change.
- tests/e2e/core/upgrade/test_config_roundtrip_properties.py — FIXED-TEST. `test_p1_explicit_null_leaves_survive_a_noop_save[102]`: the generator draws leaves from the fork's DOCUMENTED/DEFAULT_CONFIG key set (differs from upstream's), so seed 102's random walk produced no null leaf and the test's own precondition assert fired. `gen_case(require_null=True)` plants one explicit null leaf when the draw has none; the roundtrip property is unchanged.
- tests/e2e/test_413_incident_chain.py — FIXED-CODE + FIXED-TEST. (a) CODE: `run_agent.jittered_backoff` re-export dropped by the merge (fork/main:run_agent.py:168 `# noqa: F401` seam the test monkeypatches) — restored. (b) TEST: `test_malformed_200_stream_retries_then_fails_over`'s `_FakeStream` yields zero events; upstream c1281fff2a (`fix(anthropic): require message stop for streams`, upstream-only, adopted) now treats an eventless native stream as a drop (`EmptyStreamError`), so the successful fallback stream emits a `message_stop` event — same adaptation upstream made to tests/agent/test_streaming.py in that commit.
- tests/e2e/test_fallback_compaction_prompt_identity_e2e.py — FIXED-TEST (harness). `[head]` arm: turn-3 system prompt gained a `# Project Context` SOUL.md block the turn-1 prompt lacked. Root cause: the suite conftest pins `hermes_state.DEFAULT_DB_PATH` to its own sandbox (`<tmp>/hermes_test`), so the agent's db-derived home (`_agent_home`) was a second, never-ensured directory; upstream's `skills.auto_load` prompt block (`build_auto_load_prompt` -> `load_config_readonly` -> `ensure_hermes_home`) ensures that home mid-build, seeding its SOUL.md between the identity read (None) and the context-files read (seeded) — a harness artifact, not the #1077 property. The test now pins `DEFAULT_DB_PATH` to its own `HERMES_HOME`. Audit-hooked the write to prove it (`ensure_hermes_home` from `agent/skill_commands.py:723`). `pre_1077` negative control still fails as designed. 2 passed.
- tests/e2e/test_interrupt_close_owns_transcript_e2e.py — FIXED-TEST (harness). The `OWNS_TRANSCRIPT_E2E_SABOTAGE` negative control patched `agent.conversation_loop.provider_owns_transcript`; upstream extracted the request builder (the one reader) into `agent.turn_context`, so the sabotage was inert and the negative control passed for the wrong reason. Repointed `tests/e2e/_owns_transcript_driver.py` to the module that reads it.
- tests/e2e/test_relay_native_openai_stream.py — FIXED-TEST. The test's `_count_chunk` wrapper took `(self, diag, chunk)`; fork #1617 (b26623eca8, on fork/main) added `heartbeat=` for content-free keepalive chunks. Wrapper now forwards `**kwargs`. (Upstream-only test file; the merge's src is the fork contract.)
- tests/e2e/test_wake_prefix_byte_stability.py — FIXED-TEST. Upstream bf1bf7515a (`fix: retry Kanban wakes until adapter admission`, adopted): `deliver_wake` requires `event._gateway_accepted is True`; the test's `_PushAdapter` double bypasses `BasePlatformAdapter.handle_message` (which stamps it) and calls the runner directly. Double now stamps acceptance, like tests/gateway/test_kanban_wake_scope.py's double. 24 passed.

## tests/hermes_state
- tests/hermes_state/test_archive_retires_routing.py — FIXED-CODE (attempt 1, 39226a2bca).
- tests/hermes_state/test_corrupt_row_robustness.py — green with the above; no change at attempt 2.
- tests/hermes_state/test_production_root_anchor.py — FIXED-TEST (attempt 1, 3d78ccfe62: home-io opt-out).
- tests/hermes_state/test_routing_write_isolation.py — FIXED-TEST (attempt 1, 3d78ccfe62: `allow_real_home_io` opt-out, the CI red: "TEST BUG: file I/O against the REAL hermes home"). NOTE: `test_session_store_with_tmp_scope_cannot_bind_production_db` is red on THIS host under the lane runner only because the runner sandboxes `HOME` to a tmp dir, so `REAL_ROOT` (= `~/.hermes` via expanduser) is itself the sandbox and no production path exists to guard — it fails identically on the fork/main baseline worktree under the same runner (INHERITED-by-runner), and passes 4/4 with the real `HOME` (`env -i ... HOME=$HOME HERMES_HOME=<tmp>` through test-gate), which is the CI shape.
- tests/hermes_state/test_session_list_denorm_acceptance.py — FIXED-CODE (attempt 1, 39226a2bca).

## tests/plugins
- tests/plugins/memory/test_mem0_prefetch_floor.py — FIXED-CODE (attempt 1, ed235fde5f).
- tests/plugins/memory/test_mem0_selfhost.py — FIXED-CODE (attempt 1, ed235fde5f: shared daemon pool).
- tests/plugins/memory/test_multiplex_memory_identity_scope.py — FIXED-TEST (attempt 1, ed235fde5f: lineage).
- tests/plugins/memory/test_provider_threads_inherit_profile.py — FIXED-TEST (attempt 1, ed235fde5f).
- tests/plugins/test_chronos_cron.py — FIXED-TEST (attempt 1, uncommitted until now): fixtures used a hard-coded 2026-06-18 `next_run_at`, now in the past, so the merged scheduler (which skips past-due jobs on provision) never registered them; use `_future()`.

## tests (root)
- tests/test_hermes_state_messages.py — FIXED-TEST (attempt 1, ed235fde5f).
- tests/test_hermes_state_search.py — FIXED-TEST (attempt 1, 3d78ccfe62).
- tests/test_live_system_guard_self_test.py — FIXED-TEST (attempt 1, uncommitted until now): `tests/_fixtures/live_system_guard.py` carries the fork hardening (#546 / t_24f73ced) the merge dropped — lazy child snapshot keyed by create_time, spawned-child record incl. `asyncio.create_subprocess_*`, AccessDenied fails closed.
- tests/test_log_isolation.py — FIXED-TEST (attempt 1, 3d78ccfe62).
- tests/test_managed_runtime_resolution.py — FIXED-TEST (attempt 1, uncommitted until now): `tests/fixtures/resolution_allowlist.json` gains the four bare-`which` sites the merge brought in (gateway/run.py systemd shortcut, tools/vision_tools.py, tools/web_pdf_local.py x2); the allowlist is fork-owned so the merge pulled no rows for them.
- tests/test_model_switch_skew_guard_toggle.py — FIXED-CODE (attempt 1, uncommitted until now): `gateway/slash_commands_model.py` honours `model.stale_code_switch_guard: false` (the fork toggle) with the WARNING breadcrumb, read through the `gateway.slash_commands` facade the test patches.
- tests/test_p2_backfill_remainder.py — FIXED-CODE + FIXED-TEST (attempt 1, uncommitted until now): `plugins/platforms/whatsapp/adapter.py` npm-install failure message carries the shlex-quoted manual remedy (fork P2 backfill); the test stubs `find_node_executable` because upstream's `connect()` admits node through PM before the preflight under test.
- tests/test_p2_backfill_small.py — FIXED-CODE (attempt 1, uncommitted until now): `tools/self_repo_guard._scratch_dir_hint()` seam restored so the clone-destination quoting test can monkeypatch it.
- tests/test_provider_registry_seam.py — FIXED-CODE (two seams). (a) `providers.list_providers()` took upstream's module-global `_PROVIDER_LIST_CACHE` memo; the fork memoises PER GENERATION on the seam snapshot (`Generation.providers_list`, registry-pins v0.2 Phase 1, 860e58f818) so a publish can never serve a stale list and no seam lock is taken on the read path — `test_providers_list_memo_is_per_generation_and_lock_free` red (`fresh.providers_list` never filled; a registration after the memo was invisible). Restored the per-generation memo; `_PROVIDER_LIST_CACHE` stays as the legacy reset knob tests/plugin_dev clear. (b) `test_cold_reader_takes_the_lock_exactly_once_and_sees_one_generation` timed out because `hermes_cli/models_catalog_static.py` ran `sync_plugin_provider_catalog()` AT IMPORT (upstream a25cf4d77d), which calls `list_providers()` -> plugin discovery while `hermes_cli.models` is mid-import — the fork made this lazy in e8249c38cb / #816 (canary tests/providers/test_fork_canary_plugin_discovery_not_at_import.py). Lane M5 already carries the exact fix (lane-M5 eadc1421b8, `hermes_cli/models_catalog_static.py`, +10/-5); applied the same hunk here so this lane's file is green standalone. FOLLOWUP->orchestrator: identical hunk in M5 and M6 — fold either, the other is a no-op.
- tests/test_state_db_recover_hint_pasteable.py — FIXED-TEST (attempt 1, 3d78ccfe62).

## Cross-lane notes
- `agent/chat_completion_helpers.py` `_record_successful_api_call` idempotency stamp: minimal, additive; lanes L1/M3 own that file's other hunks — no overlap with their diffs at the fold head (checked `git diff 93556253a7..<lane>` for the function).
- `run_agent.py` import block: one added line; M3-agent* lanes touch run_agent.py elsewhere.
- `providers/__init__.py` `list_providers`: M5 does not touch it; L5 touched `hermes_cli/models_catalog_static.py` only via the same hunk as M5 (+8/-2 on lane-L5, superseded by M5's +10/-5 which this lane copies).
