# F01-hermes_state — round-4 re-thread ledger (parity 2026-10-01, card t_b28570ee)

Manifest: `L13-root-py-followup-hermes_state.md` (53 items). Targets: `hermes_state.py` + `hermes_state_*.py`.
Nothing committed; 12 target files + this ledger are `git add`ed. Every file: `py_compile` OK,
`ruff --select F821,F811,F823` clean. `_production_state_roots` was pre-restored by the orchestrator.

Counts: PORTED 37 · NO-PORT-NEEDED 14 · DROPPED 1 · XFOLLOWUP 0 (code) · XFOLLOWUP-TEST 4 · TODO 0.

## Rows

| target::symbol | status | evidence | test(s) |
|---|---|---|---|
| sessions::`_delete_delegate_children` | PORTED | `orphaned_child_ids` out-param collects stragglers before the FK orphan UPDATE | test_delegate_cascade, test_session_list_denorm_acceptance |
| sessions::`SessionDB._insert_session_row` | PORTED | previous root captured before upsert; `effective_last_active` column in INSERT; recompute root + row after `_inherit_parent_session_metadata` | denorm_acceptance / denorm_reland |
| sessions::`SessionDB.end_session` | PORTED | root captured before `_end_and_bump`, recompute after (upstream's `_end_and_bump` kept) | test_session_lifecycle_status |
| sessions::`SessionDB.reopen_session` | PORTED | root captured before the busy-queue retire / unarchive, recompute after | test_auto_archived_lineage_reactivation |
| sessions::`SessionDB.clear_session_activity_labels` | NO-PORT-NEEDED | upstream reads via `self._read_one(...)` (no `self._conn`); fork delta was the same `_read_ctx` fix | — |
| sessions::`SessionDB.update_session_meta` | PORTED | `_execute_write` closure with previous-root capture + both recomputes (model_config flips visibility / root) | denorm_reland `recompute_required` |
| sessions::`SessionDB.update_system_prompt` | PORTED | `logger.warning(..., stack_info=True)` on explicit `system_prompt=None` | — |
| sessions::`SessionDB.update_session_model` | NO-PORT-NEEDED | upstream body no longer nulls `system_prompt`/`system_prompt_hash` nor calls `_delete_unreferenced_system_prompts` (grep: 0 hits in method) — fork delta absorbed | — |
| sessions::`SessionDB.update_session_runtime_lock` | NO-PORT-NEEDED | same: upstream retains the prompt (0 `system_prompt` hits in method) | — |
| sessions::`SessionDB.set_session_archived` | PORTED | `ARCHIVE_END_REASON = "archived"`; archive retires live rows (`COALESCE(ended_at/end_reason)`), deletes lineage's `gateway_routing` rows by `$.session_id`; unarchive reverses only `'archived'` rows. Live probe: routing key for archived session gone, sibling kept, `end_reason='archived'` | test_session_archiving (pass), test_archive_retires_routing (see XFOLLOWUP-TEST/F03 below) |
| sessions::`SessionDB.list_sessions_rich` | PORTED | `_force_cte_oracle` kwarg; `dashboard.session_list_denorm` gate → `_list_sessions_rich_denorm`; `_list_row` pops both `_effective_last_active` and `effective_last_active`. Upstream's `COALESCE(child.source,'') != 'tool'` CTE clause RESTORED (prior attempt had removed it; not a manifest item, upstream 98a6e4a90e, removal proven non-load-bearing: same reds before/after) | test_session_list_denorm_acceptance (oracle parity rows pass), test_pinned_archived_sidebar |
| sessions::`SessionDB.session_count_by_source` | PORTED | `if conn is None` (was `self._conn`) | — |
| sessions::`SessionDB.get_session_delete_targets` | NO-PORT-NEEDED | upstream already passes `conn` to `_collect_delegate_child_ids` | test_delegate_cascade |
| sessions::`SessionDB.delete_session` | PORTED | orphan collection (delegate + direct) → `_recompute_effective_last_active_many` after DELETE | denorm_acceptance `[delete-session]` pass |
| sessions::`SessionDB.delete_sessions` | PORTED | same shape, chunked | denorm_acceptance `[delete-sessions]` pass |
| sessions::`SessionDB.delete_empty_sessions` | PORTED | same shape | denorm_acceptance `[delete-empty]` pass |
| facade::`__getattr__` (fork-only) | PORTED | lazy `DEFAULT_DB_PATH` resolves `get_hermes_home()` at access; `_IMPORT_DEFAULT_DB_PATH` snapshot kept; `_default_db_path()` honours a monkeypatched real global via `globals().get` | test_production_root_anchor, test_live_db_guard_ancestry |
| facade::`_production_state_roots` | NO-PORT-NEEDED | restored by orchestrator before this lane (card body) | test_production_root_anchor |
| facade::`SessionDB.restore_rewound` | DROPPED | symbol deleted upstream; fork delta was docstring-only (deprecation note). `git grep restore_rewound -- hermes_state_* gateway agent tests` → 0 hits; `restore_ids` (the replacement) is in the facade | test_undo_redo_stack pass |
| guard::`_real_platform_state_root` | PORTED | anchored on `hermes_state._os_account_home()` (late import) with `expanduser` fallback, both win32 and posix arms | test_live_db_isolation_guard (1 inherited red, see below), test_production_root_anchor |
| common::`_trigram_fts_config_enabled` (fork-only) | NO-PORT-NEEDED | already in `hermes_state_common.py` and consumed by `hermes_state_schema.py:1309` (landed in rounds 1-3) | test_fts_runtime_rebuild, test_fts_rebuild_admission pass |
| repair::`_persistent_repair_exhausted_error` | PORTED | `hint_value(str(db_path))` on both `--source` occurrences (upstream rewrote the message to `hermes sessions recover`; shell-quoting re-threaded onto it) | — |
| repair::`_backup_db_file` | PORTED | upstream moved the message into `_backup_free_space_error` / `_MANUAL_RECOVER_HINT`; `hint_value` applied there | — |
| repair::`_db_opens_cleanly` | NO-PORT-NEEDED | comment-only delta (test path reference); upstream rewrote the comment (0 hits for the old path) | — |
| fts::`SessionDB._is_trigram_unavailable_error` | PORTED | `"no such tokenizer" in err and ("trigram" in err or "cjk_unicode61" in err)` — fork's looser match, keeping upstream's cjk tokenizer coverage | test_hermes_state_core (not run: file not in lane list; logic is a strict superset) |
| fts::`SessionDB._drop_fts_triggers` | NO-PORT-NEEDED | manifest diff is empty (identical bodies) | — |
| gateway::`SessionDB.record_gateway_session_peer` | PORTED | root captured first; self-heal INSERT gated on `not ancestors`; recompute root + row after the insert | test_orphan_gateway_session_repair |
| gateway::`SessionDB.save_gateway_routing_entry` | PORTED | `_execute_write` closure calling `_assert_unique_gateway_routes` before upsert | test_routing_write_isolation (2 merged-only reds are F03, below) |
| gateway::`SessionDB.replace_gateway_routing_entries` | PORTED | `retired_keys=()` kwarg → `_assert_unique_gateway_routes(..., retired_keys=)` | gateway/session_persistence already passes `retired_keys` |
| gateway::`SessionDB.adopt_orphaned_gateway_session` | PORTED | recompute for orphan and donor after the re-parent | test_orphan_gateway_session_repair pass |
| gateway::`SessionDB.get_handoff_state` | NO-PORT-NEEDED | upstream uses `_read_one` | — |
| gateway::`SessionDB.list_pending_handoffs` | NO-PORT-NEEDED | upstream uses `_read_all` | — |
| compression::`SessionDB.publish_compression_child` | PORTED | parent `end_reason == ARCHIVE_END_REASON` tolerated like automatic reasons; `_recompute_effective_last_active_for_session(child)` after the closure UPDATE | test_auto_archived_lineage_reactivation, test_composite_carrier_rewind |
| compression::`SessionDB.acquire_session_turn_lease` | PORTED | `wait_notice_backoff=2.0`, `wait_notice_max_interval_seconds=300.0`; geometric notice cadence after the first interval | test_session_turn_lease pass |
| compression::`SessionDB.get_compression_lock_holder` | NO-PORT-NEEDED | upstream uses `_read_one` | — |
| messages::`SessionDB.set_message_api_content` (fork-only) | NO-PORT-NEEDED | present in merge commit 0c16eec702 `hermes_state_messages.py` (landed rounds 1-3) | — |
| messages::`SessionDB.append_message` | PORTED | `_bump_effective_last_active_for_message` after the INSERT | denorm_reland insert contract (checked across all siblings: 0 offenders) |
| messages::`SessionDB.append_messages_batch` | PORTED | `row_ids_out` positional contract (inserted ids vs repaired `_row_id`), extended only after commit; chunked recursion passes it through | test_append_messages_batch pass |
| messages::`SessionDB._insert_message_rows` | PORTED | `row_ids_out` + per-row `_bump_effective_last_active_for_message` | test_append_messages_batch |
| messages::`SessionDB.replace_messages` | PORTED | recompute after counters | denorm_reland `recompute_required` |
| messages::`SessionDB.archive_and_compact` | PORTED | `_carry_parent_timestamps` moved BEFORE the rewind/archive (both branches); `_active_duplicate_tool_result_ids` snapshot + `_raise_if_compaction_introduced_duplicate_results` (new sibling helper, late-imports `TranscriptInvariantError`); recompute on both return paths | test_composite_carrier_rewind pass |
| messages::`SessionDB.get_messages` | PORTED | `preserve_unparseable_tool_calls` → `_row_to_message_dict` sentinel `{"unparseable": True}` | gateway auto-resume gate callers |
| messages::`SessionDB.get_messages_as_conversation` | PORTED | `include_timestamp=False` kwarg threaded to `_rows_to_conversation` (live symptom in card body) | test_state_rewind_generalized, test_half_turn_target pass |
| messages::`SessionDB._rows_to_conversation` | PORTED | `timestamp` surfaced only on opt-in; tool rows carry `name`; `_dedupe_replayed_user` keyed on the ROW timestamp (not the gated msg key); `_resolve_carried_row_ids` falls back to unique identity when timestamp absent | test_state_rewind_generalized |
| messages::`SessionDB.rewind_to_message` | PORTED | `require_user_role=True` positional; `_raise_if_rewind_would_orphan_tool(..., conn=conn)` pre-mutation; `rewound_ids` in result (live symptoms in card body) | test_undo_redo_stack, test_half_turn_target, test_state_rewind_generalized, gateway/test_undo_error_honesty — all pass |
| messages::`SessionDB.clear_messages` | PORTED | recompute after reset | — |
| usage::`SessionDB.update_session_billing_route` | NO-PORT-NEEDED | upstream body has no `system_prompt` nulling / `_delete_unreferenced_system_prompts` (0 hits) | — |
| usage::`SessionDB._coalesce_token_deltas` | PORTED | `_TOKEN_DELTA_SNAPSHOT_FIELDS` last-non-None-wins merge; `_TOKEN_DELTA_FLAG_FIELDS` folded into the route key (facade class attrs) | test_aux_usage_accounting, test_session_model_usage_pk_heal pass |
| usage::`SessionDB.update_token_counts` | PORTED | `*_unknown` absorbing flags (`MAX(COALESCE(f,0),?)`), `last_turn_*` snapshot columns (`COALESCE(?, col)`), `cost_status` CASE judged on post-update dollars; param order documented inline | test_aux_usage_accounting |
| usage::`SessionDB._record_model_usage` | PORTED | 5 unknown-flag columns in `_MODEL_USAGE_UPSERT_SQL` with `MAX()` merge; per-row `cost_status` scoping ('unknown' vs 'partial' on the row's own dollars) | test_session_model_usage_pk_heal |
| usage::`SessionDB.record_auxiliary_usage` | PORTED | unknown-flag kwargs normalised into `usage` | test_aux_usage_accounting |
| maintenance::`SessionDB.prune_sessions` | PORTED | orphan collection before the chunked DELETE, `_recompute_effective_last_active_many` after | see XFOLLOWUP-TEST (prune contract) |
| maintenance::`SessionDB.logical_size_bytes` | PORTED | `if conn is None` in the PRAGMA helper (`_read_pragma_ints`, upstream's extraction) | — |

## Test verdicts (file-scoped via e45-pt.sh; baseline via e45-ptd.sh on `base/`)

Named undo/rewind suites (5 files): **48 passed, 2 failed**. `tests/hermes_state/*` touching ported symbols
(21 files): **264 passed, 14 failed, 6 skipped**. Every red classified:

| red | class | note |
|---|---|---|
| gateway/test_undo_drain_guard::test_stop_registers_only_a_not_done_drain, ::test_stop_sets_persist_superseded_on_live_agent | XFOLLOWUP F02-gateway_run | `_draining_turns` registration + `running_agent._persist_superseded = True` on /stop live in gateway/run_agent_cache.py; already row 68 (TODO) of F02 ledger. Pass on base. |
| hermes_state/test_archive_retires_routing (5), test_routing_write_isolation::test_routing_save_lands_in_the_sandbox_not_production, ::test_no_pytest_scoped_rows_in_production_routing | XFOLLOWUP F03-gateway_session | upstream's `SessionStore._routing_db` (gateway/session_persistence.py:84, #66887) pins `_routing_home/state.db` = `get_hermes_home()` at construction, ignoring the fork tests' `monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp/state.db)` idiom (base had no `_routing_home`; routing went through `_db` → `_default_db_path()`). `store._db` and `_routing_db` are now two different files, so the durable table read by the test is empty and `home_io_guard` flags the sandbox home write. SessionDB itself is correct (live probe above). Either F03 makes `_routing_home` honour a re-pointed `DEFAULT_DB_PATH`, or XFOLLOWUP-TEST: fixtures set `HERMES_HOME` instead of the module constant. Pass on base (7). |
| test_routing_write_isolation::test_session_store_with_tmp_scope_cannot_bind_production_db | inherited | fails on base too (`DID NOT RAISE RuntimeError`) |
| test_live_db_isolation_guard::TestSubprocessChildCovered::test_child_without_hermes_home_is_refused | inherited | fails on base too (`assert 0 != 0`) |
| test_session_list_denorm_acceptance::test_all_five_delete_orphan_sites_promote_surviving_continuation[prune-sessions], test_session_list_denorm_reland::test_prune_orphan_recomputes_denorm_and_survives_reopen | XFOLLOWUP-TEST | upstream 7d49b46e15 "prune keeps the compressed-away start of a chat still in use": `prune_sessions` now uses `whole_lineages=True` (`_continued_ancestors_sql`), so a compression parent with a surviving continuation child is deliberately NOT pruned. The fork tests build exactly that shape and assert the parent is gone. Proposed: make the child also match the prune window / end it, or drop the `prune-sessions` parametrization (the recompute port in `prune_sessions` is in place; the other 4 delete sites pass). Pass on base only under the old contract. |
| test_session_list_denorm_acceptance::test_all_six_session_parent_mutation_sites_are_maintenance_adjacent | XFOLLOWUP-TEST | scans facade + `_portability` only → `assert 1 == 7`. Writers now live in `_sessions/_gateway/_profile_repair/_maintenance`. Scanning every `hermes_state_*.py`: 8 writer sites, 0 unmaintained (verified in-lane). Proposed: iterate `glob("hermes_state_*.py")` and assert `sites - maintained == set()` without the `== 7` count (change-detector). |
| test_session_list_denorm_reland::test_every_sessiondb_message_insert_path_is_effective_last_active_adjacent, ::test_every_parent_session_id_writer_is_effective_last_active_adjacent | XFOLLOWUP-TEST | both read `Path(hermes_state.__file__)` only (facade) → sees no production INSERT / parent writer. Across all siblings: insert paths `{_clone_message_tail_rows, _clone_message_rows, _db_opens_cleanly(exempt)}` 0 offenders (`append_message` now uses the `_INSERT_MESSAGE_SQL` constant and bumps). Proposed: scan `hermes_state_*.py`. |

## Decisions under ambiguity
- Upstream's `COALESCE(child.source,'') != 'tool'` clause in `list_sessions_rich` restored (unlisted removal by attempt 1; upstream intent 98a6e4a90e; no test depends on the removal).
- `_raise_if_compaction_introduced_duplicate_results` is a new sibling helper in `_messages` (late-import of `TranscriptInvariantError`), not a facade append, per the brief's sibling rule.
- `restore_rewound` ledgered DROPPED rather than re-added: upstream deleted it, fork delta was prose, no caller or test remains.
