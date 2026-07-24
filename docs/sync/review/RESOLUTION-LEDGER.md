# Parity Merge Resolution Ledger — 2026-07-23

Frozen upstream target: `a7a696ba5`. Fork base: `37fa0a353`. 125 conflict files / 912 hunks.
Branch: `sync/upstream-2026-07-23`. Resolver: subagent (Hermes). Merge NOT committed; left for orchestrator (Apollo).

Legend: **U**=union merge, **B**=both-sides reconciled, **F**=kept fork, **UP**=took upstream, **TEST**=stale-test updated to merged contract.

---

## Per-file resolutions

### AA — fork features upstream ingested (reconciled both evolutions)

- `tui_gateway/synthetic_turn.py` — **F**. Only delta: fork re-samples `tick = time.monotonic()` AFTER the CPU burn for delta cadence; upstream reuses stale `now`. Fork correct (cadence must reflect real elapsed wall-time incl. burn). Kept fork.
- `tui_gateway/host_supervisor.py` — **UP**. Upstream adds `"session.save": "run-concurrent"` route; fork lacked it. Superset.
- `tui_gateway/compute_host.py` — **B**. Based on upstream (adds session.save + session.compress control routes); re-applied fork `source=frame.get("source")` (client-source attribution — merged server.py `_make_agent` keeps BOTH `source` and `platform_override`). Upstream had renamed it `platform_override=`.
- `scripts/iso-certify.py` — **B**. Merged: fork probe-adequacy INCONCLUSIVE leg (probe failure → INCONCLUSIVE not FAIL) + upstream `message.start` race-guard (`started` flag). Independent regions.
- `tests/tui_gateway/test_iso_certify_seam.py` — **UP/TEST**. Timing bounds 1.5→5.0s (CI-robust).
- `tests/tui_gateway/test_compute_host_phase1.py` — **UP**. Upstream superset (parent_guard + session.compress + session.save assertions).
- `tests/hermes_cli/test_kanban_worktree_isolation.py` — **F**. Fork superset (Greptile #276 stale-branch tests).
- `tests/test_web_server_sessiondb_eventloop.py` — **DEFERRED** (resolve with web_server.py).

### Locales (union merge)

All 16 `locales/*.yaml` — **UP**. Sole conflict is the `reasoning` settings block. Upstream side is a strict superset of the fork's: fork added `max`; upstream added `max, ultra` + new picker keys (`picker_title`, `choice_*`). All keys upstream-owned; NO fork-only keys (no gateway.branch/merge in conflict regions — verified). Took upstream for all 16. YAML re-validated valid, zero residual markers.

### DU / arch-split

- `plugins/memory/mem0/_backend.py`, `_oss_providers.py`, `_setup.py` — **F (git rm)**. Per prior recorded decision (`docs/sync/review/mem0-resolution-decision.md`): fork ships a self-contained 1653-line `__init__.py` (`_DirectRestMem0Client`, hybrid retrieval, dedup, rerank, self-host) that supersedes upstream's thin-delegation `_backend`/`OSSBackend` architecture. Merged `__init__.py` imports NONE of the deleted helpers (verified); nothing else in tree imports them. Removed.
- `tests/plugins/memory/test_mem0_backend.py`, `test_mem0_setup.py` — **F (git rm)**. Import `Mem0Backend`/`PlatformBackend`/`OSSBackend` symbols the fork `__init__` doesn't export → would import-fail. Removed per same decision; fork mem0 coverage lives in retained `test_mem0_remember.py`/`test_mem0_selfhost.py`.
- `tests/run_agent/test_run_agent.py` — **F (git rm) + MIGRATE**. Fork split the monolith into per-issue files (legit god-file refactor). Verified upstream's new behaviors are covered in the split: redirect→`test_steer.py` (34 cases), engine_preflight→`test_engine_preflight_wire.py`, anthropic-interrupt→`test_cascading_interrupt_6600.py`, list-content-flatten→`test_66267_multimodal_interim.py`. Only the 2 `*_tool_tail*` truncated-tool cases had no equivalent → **migrated into `test_run_agent_conversation.py::TestRunConversation`**. ⚠️ `TestCredentialPoolRecovery` in fork's `test_run_agent_providers.py` still uses the OLD mock contract (no `api_key_hint`) but merged prod uses new `try_refresh_matching(api_key_hint=)` — flagged as expected post-merge STALE-TEST for bisect phase.

### Semantic UU + DU

**Resolved so far (batch 1 — union / both-sides reconciled):**
- `agent/context_engine.py` — **U**. Kept fork `import logging`+`logger`; added upstream `Optional`, `redact_sensitive_text` import + `sanitize_memory_context`/`automatic_compaction_status_message` fns.
- `acp_adapter/session.py` — **B**. Call `get_messages_as_conversation(session_id, include_timestamp=True, repair_alternation=True)` — both fork (LCM ts) + upstream (live-replay repair) kwargs. ⚠️ requires hermes_state signature to keep BOTH params.
- `agent/usage_pricing.py` — **U**. Unioned both pricing entries (fork claude-fable-5 + upstream claude-sonnet-5).
- `gateway/session_context.py` — **U**. Unioned fork send-origin + cron-session ContextVar helpers (fork-features #1 cron approval gating) with upstream `declare_stateless_channel`. All 4 fns present.
- `agent/copilot_acp_client.py` — **B**. Kept fork `env=_subproc_env` (relay-lane env patch) + added upstream `creationflags=windows_hide_flags()`.
- `agent/error_classifier.py` — **U**. Unioned fork malformed-conversation check + upstream empty-provider-response check (order preserved: malformed→empty→overflow).
- `gateway/status.py` — **U**. Kept fork `start_time`(pid)+`boot_id`; added upstream `hermes_home` field.
- `hermes_cli/model_switch.py` — **U**. Unioned fork failover-lane-hide regex + upstream Palantir opaque-ID display.
- `hermes_cli/cron.py` — **U**. Unioned fork `cron_run_now` + upstream `cron_runs` (durable history).
- `hermes_cli/providers.py` — **U**. Unioned fork `infer_api_mode_from_provider` + upstream `host_mandated_api_mode`.
- `scripts/release.py` — **UP+F**. Adopted upstream directory-based contributor arch (`LEGACY_AUTHOR_MAP`+`_load_contributor_dir`); dropped fork `ACP_REGISTRY_MANIFEST` refs (acp_registry/ deleted upstream, staged D); **preserved 5 fork-critical author entries** (apollo@kyzcreig.local/@daemonarchy/@ang.ventures, Kyzcreig@, DavidMetcalfe@) in LEGACY_AUTHOR_MAP so contributor-check resolves fork commits.
- `scripts/run_tests_parallel.py` — **U**. Unioned fork flags (`--min-tests`/`--strict-noop`/`--test-scope`/`--changed-files-scope`) + upstream `--file-retries`.
- `pyproject.toml` — **B**. Upstream comment + fork py-modules list (keeps fork-only `hermes_state_ext`, `hermes_undo`).
- `cli-config.yaml.example` — **U**. Unioned fork pricing block + upstream command-helper secret-source block.

**Pending high-risk (deferred to dedicated phase):** gateway/run.py (28), tui_gateway/server.py (16), hermes_state.py (11 — MUST keep both `include_timestamp`+`repair_alternation` on get_messages_as_conversation), hermes_cli/web_server.py (11), gateway/slash_commands.py (11), gateway/session.py (7), run_agent.py (7), + desktop TS + remaining tests.


---

## Worker 2 continuation (48 remaining UU → resolving)

### Pre-resolved by worker 1 (marker-free, just needed staging)
- `tools/approval.py` — **B (verified)**. Fork `_is_cron_session()` ContextVar helper (fork-feature #1) + upstream hardline-blocklist parser-limit/grep-malformed detection. Marker-free, compiles, staged.
- `.github/actions/detect-changes/action.yml` — **B (verified)**. Fork ci_review_files + upstream github-token input + test_scope output + `.previous_filename` rename surface. Marker-free, staged.

### Agent core (batch 2)
- `agent/moa_loop.py` — **B**. Upstream refactored monolithic `create()` into `prepare`/`rebase_prepared_request`/`_call_prepared_aggregator`/`create`; the single conflict (fork monolith body vs upstream `rebase_prepared_request`) took THEIRS. Then **re-injected fork's per-advisor `_pending_reference_pricing_calls` feature** (consume_reference_pricing_calls) into upstream's fan-out — was declared+consumed but upstream dropped the population site. Matched upstream's fold-not-overwrite + `_accounting_lock` contract. py_compile OK.
- `agent/auxiliary_client.py` — **B**. H1: **UP** `_read_main_model_for_aux()` (aux-scoped resolver). H2: **B** union — upstream `_close_cached_client(built_client)` concurrent-loser close + fork `_normalize_resolved_model` caller-model normalization. H3: **UP** `_unwrap_moa_provider` block (fork's api_mode-profile-fallback code was already auto-merged below; fork side was orphan comment). py_compile OK.
- `agent/chat_completion_helpers.py` — **U**. H1: union imports (`_ceil_chars_to_tokens` fork + `flatten_message_text` upstream, both used). H2: union (`relay_headers` fork-feature imports + `stream_single_writer` upstream). H3: union both added tail blocks (upstream cancel-check first, then fork reasoning/content splicer flush). py_compile OK.
- `agent/context_compressor.py` — **B**. H1+H2: union fork skew-reset/`_clear_persisted_skew_history` helper + upstream telemetry (`_begin_compression_telemetry` etc.); fixed misplaced telemetry resets into parent method. H3: **HYBRID** — chained fork `resolve_compression_threshold` (config per_model + **Codex gpt-5.5 autoraise** + global) INTO upstream `resolve_model_threshold` (substring map) so `_new_base` is defined AND fork autoraise survives a mid-session model switch (matches __init__ precedence). H4: **UP** (base log + telemetry emit superset). py_compile OK.
- `agent/conversation_compression.py` — **U**. H1: union imports (fork `compaction_ext` fork-feature helpers + upstream `context_engine` sanitize/status). H2: union both function blocks (fork announce/`_warn_compaction_stats_once`/`_emit_compaction_announce` + upstream status templates/`CompressionCommitFence`/memory-snapshot). H3: union params (`trigger_reason` fork + `defer_context_engine_notification`/`commit_fence` upstream). H4: **UP** (telemetry emit superset). py_compile OK.
- `agent/model_metadata.py` — **B**. H1: union cache-reconcile elif branches, **bedrock branch FIRST** (fork aggregator branch's `dedicated` set includes bedrock → would shadow it) then fork cross-provider aggregator branch. H2-H6: **HYBRID token estimate** — reconciled fork's tunable `COMPOSITION_CHARS_PER_TOKEN` (3.5 tail / 4.0 fixed via `_ceil_chars_to_tokens`) with upstream's CJK-density counting (`_CJK_DENSE_RE`): CJK/Hangul/Kana ~1 token each + sparse remainder at tunable 3.5. Verified: CJK`你好世界`=4 (upstream contract), `hello`=2, `a*400`=115 (fork divisor). H5: **UP** `estimate_messages_tokens_rough` return (upstream per-message text_tokens accumulation; fork `total_chars` var gone). H6: **F** system_prompt uses `_ceil_chars_to_tokens_fixed` (intentional looser 4.0 for fixed prefix). py_compile OK + runtime-verified.
- `tests/agent/test_model_metadata.py` — **U/TEST**. H1: **UP** CJK expect==4 (matches merged hybrid). H2: union both test classes (fork aggregator/Greptile + upstream moa per-model context). py_compile OK.
- `agent/conversation_loop.py` — **B**. H1: union imports (`compose_request_breakdown` fork + `_estimate_tools_tokens_rough` upstream). H2: union — keep fork `_call_composition` breakdown (used @2937/2964) AND upstream `total_chars` (used @1729/1941). H3: union — upstream `_preflight_threshold`+insufficient-progress `_preflight_compression_blocked` block + `_defer_preflight` getattr, THEN fork `_should_compress_preflight` calibration getattr (`should_compress_calibrated` inherited from ContextEngine base). H4: **UP** guard chain (max_compression_attempts + _preflight_compression_blocked + _defer_preflight; fork calibration preserved via _should_compress_preflight). py_compile OK.
- `agent/turn_context.py` — **B (intricate preflight reconciliation)**. H1: union pure helpers (fork `maybe_stamp_empty_resume_row` + upstream `compose_user_api_content`/`substitute_api_content`/etc.). H2: union (fork cron auto-model publish + upstream `note_turn_start` tripwire). H3: union — fork skew CALIBRATION setup (`note_rough_sent`+`calibrated_tokens`+cold-start observability, `_calibrated`) THEN upstream snapshot/`_defer_preflight`/`_preflight_deferred`. H4: **UP** (`_should_compress_now`/`_compress_block_reason` init). H5: **UP** flag structure but trigger changed to fork `should_compress_calibrated`. H6: **UP** `automatic_compaction_status_message`. Auto-merged loop tail (upstream `_max_preflight_passes`/`_preflight_compression_blocked` + fork calibrated re-check + `trigger_reason`) verified coherent. py_compile OK.
- `tools/async_delegation.py` — **B**. H1: **F** `_MAX_DELIVERY_ATTEMPTS = 10` + 'parked' terminal state (surviving test `test_exhausted_pending_completion_is_parked_not_re_enqueued` contracts 'parked'; merged body uses 'parked'; upstream's 8/'dropped' rejected). H2: union both signature params (fork `durable_spec`/`current_boot_id` + upstream `delegation_id`; all used). py_compile OK. **Carryover (b) resolved: kept fork 10.**
- `tools/kanban_tools.py` — **B**. H1: reconciled session-id origin — prefer upstream `_current_origin_session_id()` (request-scoped api_server binding, fixes subagent-clobber wrong-session wake), then fork `_current_session_id()` (gateway-aware contextvar), then env. H2: kept fork `model_override` arg + type-check (surviving `test_kanban_tools.py` contracts `model_override` key + non-string rejection) + accept upstream `model` alias + added `provider`/`provider_override` support. **Fixed auto-merge DUPLICATE kwarg** `model_override=model_override` in create_task call. py_compile OK.
- `cron/scheduler.py` — **B**. H1: union (fork transient-failure suppression `_is_transient_cron_failure`/`_should_suppress_transient_failure_page`/`_job_is_recurring` + upstream `_set_cron_session_title` dedup). H2: **fixed duplicate import** — unioned to one line keeping fork `get_job`. H3: **UP minus dead code** — kept upstream `_read_windows_pyvenv_cfg`+`_windows_cron_python_invocation` (Windows support, used @2446) but STRIPPED upstream's inline `_get_script_timeout` (dead in fork — fork uses `scheduler_ext.get_script_timeout`, fork-feature #6). H4: **UP** ordering (reasoning resolved AFTER provider auth) — took upstream import-only at pre-auth spot, then **re-pointed post-auth resolution (line ~3707) to fork's `scheduler_ext.resolve_cron_reasoning_config`** (per-job reasoning override, fork-feature #6) against the final post-fallback model. py_compile OK.
- `tools/delegate_tool.py` — **B**. H1: kept fork's `strip_blocked_delegate_toolsets` (fork-feature #11 tool_gate golden helper, code_execution exemption) + appended upstream's `kanban` strip. H2: **UP** — wrap `AIAgent(...)` construction in `delegated_child_context()` + `disabled_toolsets=child_disabled_toolsets`, re-threaded fork's `prefill_messages=child_prefill_messages`. H3: reconciled — upstream `delegated_child_context()` wraps run_conversation + fork's `_bind_child_send_origin`/`_bind_child_cron_session` (relay send-origin + cron approval, fork-features) around it. H4: **UP** batch comment (matches merged `dispatch_async_delegation_batch` ONE-unit behavior). H5: union. py_compile OK.
- `plugins/platforms/discord/adapter.py` — **B (dual-extraction reconciliation)**. H1: **F** restart-backfill constants (`_RESTART_BACKFILL_ANCHOR_MARGIN_S`, fork recovery feature). H2: **F** on_message calls fork's `_dispatch_incoming_message` (tested backfill-consistent path; upstream's parallel `_dispatch_discord_message`/`_discord_message_admission` left in tree but unused — no tests reference them, harmless). H3: **UP** `/reasoning` native `app_commands.choices` (max/ultra ultra-class contract). H4: union — fork's `_dispatch_incoming_message` method + adopted upstream's `_handle_message` signature (`-> bool`, `*, recovered`); **removed duplicate `_handle_message` def stub** from the union. H5: **F** restart-recovery `mark_channel_active` marking, adapted gate `return`→`return False` (upstream bool contract). Backfill (`test_restart_backfill`) path preserved. py_compile OK.




---

## Worker 3 continuation (final 27 UU + 1 AA → resolving THE hardest core)

Marker style note: after a mid-work `git checkout --conflict=diff3` the markers
became `<<<<<<< ours / ||||||| base / >>>>>>> theirs` (not HEAD/sha) — resolvers
adapted. venv = `~/.hermes/hermes-agent/venv/bin/python` for all py_compile.

### hermes_state.py — **B** (11 hunks). py_compile OK, staged.
- H1: **UP** — added `_compression_lock_holder_process_is_dead` (PID-liveness lock reclaim) + `_scrub_surrogates`.
- H2: **B** — took upstream SCHEMA_VERSION=23 + FTS_STORAGE_VERSION block, re-appended fork's 2 `_EFFECTIVE_LAST_ACTIVE_BACKFILL_*` constants (denorm feature).
- H3: **U** — union fork denorm helpers (`_is_effective_last_active_visible` etc.) + upstream FTS runtime-rebuild (`_try_runtime_fts_rebuild`/`_is_fts_write_corruption_error`).
- **H4/H5/H6 (FTS init): UP** — the merged tree GLOBALLY adopted upstream's **v23 external-content FTS** (`LEGACY_FTS_SQL`, `_ensure_fts_cjk_schema`, `_db_has_legacy_inline_fts`, `FTS_STORAGE_VERSION` all auto-merged), so the v11 inline-migration + trigram-init hunks take upstream for consistency. ⚠️ **Operator note:** fork's `_trigram_fts_config_enabled` disk-opt-out helper (4 uses) survives as a symbol but is NO LONGER in the v23 init path — the config-gated trigram *disable* feature is effectively dormant under v23. Flag for fork-feature review (not in fork-features.json).
- **H7/H8 (sessions INSERT): U** — unioned columns: upstream `git_repo_root` + fork `effective_last_active` (16 cols, 15 `?` + NULL, 15-value param tuple; counts verified).
- H9: **U** — upstream parent-metadata inherit (cwd/git_repo_root/routing COALESCE) THEN fork `_recompute_effective_last_active` calls.
- **H1(sig)/H10/H11: U** — `get_messages_as_conversation` keeps BOTH `include_timestamp` (fork LCM) + `repair_alternation` (upstream). Fixed auto-merge DUPLICATE of the `include_timestamp` guard block (h11 conversion created a dup of the canonical LCM block → removed the redundant one).

### hermes_cli/config.py — **U/B** (4 hunks). Resolves carryover (b) auth-registry warning. py_compile OK, staged.
- H1: **U** imports — `VALID_REASONING_EFFORTS, get_hermes_home, get_process_hermes_home` (get_process is a re-export, noqa:F811).
- H2: **U** — fork resume/drain keys + upstream `local_stream_stale_timeout`.
- H3: **B** — took upstream's new compression keys (min_tail_user_messages/max_attempts/proactive_prune*/hygiene_timeout/cooldown) but KEPT fork values `hygiene_hard_message_limit: 400` + `hygiene_threshold: 0.85`.
- H4: **F** — fork superset (session_list_denorm, desktop_auto_resume, session_sync all fork-only).
- gateway.py: 0 hunks (auto-merged clean), staged.

### hermes_cli/backup.py — **F superset** (3 hunks). py_compile OK, staged.
- Kept fork's bounded safe-copy (`_SAFE_COPY_DEADLINE_S` deadline + `_progress` callback + busy_timeout + page-wise 1000/step) — superset of upstream. Reconciled H1 docstring to upstream's fail-closed wording (caller already fails closed: False → error+continue, no raw-copy fallback; verified at call site).

### hermes_cli/commands.py — **B** (3 hunks). py_compile OK, staged.
- Upstream folded `/credits`+`/billing` into `/topup` (merged registry defines only `topup` CommandDef — credits/billing gone). Resolved `_SLACK_VIA_HERMES_ONLY` = `{topup, version, moa, boomerang, debug, merge}` (upstream topup base + fork additive version/boomerang/merge, all verified as real CommandDefs). H1: kept `*VALID_REASONING_EFFORTS` + added upstream `--global` flag.

### hermes_cli/web_server.py — **B/UP converge** (11 hunks). py_compile OK, AST-test verified, staged.
- **Parallel-invention convergence:** fork offloaded blocking SessionDB calls via `_blocking_io(_do)`; upstream via `asyncio.to_thread(named_helper)`. Per doctrine (equivalent parallel invention → converge on upstream). Took UPSTREAM's `to_thread`+inner-closure form for all 10 TARGET_HANDLERS (7 endpoint hunks + 2 analytics fns). Upstream's endpoints are the SUPERSET (retain fork's `already_absent` idempotent-delete + `DeclaredMemoryProvider`).
- H1: **U** imports — kept fork's `is_truthy_value` (used at ~1731, fork feature) + upstream base.
- **Analytics fns (`_get_usage_analytics`/`_get_models_analytics`) + `_prune_sessions`:** whole-function replace with upstream's copy — the between-hunk auto-merged code carried fork's deeper `_do()` indentation, producing a SYNC def containing `await _blocking_io(_do)` (SyntaxError). Upstream's analytics is a superset (keeps fork's session-only dedup fold + adds aux-usage `_aux_usage_rows`/`_merge_aux_into_by_model`/`_aux_task_summary`/`by_task`, issue #23270). Fork's extra offloaded `get_session_stats`/`_read_session_stats` endpoint preserved (not in TARGET_HANDLERS; fork dashboard-stall fix).
- `tests/test_web_server_sessiondb_eventloop.py` (AA, deferred by worker 2): **UP/TEST** — took upstream's AST source-contract test (superset, covers all 10 handlers incl. analytics via `to_thread`). Verified it PASSES against the merged tree (all 10 handlers offload DB-open to an executor helper).



---

## Gates status

_(final)_

## Items needing operator decision

_(final)_
