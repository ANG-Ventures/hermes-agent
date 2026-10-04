# Ledger: lane T-misc (parity 2026-10-01)

| path | hunks | kind | why | residual risk |
|---|---|---|---|---|
| tests/ci/test_live_comment.py | 1 | F | fork retired `.github/workflows/ci-review-comment.yml` (aff7aea248, #1089) and deleted the 2 tests that parse it (`test_workflow_watch_list_names_a_workflow_that_exists`, `test_poller_never_watches_its_own_workflow`); upstream only changed their `importorskip("yaml")` module name. Kept deleted. | FOLLOWUP (L12): if `.github/workflows/ci-review-comment.yml` (DU) is KEPT, restore the 2 tests from upstream stage; if deleted, nothing. |
| tests/conftest.py | 9 | B | Upstream split conftest into `tests/_fixtures/{env_filter,live_system_guard,platform_gating}.py` (07b39f1e8a) + added host-rendezvous/relay-plugins/pinned-home isolation, multiplex-latch reset (3c), `_reset_foreground_exit_fence`, keychain mirror stub, basetemp relocation, real-home tripwire, `real_bash`. Took upstream structure (imports replace the inline credential filter + `_live_system_guard`), re-threaded fork behavior that lives in conftest: 3-GUARD sandbox-under-live-root refusal (t_f64dfdb6), fork 3b comment, `_isolate_fallback_sticky_store`, `_isolate_session_contextvars`, hermetic Claude-Code creds FILE read + both-module patch loop (fork test `tests/agent/test_aux_cache_isolation.py` reads the adapter alias) with upstream mirror stub added inside the loop, RLIMIT_NOFILE raise + `allow_sys_modules_purge` marker in `pytest_configure` (upstream `_relocate_basetemp_outside_operator_home(config)` kept first), fork lazy moa-cache fixture + `_kanban_stubbed_liveness_implies_identity`. Fork log-handler strip step renumbered 3c->3d (comment only; upstream took 3c). Fork `_SAFE_TMP_ROOT` relocation kept beside upstream temp-root stripping (parallel invention; fork test `tests/test_tmp_root_hermeticity.py` imports `_is_under_real_hermes_root`/`_real_hermes_root`). Gate AC3 "lost" symbols are all bodies that moved to `tests/_fixtures/*`. | **FOLLOWUP (must-apply, outside lane): `tests/_fixtures/env_filter.py` needs fork deltas `"_KEY"` credential suffix + `HERMES_KANBAN_EXIT_FILE`, `KANBAN_CLAIMED_CARD_PIN`, `HERMES_KANBAN_SANDBOX` in `_HERMES_BEHAVIORAL_VARS`; `tests/_fixtures/live_system_guard.py` needs fork lazy `_initial_children` snapshot (t_24f73ced), `_remember_spawned_child` (Popen + asyncio exec/shell), AccessDenied fail-closed (C5 #71).** Ready patch (`git apply --check` clean on this tree): `/tmp/e45-T-misc/followup/T-misc-followup-fixtures.patch`, also in the appendix of this ledger. Until applied, local runs on a box with CLAUDE_API_PROXY_KEY / inside a kanban worker lose those scrubs (CI unaffected). NOT RUN: `hermes_constants.py` still conflicted (sibling lane) so conftest cannot import yet. Fork `pytest_plugins = ["tests.sys_modules_leak_gate"]` stays in tests/conftest.py although upstream's comment warns a non-root conftest carrying it fails a whole-directory collection; the fork runner passes file paths (unchanged fork behavior). |
| tests/computer_use/test_cua_no_overlay.py | 1 | UP | Upstream moved the embedded daemon to `tools/computer_use/cua_backend_daemon.py` (Thread patch target follows) and gated the test with `@pytest.mark.platforms("not macos")`. Fork's parity-2026-08-30 `sys.platform="linux"` pin solved the same macOS-branch problem by faking the host OS; upstream's marker supersedes it (parallel invention, upstream doctrine "don't fake the host OS"). | Test no longer runs on a Mac dev box (skipped by marker); Linux CI covers it. |
| tests/cron/test_codex_execution_paths.py | 1 | B | Upstream purged `test_gateway_run_agent_codex_path_handles_internal_401_refresh` (aedc6ccc3a low-value purge; fork never modified it, so the deletion stands) and repointed the OpenAI seam to `agent.process_bootstrap.OpenAI` (2a95791992 re-export drop). Fork-added `_RetryCaptureAgent`, `_patch_codex_runtime`, `test_cron_per_job_api_max_retries_override`, `test_cron_without_override_inherits_agent_default` kept (fork feature: cron per-job `api_max_retries`); the fork helper's `run_agent.OpenAI` patch repointed to upstream's seam. | Fork tests depend on `cron/scheduler` still applying per-job `api_max_retries` after the L-lane resolution. Not run (tree not importable yet). |
| tests/cron/test_cron_incidents.py | 1 | U | `fake_deliver` stub signature: fork added positional `success=True` (failed cron runs wear the failure header, t_6bedca00 #1336), upstream added `**kwargs`. Union signature accepts both call shapes. | Depends on which `_deliver_result` signature `cron/scheduler.py` (sibling lane, still conflicted) lands; the union stub tolerates either. Not run. |
| tests/cron/test_cron_script.py | 1 | U | Fork-added `test_script_child_is_not_an_agent_process` (cron script child drops AI_AGENT/agent marker, t_7fee0f83) + gh-shim lane tests (`test_shimmed_child_gets_job_lane...`, `test_no_shim_leaves_the_env_untouched`) kept; upstream's marker migration `windows_only` -> `platforms("windows")` taken on the following test. | Fork tests repointed from `cron.scheduler` to upstream's extracted `cron.scheduler_script` (where upstream's own tests in this file import `_run_job_script`; `cron.scheduler` no longer re-exports it). **FOLLOWUP (extends L10-gw-miscb's): when porting fork `_run_job_script` deltas from `:2:cron/scheduler.py` (~L5065-5090) into `cron/scheduler_script.py`, include (a) the agent-marker scrub (AI_AGENT + agent marker removed from the script child env, t_7fee0f83) and (b) the gh-shim lane env + the `CRON_SCRIPT_DEFAULT_GH_LANE` constant, defined IN `cron/scheduler_script.py` (these tests import it from there).** EXPECTED RED until ported. Not run. |
| tests/cron/test_parallel_pool.py | 1 | UP | Upstream deleted the sequential (workdir) pool (b7c59bda54 "isolate per-execution working directories": `_sequential_pool`/`_get_sequential_pool` gone, workdir jobs run in the parallel pool), re-keyed pools per home (cb647a018f: `_parallel_pools`/`_parallel_pool_max_workers` dicts), then purged the renamed workdir tests + `TestTickBatchAdvance` (657bc3682d, aedc6ccc3a). DROPPED fork-side copies: `test_sequential_job_does_not_block_ticker` (fork had only converted its stopwatch to an event, dacc7ba8d4), `test_sequential_running_guard_prevents_double_dispatch`, `test_get_sequential_pool_is_persistent` (subject `_sequential_pool` removed; replacement = per-home parallel pool, covered by `TestRunningJobGuard` + `TestSyncMode`), `test_tick_calls_advance_next_runs_once_with_all_due_ids` (base test, fork-unmodified, upstream purge). The fork tests also assign `sched._parallel_pool_max_workers = None`, which would clobber upstream's per-home dict. Fork's event-based witness in `test_sync_false_returns_immediately` (auto-merged) kept. | If the cron/scheduler lane KEEPS the fork's sequential pool, restore the 3 sequential tests from fork stage (HEAD:tests/cron/test_parallel_pool.py). Not run. |
| tests/cron/test_run_one_job.py | 1 | U | Same as test_cron_incidents: `fake_deliver` stub takes fork's positional `success=True` (t_6bedca00) and upstream's `**kwargs`. | Tolerates either `_deliver_result` signature from the scheduler lane. Not run. |
| tests/honcho_plugin/test_auth_recovery.py | 1 | B | Fork test `test_external_relogin_in_same_mtime_tick_clears_verdict` kept (fd63db0446: `reauth_required` memo keyed on content). Upstream purged `test_error_body_is_logged` (85cd82f1dd; fork never modified it, deletion stands, `logging` import already gone) and replaced `test_redaction_strips_token_values` with `test_honcho_token_prefixes_are_registered_with_the_shared_redactor` (redaction moved to `agent.redact`). | Fork test needs the content-keyed memo to survive in `plugins/memory/honcho/oauth.py` (UU, sibling lane; upstream's copy already hashes file bytes at L175). Not run. |
| tests/plugins/memory/test_memory_lazy_install.py | 1 | UP | Upstream replaced `tools.lazy_deps` (LAZY_DEPS allowlist, `ensure()`, sealed-venv durable target) with PM admission (3d12e86ef1, 657bc3682d): the file is now one `test_provider_sdk_admission` over `pm.client.sync_venv`. DROPPED the fork-side copies of the base tests `TestAllowlistEntries`, `TestSupermemoryEnsureCalled` (subject `tools.lazy_deps.LAZY_DEPS`/`ensure` is an 8-line stub upstream; replacement = `test_provider_sdk_admission`). Fork delta re-applied: `extra` parametrized over `supermemory` only, because the fork's self-contained mem0 has no `_create_backend` (same reason the fork removed `TestMem0EnsureCalled` on 2026-06-29). | NOTE for mem0 owner: merged `plugins/memory/mem0/plugin.yaml` now declares `extra: mem0` (upstream) instead of the fork's `pip_dependencies: mem0ai>=2.0.10,<3`, while fork `_get_client` still does a bare `from mem0 import MemoryClient` with no `pm.ensure_import`; verify the SDK still gets installed for cloud-mode mem0. Not run. |
| tests/providers/test_provider_registry.py | 1 | U | Imports: fork `from hermes_cli import provider_seam` (provider-registry generation seam fixture) + upstream's `from providers.base import ProviderProfile` (package re-export dropped upstream). | Fixture uses `provider_seam.current/_restore/_reset`; needs `hermes_cli/provider_seam.py` + `providers/__init__.py` (UU, sibling lane) to keep the seam. Not run. |
| tests/test_batch_runner_checkpoint.py | 1 | F | Fork-authored `test_run_batch_accepts_max_reasoning_effort` + `test_run_batch_accepts_ultra_reasoning_effort` kept; upstream side of the hunk was only a blank-line removal (purge commit 524c38a98a touched other tests, auto-merged). | Needs `batch_runner.main` to keep accepting `reasoning_effort="max"/"ultra"`. Not run. |
| tests/test_hermes_constants.py | 1 | B | Fork-added `TestReasoningLabel` (shared `reasoning_label` chokepoint for the runtime-footer reasoning field / compaction + route-change announces) kept. Upstream deleted `managed_node_tree_in_use()` and its `TestManagedNodeTreeInUse` class (body auto-merged away); the orphaned class header left in the fork side of the hunk is dropped. | `reasoning_label` is present in the working hermes_constants module (L1216) though that file is still conflicted in a sibling lane. Not run. |
| tests/test_managed_runtime_resolution.py | 1 | UP | `_ALLOWED` bare-`which` allowlist (a stale-entry test enforces exact match with live call sites). Upstream dropped `("tools/lazy_deps.py","uv")` (lazy_deps is a 26-line PM stub upstream) and added `("hermes_cli/source_build.py","node")`. Fork's extra `("tools/browser_use_cli.py","uv")` entry dropped: the merged (auto-merged, staged) `tools/browser_use_cli.py` has no `which(` call left, so the entry would be stale. | FOLLOWUP (orchestrator, after tree compiles): run this file. (1) If `tools/lazy_deps.py` (UU, sibling lane) keeps the fork's `shutil.which("uv")` fallback, re-add `("tools/lazy_deps.py","uv")` to `_ALLOWED`. (2) Upstream's NEW `test_bare_which_and_known_path_tables_are_allowlisted` checks every bare `shutil.which`/known-path table against `tests/fixtures/resolution_allowlist.json` (outside lane): fork-only modules with bare lookups will need rows there or a move to `hermes_platform` resolvers. |
| tests/tui_gateway/test_fast_session_scope.py | 1 | F | Fork helper `_fast_capability()` (stand-in for `hermes_cli.models.resolve_fast_mode_capability`, the fork's full-route /fast validation) kept; upstream side was a blank-line removal. Upstream's new `test_stale_session_id_is_refused_not_persisted_globally` + `TestSessionInfoFastFollowsTheRoute` auto-merged in alongside (fork feature: /fast). | Fork tests patch `resolve_fast_mode_capability`; upstream's new class asserts `server._session_info(...)["fast"]` route gating (fd602278c7). Both must hold in the merged `tui_gateway/server*` + `hermes_cli/models.py` (sibling lanes): if the fork's capability path replaces upstream's route check, `TestSessionInfoFastFollowsTheRoute` may go red. Not run. |
| tests/tui_gateway/test_kanban_notify_poller.py | 1 | B | Fork rewrote `test_done_reopen_notifies_once_per_event_until_archive` into `test_done_delivers_then_unsubscribes` (kanban notify policy: unsub after done delivery, t_6d6e9467 #1097) and deleted the retained-subscription block; upstream only repointed that block's `kb.connect()` to `kanban_db_connect` (e3ab65fe80 re-export drop). Fork deletion kept; the one fork-side `kb.connect()` left in the rewritten test repointed to `kbc.connect()` to match upstream's module split. Upstream purge of `test_blocked_includes_reason` / `test_completed_prefers_payload_summary` auto-merged (fork-unmodified). | `kb._append_event`, `kb.complete_task` still read through `kanban_db` (as upstream's own lines do). Needs fork unsub-after-done to survive in `tui_gateway` + `kanban_db*` (sibling lanes). Not run. |
| tests/tui_gateway/conftest.py | 1 (AA) | U | Both sides added the file. Fork: autouse `_restore_server_method_table` (repairs `tui_gateway.server._methods` after leaked stub routes). Upstream: eager `import hermes_bootstrap` so PM boot never runs inside a mocked `hermes_constants` window (925c08ceca). Union: one docstring carrying both rationales, both imports, fork fixture. | The fork fixture's rule "never import tui_gateway.server here" still holds; the bootstrap import is a different module. Not run. |
| tests/acp_adapter/test_acp_mcp_discovery.py | 2 | B | Fork converted `assert elapsed < 0.2` into event ordering witnesses (`entered` / `discover_returned`, wall-clock flake conversion); upstream made the discovery thread per-home (`mcp_startup._mcp_discovery_thread` is now a dict, accessor `_current_home_thread()`). Kept the fork witnesses and the 10s join, reading the thread through upstream's accessor. | none beyond `acp_adapter/mcp_startup` keeping `_current_home_thread` (upstream). Not run. |
| tests/acp_adapter/test_server.py | 2 | TEST | `test_compact_compresses_context`: upstream routes ACP `/compress` through the shared `agent/conversation_compression_manual.py::compress_now`, which calls `_compress_context(..., defer_context_engine_notification=True, task_id=...)`; the fork asserts `trigger_reason="manual_compress_command"` (compaction trigger attribution, cde99bfb06 #630; fork `conversation_compression` logs a missing reason as a WIRING DEFECT / UNATTRIBUTED). Stub accepts `trigger_reason` + `**kwargs` and the call assertion carries BOTH upstream kwargs and the fork `trigger_reason`. | **FOLLOWUP (outside lane, EXPECTED RED until done): `agent/conversation_compression_manual.py::compress_now` must pass `trigger_reason="manual_compress_command"` into `agent._compress_context(...)` (L126-128).** The merged `acp_adapter/server.py` no longer has the fork's inline call, so without this every manual compress that goes through `compress_now` is UNATTRIBUTED. Same chokepoint serves CLI/gateway/TUI once those lanes delegate to it. |
| tests/tui_gateway/test_slash_worker_mcp_discovery.py | 2 | UP | Parallel fix of the same shard flake: both sides pinned `mcp_discovery_timeout: 30`; the fork widened the single `/tools` read to 60s (`_SLASH_WORKER_RESPONSE_TIMEOUT_S`), upstream restructured into a warm-up `/version` request with a 60s budget followed by a 10s `/tools` read that also checks the response id. Converged on upstream; the fork-only constant had no consumer left, so the file is byte-identical to upstream. | none (same budget for the cold path, stricter warm path). Not run. |
| tests/cron/test_preflight_config.py | 2 | U | Two `fake_deliver` stubs: fork positional `success=True` (t_6bedca00) + upstream `**kwargs`, union signature (same as test_cron_incidents / test_run_one_job). | Tolerates either `_deliver_result` signature. Not run. |
| tests/cron/test_cleanup_timeout.py | 5 | B | Fork replaced the `elapsed < 0.5` stopwatches with event witnesses (`entered`/`returned`, `SCHEDULING_GUARD_SECONDS`) and gave each fire its own `HangingSessionDB`; upstream only loosened the stopwatch bounds, added `test_detached_worker_teardown_waits_for_future` (needs `Future`), moved the SessionDB seam to the pool (`acquire`) and keyed in-flight ids by `_inflight_key`. Result: fork witnesses, `import time` gone (no consumer), `Future` import, the guard test patches upstream's pool `acquire` seam with the fork's `side_effect=fake_dbs`, cleanup discards `sched._inflight_key(...)` after waiting out the hung DBs. | `side_effect=[db1, db2]` assumes one `acquire()` per `run_job` (upstream calls it once at scheduler L1843); a second call per run would raise StopIteration. Not run. |
| tests/cron/test_cron_no_agent.py | 3 | B | Upstream added routed-profile secret-scope tests (`_PRESENCE_PROBE`, `test_no_agent_script_gets_owning_profiles_declared_secret_never_launch_residue`, interpreter tests) and purged `test_timed_out_no_agent_script_delivery_is_not_mislabeled_as_provider_failure` + `test_agent_provider_timeout_delivery_keeps_fallback_guidance` (aedc6ccc3a). Fork had adapted both to upstream's scheduler (`_connect_telegram` helper: connected-platform preflight, `heartbeat_fire_claim` stub, `cron.suppress_transient_failure_page=False`) and they pin the failed-run delivery taxonomy the fork changed (t_6bedca00) — KEPT, re-inserted after upstream's new tests with the Popen/`_get_script_timeout`/`_terminate_cron_script_process` patches repointed to `cron.scheduler_script` (upstream extraction; patch where production reads). `import subprocess` restored (upstream dropped it with the tests). | Fork tests patch `scheduler.load_config` for `cron.suppress_transient_failure_page`; if the merged scheduler reads that toggle elsewhere the gate may re-suppress delivery. Not run. |
| tests/cron/test_script_claim_heartbeat.py | 4 | B | `test_repeated_heartbeat_errors_cancel_after_bounded_grace`: parallel fix of the same flake. Fork made it deterministic (fake `time.monotonic` sequence, `calls == 3` pins the grace predicate exactly, c7537c2cc9); upstream kept wall-clock with looser bounds + elapsed assertion (#111471). Fork version taken whole (the auto-merge had leaked upstream's docstring tail / `last_confirmed_at` monotonic read into the fork body, which would have consumed the fake clock), with upstream's kwarg rename `fire_claim_lost` -> `claim_lost` applied and the #111471 contract note folded into the docstring. Verified against upstream's `_heartbeat_loop` (scheduler.py L2659): pre-loop validation = call 1, loop reads 0.0/1.0/3.0 as the fork sequence assumes. | Patches `scheduler.time.monotonic`; if the scheduler lane moves `_heartbeat_loop` out of `cron/scheduler.py`, repoint. Not run. |
| tests/perf_guards/test_pattern_b_scaling.py | 3 | B | Upstream purged Guard 1 (`TestStreamedTextAccumulationLinear`, xfail strict=False, could never fail) and Guard 3 (`TestToolCallFragmentAssemblyLinear`) and renamed the helper to `_list_and_count_statements` with captured SQL in the failure message (85cd82f1dd/3f2c86d627). Guard 1 dropped (fork never modified it; subject was an xfail no-op; the N+1 guard `TestListSessionsRichQueryBound` is what both sides keep). Guard 3 KEPT: the fork rewrote it onto CPU time (`_min_cpu_time`, #734 deflake for quota-capped self-hosted runners) and it is the fork's regression guard for the SSE fragment accumulator shape. `_min_time` (wall clock) dropped with Guard 1 (no consumer left); `import time` restored for `_min_cpu_time`. Upstream's helper rename + message taken. | none. Not run. |
| tests/test_mcp_serve.py | 3 | F | Parallel fix of the same mtime-gate flake: fork factored `_bump_mtime(db_path)` (a helper, +1s ns bump), upstream inlined the same `os.utime(ns=mtime+1s)` at the three sites. Fork helper kept (identical semantics, one definition). | none. Not run. |
| tests/test_packaging_metadata.py | 3 | B | Upstream rewrote the file to three PM-era tests (test deps are group-only in manifest+lock; core/optional speech deps; starlette CVE floor via `packaging`) — replacements for the base tests `test_packaging_declared_as_core_dependency`, `test_faster_whisper_is_not_a_base_dependency`, the two starlette CVE tests. DROPPED (base tests, fork-unmodified, subject gone): `_PIN_RE`/`_lazy_deps_pinned_specs`/`test_pyproject_pins_are_internally_consistent`/`test_security_pins_present_in_mirrored_lazy_features`/`_lazy_deps_by_feature` (subject `tools.lazy_deps.LAZY_DEPS`, a stub upstream), `test_build_system_requires_exempt_from_exclude_newer`, `_UPDATE_DOWNGRADE_GUARD_FLOORS`. Fork-added `test_every_root_module_imported_by_shipped_code_is_in_py_modules` dropped: replacement = upstream `tests/test_packaging_py_modules.py::test_every_root_module_imported_by_packaged_code_is_shipped` (py-modules now derived in setup.py). Fork-added `test_every_on_disk_subpackage_is_covered_by_packages_find` (#34701) KEPT — upstream still hand-maintains `[tool.setuptools.packages.find] include` — with its `importorskip("setuptools")` guard. | Upstream's `uv.lock` tests read the merged lock (sibling lane). Not run. |
| tests/plugins/test_kanban_dashboard_plugin.py | 7 | B | Fork kanban-dashboard features kept: review exit guard (`..._refuses_shortcut`, `test_review_card_cannot_leave_review_via_any_direct_status`, `test_set_status_direct_owns_the_review_exit_guard`, t_164f0178), delete-running-task 409/force (2026-08-08), reassign/reclaim race reporting (`test_reassign_endpoint_reports_reclaim_that_landed_before_assign_refused` + bulk), `test_create_from_session_link_lands_in_viewer_home` (C7 k121). Upstream's new `test_touch_card_tap_opens_instead_of_dragging` (#115568) and `current_run_started_at` board test unioned in. Upstream's re-export drop (e3ab65fe80) repointed every `kb.connect()` to `kanban_db_connect.connect()`: the 14 remaining fork-side call sites repointed the same way (upstream's own lines in this file already use `kbc`). | Fork tests assert `plugins/kanban/dashboard/plugin_api._set_status_direct` guard + reclaim wording and the `in_viewer_home` facet — the plugin (L11-tools-plugins lane) must carry them. `kb.write_txn`/`kb.claim_task` etc. still read via `kanban_db` as upstream's lines do. Not run. |
| tests/test_pty_session.py (UD) -> tests/hermes_cli/test_pty_session.py | 1 | UP+port | Upstream MOVED the file (d10bb2ab6f, tests mirror the source tree) and extended it (FailingWS, replay-failure detach #110849, concurrent-reap tests). Fork delta ported onto the moved file: `wait_until(predicate)` helper replacing the four fixed `asyncio.sleep(0.05)` drain budgets (load makes the test slower, never red). Upstream's own new `asyncio.sleep(0.05)` in the superseded-socket test left as upstream wrote it. Fork's deflake of `test_reaper_loop_invokes_reap` (event witness) NOT ported: upstream purged that test as low-value (4b67380698); it was a base test the fork only deflaked, no fork-only coverage (`run_reaper` loop iteration is also exercised by `test_concurrent_reap_idle_is_idempotent`). `git rm tests/test_pty_session.py`. | If the reaper-loop test is wanted back, fork copy = `HEAD:tests/test_pty_session.py::test_reaper_loop_invokes_reap`. Not runnable yet (tests/hermes_cli/conftest imports `hermes_cli.kanban_db`, still conflicted in a sibling lane). |
| tests/acp/test_session.py (UD) -> tests/acp_adapter/test_session.py | 1 | UP | Upstream MOVED the file (d10bb2ab6f) and rewrote the touched tests (session cwd during init, toolset config surface, `message_uid` on restored rows). Fork delta (drop the `timestamp` assertion on `get_messages_as_conversation` rows + filter `timestamp`/`_db_persisted` out of the restored-history shape compare) NOT ported: its premise is the fork's `include_timestamp` gate (default off) on the SessionDB loader, and the staged merged `hermes_state_messages.py::_rows_to_conversation` restores `timestamp` on every projection unconditionally (no `include_timestamp` parameter exists in the merged SessionDB), so upstream's assertions match the staged source. `git rm tests/acp/test_session.py`. | **FOLLOWUP (outside lane, hermes_state / acp lanes — REAL DEFECT if left): the fork's `include_timestamp=True` kwarg is still passed by `acp_adapter/session.py:474`, `cli.py:13357`, `hermes_cli/cli_agent_setup_mixin.py:700`, `gateway/platforms/api_server.py:3156` (all staged) but the merged `hermes_state_messages.py::get_messages_as_conversation` has no such parameter -> TypeError; in `acp_adapter/session.py` it is swallowed by `except Exception` and the ACP session restores with EMPTY history. Either restore the fork's `include_timestamp` kwarg on the merged loader or drop it at those call sites (`agent/conversation_compression.py:1959` already signature-inspects). `test_get_session_restores_reasoning...` in this file would go red on that path, which is the right gate.** If the hermes_state lane re-gates timestamps off by default, port the fork relaxation from `HEAD:tests/acp/test_session.py` L238 + L323-337. |
| tests/ci/test_publish_e2e_evidence.py (UD) | 1 | UP | Upstream removed the whole publish-e2e-evidence pipeline (2ab7d9bfe3: `gh --attach` supersedes the trusted-publisher chain): `scripts/ci/publish_e2e_evidence.py` + `.github/workflows/publish-e2e-evidence.yml` are both staged D in the merged tree. The fork's additions (5xx-transient classification tests for `_mod._is_transient`, FleetReview F1) target that deleted module, so the subject is gone; no replacement exists (the capability itself was retired). `git rm`. | none; `git grep publish_e2e_evidence` only hits docs/census records. |
| tests/cron/test_cron_drift_alert_once.py (UD) | 1 | UP | Upstream replaced the fail-closed model-drift skip + alert-once bit with snapshot-as-pin invariants (bcff0a920e): `drift_alerted` / `mark_drift_alerted` / `[drift_skip]` have 0 refs in `MERGE_HEAD:cron/jobs.py` and `MERGE_HEAD:cron/scheduler.py`; replacement tests = `tests/cron/test_cron_provider_pin.py` (staged M, auto-merged). The fork's only delta in this file was the mechanical `fake_deliver(..., success=True, ...)` stub signature (t_6bedca00), no fork-only coverage. `git rm`. | `cron/scheduler.py` + `cron/jobs.py` are UU (sibling lane); fork HEAD still carries 13 drift-guard refs in scheduler.py. If that lane KEEPS the drift guard, restore this file from `HEAD:tests/cron/test_cron_drift_alert_once.py` (and keep `mark_drift_alerted` in jobs.py); the upstream intent is removal. |
| tests/cron/test_cronjob_schema.py (UD) | 1 | UP | Upstream purged it (aedc6ccc3a low-value purge, lane py05). Fork delta = a 5-line parity-2026-08-30 comment noting why `test_cronjob_schema_reasoning_effort_matches_generic_contract` was removed (the absence is pinned by `tests/cron/test_cron_reasoning_effort.py::test_schema_does_not_expose_reasoning_effort`, which survives). No fork-only test. `git rm`. | none. |
| tests/cron/test_scheduler_mcp_init.py (UD) | 1 | UP | Upstream purged it (aedc6ccc3a). Fork delta = a docstring underline (`====` -> `----`). No fork-only test. `git rm`. | none. |
| tests/install/install-update-e2e.sh (UD) | 1 | UP | Upstream retired the bubblewrap fake-Internet sandbox (ea4cd375f8): `scripts/sandbox/stage2-run.sh` + `proxy.py` are staged D, `install-e2e-run.yml` (staged M) now runs the new `tests/install/installer-script-e2e.sh` (staged A) on the bare runner; nothing in the merged tree references `install-update-e2e.sh` (only `windows-install-update-e2e.yml`, a different driver). Fork delta = sandbox-bound npm `_logs` capture + `diagnose_node_gyp` verbose re-run (reads `$SANDBOX_ROOT/home/.npm/_logs`, runs through `in_sandbox` with the sandbox node home) — not portable as-is to the bare-runner driver. `git rm`. | Optional follow-up (not required): the npm-debug-log capture idea could be re-done in `installer-script-e2e.sh` against the runner HOME if install failures there turn opaque again; fork copy = `HEAD:tests/install/install-update-e2e.sh` L115-154. |
| tests/plugins/memory/test_hindsight_provider.py (UD) | 1 | UP | Upstream dropped the bundled hindsight memory provider and its seven test files (090b06c345); `plugins/memory/hindsight/*` is staged D in the merged tree, so the subject is gone. Fork delta = CI pacing only (`_RETAIN_OP_POLL_INTERVAL_S = 0.001`, 60s prefetch join), no fork-only coverage. `git rm`. | none (a hindsight provider can only return as a standalone plugin repo per AGENTS.md policy). |
| tests/skills/test_sdlc_review_skill.py (UD) | 1 | F | Upstream purged the file as low-value (524c38a98a, lane py16). The fork rewrote `test_review_lenses_vary_per_round` into `test_review_runs_all_four_lenses_every_round`, pinning the fork's review-coverage contract (`hermes_cli.kanban_review_schema.REQUIRED_REVIEW_LENSES`, the `review_coverage:` JSON record that `kanban_db._validate_review_coverage` parses, `kanban_block(kind=capability)` escalation). Subject `skills/devops/sdlc-review/SKILL.md` exists unchanged in the merged tree and the fork feature is live on the fleet, so the fork file is KEPT (stage 2 verbatim). | RUN: 9 passed (`pytest tests/skills/test_sdlc_review_skill.py`). |
| tests/hermes_state/test_hermes_state.py (DU) -> tests/test_hermes_state_{core,maintenance,messages,search}.py | 52 upstream hunks | B (port) | Upstream MOVED `tests/test_hermes_state.py` to `tests/hermes_state/` (d10bb2ab6f) and changed it in range (+1562/-195); the fork SPLIT the same monolith into four files (737e435ff7, #697, per-file CI timeout) — same decision as the brief's `test_run_agent.py` rule: never restore the monolith, port upstream's in-range changes into the split files. Ported mechanically (pre-image match, `/tmp/e45-T-misc/route_hunks.py`) 41/52 hunks incl. the import/fixture header into all four (`_activity_snapshot` helper, `hermes_state_common` FTS constants); 11 remaining by hand: 52 new upstream tests all present (`TestAsyncDelegationsSchemaAgreement`, peer-fallback profile fencing #74285, negative `older_than_days` #116361, projected-tip title #106165, reset/branch markers, read-only WAL IOERR retry #100436, settled-open no-writes, include_ancestors suite, …), `get_session_activity` -> `_activity_snapshot` repoint, and the 14 upstream-purged base tests removed. Of those 14, two carried fork-side edits that were deflakes only (`test_search_projection_skips_context_enrichment_queries`: borrow the traced read conn through `_read_ctx`; `test_order_by_last_active_surfaces_recently_touched_older_session_first`) — dropped with upstream's purge (8f6109e493), no fork-only coverage. Fork-retained tests from EARLIER upstream purges (e.g. `TestTitleUniqueness.test_get_session_by_title`…, `TestResolveSessionByNameOrId.test_resolve_by_title_falls_back`, 335 split-only names total) kept untouched. Name parity: every upstream test name present exactly once across the four files; `ruff F401/F811/F821` clean (unused imports introduced by the shared header pruned). `git rm tests/hermes_state/test_hermes_state.py`; the four split files staged. | Not runnable yet (`tests/conftest.py` -> `hermes_constants`, sibling lane). Upstream's new tests target upstream SessionDB behaviour that the hermes_state lane must carry (`_READ_ONLY_IOERR_RETRY_BACKOFF_S`, `_connect_tracked_db`, `list_prune_candidates(older_than_days<0)` ValueError, `_reset_from`/`_branched_from` list facets, `build_activity_snapshot`). Fork copies of the two dropped deflakes: `HEAD:tests/test_hermes_state_maintenance.py` / `_search.py`. Upstream's `test_v23_rebuild_from_trigram_tool_calls_projection` etc. landed in `_search.py` beside the fork's FTS tests — re-balance the split if a file nears the 300s per-file cap. |
| tests/plugins/memory/test_mem0_backend.py (DU) | - | F (deleted) | Fork retired it with the mem0 restructure (brief known decision: `_backend.py` stays deleted; fork mem0 is self-contained, `_get_client` + `_DirectRestMem0Client`, no `Mem0Backend`). Upstream in-range delta = purge of 3 forwarding tests (`test_search_forwards_params`, `test_update_forwards`, `test_delete_forwards`), no fix to port. `git rm`. NO-PORT-NEEDED. | none. |
| tests/plugins/memory/test_mem0_setup.py (DU) | - | F (deleted) | Fork retired it (`_setup.py` deleted, no `parse_flags`/`build_oss_config`/`_check_ollama` in fork mem0; setup goes through the fork's `post_setup`). Upstream in-range delta = purge of `TestParseFlags`/`TestDryRun` + one new test `test_discovery_loaded_setup_module_exposes_post_setup` (#103078: discovery execs sibling modules before `__init__`). The fork package has no `_setup` sibling, so that import-order hazard has no subject. `git rm`. NO-PORT-NEEDED. | none. |
| tests/plugins/memory/test_mem0_v3.py (DU) | - | F (deleted) | Fork retired it (the fork's mem0 tests live in `plugins/memory/mem0/test_*.py` + `tests/plugins/memory/test_mem0_{selfhost,temporal,thread_lifecycle,...}.py`). Upstream in-range adds: (a) `TestSyncTurnTruncation` for `_SYNC_MSG_MAX_CHARS` / `_truncate_for_sync` / `sync_max_chars` config (#106235/#37421, small-context OSS embedders); (b) secret-scope fail-closed tests for `_load_config` (`test_platform_config_still_fails_closed_without_profile_scope`, `test_oss_mode_initializes_without_platform_key_in_scope`, `test_file_api_key_still_overrides_environment`). Evidence against porting: (a) fork `sync_turn` (L1855-1886) already caps the turn via `_CAPTURE_MAX_TURN_CHARS` (skips oversized user text, truncates assistant text to the remaining budget, user fact preserved) and has no OSS mode; (b) fork `_load_config` (L195-210) already reads `MEM0_API_KEY`/`MEM0_ADMIN_API_KEY` through `agent.secret_scope.get_secret` with `UnscopedSecretError` handling (adopted upstream's routing last sync, comment at L206). `git rm`. NO-PORT-NEEDED for the semantics; fork symbols differ so the tests cannot be lifted verbatim. | Optional FOLLOWUP for the mem0 owner: upstream's boundary-aware truncation (`_truncate_for_sync` keeps the last sentence boundary in the window, config `sync_max_chars` raises the cap) is a nicer cut than the fork's hard `[:budget]` slice; port = `MERGE_HEAD:plugins/memory/mem0/__init__.py::_truncate_for_sync` + tests from `MERGE_HEAD:tests/plugins/memory/test_mem0_v3.py::TestSyncTurnTruncation`, adapted to `_CAPTURE_MAX_TURN_CHARS`. |

## Appendix A — ready patch for `tests/_fixtures/*` (outside lane; see tests/conftest.py row)

`git apply --check` was clean on this tree at 2026-09-30 22:41 PT. Apply from the worktree root.

```diff
--- a/tests/_fixtures/env_filter.py	2026-09-30 22:41:30
+++ b/tests/_fixtures/env_filter.py	2026-09-30 22:41:37
@@ -13,6 +13,16 @@
 
 _CREDENTIAL_SUFFIXES = (
     "_API_KEY",
+    # Bare _KEY catches provider keys that don't use the _API_KEY suffix —
+    # notably CLAUDE_API_PROXY_KEY / CLAUDE_API_PROXY_F{N}_KEY / GATEWAY_PROXY_KEY,
+    # which hermes_cli.env_loader seeds into os.environ from the real ~/.hermes/.env
+    # at import time (before a test's HERMES_HOME redirect applies). Left unstripped,
+    # they register as `claude-api-proxy` providers and hijack resolve_provider("auto")
+    # auto-detection (it returns claude-api-proxy before reaching the Bedrock branch),
+    # breaking test_bedrock_integration's AWS auto-detect. The only non-credential
+    # _KEY var in practice is HERMES_SESSION_KEY, already stripped via
+    # _HERMES_BEHAVIORAL_VARS below.
+    "_KEY",
     "_TOKEN",
     "_SECRET",
     "_PASSWORD",
@@ -193,12 +203,21 @@
     "HERMES_KANBAN_TASK",
     "HERMES_KANBAN_WORKSPACE",
     "HERMES_KANBAN_RUN_ID",
+    "HERMES_KANBAN_EXIT_FILE",
     "HERMES_KANBAN_CLAIM_LOCK",
     "HERMES_KANBAN_DISPATCH_IN_GATEWAY",
+    # The dispatcher exports the claimed card's model pin into every worker
+    # (kanban_worker_route.CLAIMED_CARD_PIN_ENV). Left ambient it shadows the
+    # per-test fixture board's pin and flips the worker-route pin tests.
+    "KANBAN_CLAIMED_CARD_PIN",
     # Pytest is routinely launched from a delegated worker.  The worker
     # lineage marker must not make parent-state tests run as delegated
     # children; tests that exercise child behavior set it explicitly.
     "HERMES_DELEGATED_CHILD_CONTEXT",
+    # Opt-in flag that makes kanban ignore the path pins above. A developer
+    # shell exporting it would silently flip path-resolution tests; tests
+    # that exercise it set it explicitly.
+    "HERMES_KANBAN_SANDBOX",
     "HERMES_TENANT",
     # Honcho host selection changes which nested config block wins. A local
     # shell override leaked "myhost" into the full suite and flipped 20
--- a/tests/_fixtures/live_system_guard.py	2026-09-30 22:41:30
+++ b/tests/_fixtures/live_system_guard.py	2026-09-30 22:41:53
@@ -84,15 +84,63 @@
     # Capture the test process's existing children at fixture start —
     # any *new* children spawned by the test are also allowlisted via
     # the live psutil walk below. Static set keeps the fast path cheap.
+    #
+    # The snapshot is taken LAZILY, on the first guarded signal, and keeps only
+    # children created before this fixture started (t_24f73ced): psutil's
+    # children() walks every pid on the host (~45-110 ms per test on a
+    # 1.1k-process Mac), and almost no test signals anything. Each entry keeps
+    # its create_time, so a recycled pid never matches.
     try:
         import psutil as _psutil
-        _initial_children = {
-            c.pid for c in _psutil.Process(test_pid).children(recursive=True)
-        }
     except Exception:
         _psutil = None
-        _initial_children = set()
+    import time as _time
+    _fixture_started_at = _time.time()
+    _initial_children_memo = []
 
+    def _initial_children() -> dict:
+        if not _initial_children_memo:
+            snap = {}
+            if _psutil is not None:
+                try:
+                    for c in _psutil.Process(test_pid).children(recursive=True):
+                        try:
+                            created = c.create_time()
+                        except Exception:
+                            continue
+                        if created <= _fixture_started_at:
+                            snap[c.pid] = created
+                except Exception:
+                    snap = {}
+            _initial_children_memo.append(snap)
+        return _initial_children_memo[0]
+
+    def _is_initial_child(pid: int) -> bool:
+        created = _initial_children().get(pid)
+        if created is None:
+            return False
+        try:
+            return _psutil.Process(pid).create_time() == created
+        except _psutil.NoSuchProcess:
+            return True  # gone: the signal is a no-op
+        except _psutil.AccessDenied:
+            return False  # unverifiable identity: fail closed (C5 #71)
+        except Exception:
+            # Probe machinery broken (e.g. a test stubbed sys.modules["psutil"]),
+            # not evidence of a foreign process: trust the snapshot record.
+            return True
+    _spawned_children = {}
+
+    def _remember_spawned_child(pid: int) -> None:
+        """Remember a child even after it exits and is reparented under load."""
+        started_at = None
+        if _psutil is not None:
+            try:
+                started_at = _psutil.Process(pid).create_time()
+            except Exception:
+                pass
+        _spawned_children[pid] = started_at
+
     def _is_own_subtree(pid: int) -> bool:
         # PID 0 means "our own process group"; -1 means "every process we
         # can signal". Both are dangerous when paired with SIGTERM/SIGKILL,
@@ -102,15 +150,41 @@
             return True
         if pid < 0:
             return False
-        if pid == test_pid or pid in _initial_children:
+        if pid == test_pid or _is_initial_child(pid):
             return True
+        if pid in _spawned_children:
+            if _psutil is None:
+                return True
+            try:
+                walker = _psutil.Process(pid)
+            except _psutil.NoSuchProcess:
+                # The recorded child is gone, so the signal is a no-op.
+                return True
+            except _psutil.AccessDenied:
+                # Exists but unverifiable: fail closed (C5 #71).
+                return False
+            except Exception:
+                # Probe machinery broken (e.g. a test stubbed
+                # sys.modules["psutil"]), not evidence of a foreign process:
+                # the pid is on our spawn record, so trust it.
+                return True
+            started_at = _spawned_children[pid]
+            if started_at is not None:
+                if walker.create_time() == started_at:
+                    return True
+                # PID was recycled onto a process the test did not spawn.
+                return False
+            # Without a recorded identity, retain the parent-chain check below.
         if _psutil is None:
             return False
         try:
             walker = _psutil.Process(pid)
-        except Exception:
+        except _psutil.NoSuchProcess:
             # Stale PID — kill would be a no-op anyway, allow it.
             return True
+        except Exception:
+            # Exists but unverifiable (AccessDenied): fail closed (C5 #71).
+            return False
         try:
             for parent in walker.parents():
                 if parent.pid == test_pid:
@@ -377,6 +451,7 @@
             def __init__(self, cmd, *args, **kwargs):
                 _check_subprocess_cmd("Popen", cmd, kwargs)
                 super().__init__(cmd, *args, **kwargs)
+                _remember_spawned_child(self.pid)
 
         _GuardedPopen.__name__ = "Popen"
         _GuardedPopen.__qualname__ = "Popen"
@@ -449,11 +524,15 @@
             _check_subprocess_cmd(
                 "asyncio.create_subprocess_exec", [program, *args], kwargs
             )
-            return await real_async_exec(program, *args, **kwargs)
+            proc = await real_async_exec(program, *args, **kwargs)
+            _remember_spawned_child(proc.pid)
+            return proc
 
         async def _guarded_async_shell(cmd, *args, **kwargs):
             _check_subprocess_cmd("asyncio.create_subprocess_shell", cmd, kwargs)
-            return await real_async_shell(cmd, *args, **kwargs)
+            proc = await real_async_shell(cmd, *args, **kwargs)
+            _remember_spawned_child(proc.pid)
+            return proc
 
         monkeypatch.setattr(_asyncio, "create_subprocess_exec", _guarded_async_exec)
         monkeypatch.setattr(
```

## Ceiling stop (2026-09-30 ~23:20 PT) — 41/43 files staged, 2 LEFT with markers, unstaged

| path | hunks | state | notes for the next worker |
|---|---|---|---|
| tests/cron/test_scheduler.py | 26 (6,595 lines) | UNTOUCHED, markers intact, all 3 stages present | Stages pre-extracted at `/tmp/e45-T-misc/tests_cron_test_scheduler.py/s{1,2,3}`. Expect the same classes as the other cron tests here: `fake_deliver` union signature (`success=True, ..., **kwargs`), `_deliver_result`/`_run_job_script` seams moved to `cron/scheduler_delivery.py` / `cron/scheduler_script.py` (patch where production reads), `_running_job_ids` keyed by `sched._inflight_key(...)`, per-home `_parallel_pools`, upstream purges (aedc6ccc3a) vs fork deflakes/t_6bedca00 failure-header tests. Tools: `/tmp/e45-T-misc/hunks.py <f> [cap] [n]`, `/tmp/e45-T-misc/resolve.py <f> N=ours|theirs|file:...`, `/tmp/e45-T-misc/done.sh <f>`. |
| tests/tui_gateway/test_tui_gateway_server.py | 19 (24,608 lines) | UNTOUCHED, markers intact, all 3 stages present | Stages at `/tmp/e45-T-misc/tests_tui_gateway_test_tui_gateway_server.py/s{1,2,3}`. God-file; do not rush. Fork features likely in play: /fast capability (`resolve_fast_mode_capability`), /undo /redo, /branch, /merge, kanban notify unsub-after-done (t_6d6e9467), runtime footer fork fields, `_restore_server_method_table` conftest contract. Upstream: `kanban_db_connect`/`kanban_db_notify` re-export split (e3ab65fe80), per-home MCP discovery thread, `_session_info(...)["fast"]` route gating (fd602278c7). |
