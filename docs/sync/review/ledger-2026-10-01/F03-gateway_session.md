# Ledger — lane F03-gateway_session (parity sync 2026-10-01, round 4, card t_5c66311e; orchestrator t_e45c8c8d)

Manifest: `L02-gw-run-FOLLOWUP-gateway_session.py.md`. Targets: `gateway/session.py`, `gateway/session_*.py`.
Method: fork delta = `git show 26350357d7:gateway/session.py` vs `b6c8bfe8ab:gateway/session.py` (AST-extracted per
method, `/tmp/e45-F03/delta_<name>.diff`); re-threaded into upstream's sibling body (upstream structure wins).
The facade's unwired fork helpers (section B) are now reached from the siblings; late imports
(`from gateway.session import _StoreLock / _claim_turn_marker_revision / SessionEntry`) follow upstream's own pattern.

## A. Moved methods (one row per manifest item)

| target::symbol | status | evidence | test(s) |
|---|---|---|---|
| session_transcript.py::_append_to_transcript_serialized | PORTED | fork `/undo /redo` hook: user-role append calls `hermes_undo.on_user_message_appended` (redo stack invalidation) inside upstream's `_enqueue_transcript_message` critical section | test_session_continuity.py (green), test_undo_error_honesty.py (green) |
| session_persistence.py::_ensure_loaded_locked | PORTED | fork lock-released load (`_with_lock_released(self._read_routing_sources)`, double-check `_loaded` after re-acquire), `_valid_routing_keys` seed, `_seed_chat_model_pins_locked()`, `_redirect_legacy_alias_routes_locked()` before prune, prune gated on `_find_session_key_conflict() is None`; upstream's `_routing_entry_from_json`/`_import_legacy_sessions_json(db_had_entries, legacy_data, legacy_error)` reused for parsing. Upstream `_load_routing_rows_locked` removed (only caller was the replaced body); `_read_routing_sources` now resolves the loader via upstream's `_routing_db_method` (routing-home handle, not ambient `_db`) | test_discord_chat_identity.py (3 reds → green), test_session_store_lock_io.py, test_session_store_lock_never_spans_io.py, test_session_store_stale_prune.py, test_session_store_prune.py (green) |
| session_persistence.py::_persist_routing_data | PORTED | `require_primary=` (re-raise state.db failure instead of silent JSON fallback — `/model reset` contract), `retired_keys=` passthrough to `replace_gateway_routing_entries` and the mirror, `_defer_while_locked` guard (never runs under `_StoreLock`), `SessionKeyConflict` → `_reject_session_key_conflict`, mirror via `_dispatch_sessions_json_save(must_be_synchronous=not db_saved)` (writer thread, #782), `_valid_routing_keys = set(data)` after commit | test_session_route_concurrency.py, test_sessions_json_write_off_loop.py, test_session_route_invariant.py, test_c5_session_races.py (green) |
| session_persistence.py::_save_entry | PORTED | returns the allocated revision (`Optional[int]`), `_assert_unique_session_routes(candidate)` at capture, I/O half delegated to the facade's `_persist_captured_entry` deferred past `_lock` (`_defer_while_locked`), inline for non-`_StoreLock` doubles. `_persist_captured_entry` saver now resolved via `_routing_db_method` | test_routing_save_fast_path.py, test_active_turn_recovery.py, test_session_route_invariant.py (green) |
| session_lifecycle.py::recover_interrupted_turns | PORTED | `user_stopped_at is not None` → crash marker cleared without promotion (ruling 2026-09-21) inside upstream's `_promote` closure | test_active_turn_recovery.py (green) |
| session_persistence.py::_reconcile_recovered_routing_locked | PORTED | `allow_release=` kwarg: load step reads state.db via `_with_lock_released`; baseline/`_routing_db_loaded` re-checked after re-acquire | test_session_store_lock_io.py (green) |
| session_transcript.py::rewrite_transcript | PORTED | `hermes_undo.clear_state(session_id)` after a successful replace (renumbered rows invalidate the undo/redo stack) | test_undo_error_honesty.py (green) |
| session_transcript.py::load_transcript | PORTED | `include_timestamp=True` on `get_messages_as_conversation` (fork LCM ingest; kwarg lives in F01's `hermes_state_messages.py`, present in tree) | test_session_continuity.py: 1 red, see XFOLLOWUP-TEST below |
| session_transcript.py::rewind_session | PORTED | plain `/undo` path → facade `_rewind_via_undo_core` (honest busy/error/None outcomes; `hermes_undo._session_db` now the owning store via `_db_for_session_id`) under the drain lock; `require_retryable_composite=True` keeps upstream's `db.rewind_user_turn(require_retryable=True, require_composite=True)` body. Facade's `_rewind_retryable_composite` (fork copy of base's inline rewind) DELETED — superseded by upstream's `rewind_user_turn` | test_undo_error_honesty.py (8/8 green), test_retry_replacement.py (green) |
| session_lifecycle.py::mark_turn_active | PORTED | capture under `_lock` → `_save_entry(entry_data=candidate)` with lock released → `_publish_persisted_entry` with `_claim_turn_marker_revision` (highest revision wins). Upstream's `_set_turn_marker_locked` (lock-held save) removed; upstream's aware-UTC `started_at` + `_iso()` kept | test_active_turn_recovery.py, test_session_store_lock_io.py (green) |
| session.py::suspend_recently_active | NO-PORT-NEEDED | already restored on the facade by round 3 with the `user_stopped_at` guard (manifest "What WAS re-threaded") | — |
| session_persistence.py::close_all_db_handles | PORTED | `stop_sessions_json_writer(timeout=10.0)` drained before the handle sweep (warn on timeout) | test_session_db_handle_sharing.py, test_session_messages_shutdown_preserve.py (green) |
| session_lifecycle.py::mark_resume_pending | PORTED | `resume_kind/resume_handoff/resume_request_id` kwargs; body = facade `_mark_resume_pending_in_memory_locked` (legacy-alias redirect, suspended + `/stop` guard) + `_save()`. Live callers gateway/run.py:11281 (`resume_kind="self"`) satisfied | test_session_recovery.py, test_session_override_thread_recovery.py (green) |
| session_persistence.py::_prune_stale_sessions_locked | PORTED | `_StoreLock`-held caller → `_with_lock_released(_prune_stale_sessions_off_lock)`; lock-free → `_plan_stale_prune(items)` + `_apply_stale_prune_locked(plan, expected=None)`. Plan merges upstream's per-key `_db_for_key(key)` owner lookup + `_stale_entry_verdict` with the fork's `never_persisted_stub` reap on a missing row. Facade's duplicate `_plan_stale_prune(db, items)` (ambient-handle variant) deleted; `_prune_stale_sessions_off_lock` no longer gates on `self._db` | test_session_store_stale_prune.py, test_session_store_prune.py, test_c5_session_races.py::test_prune_skips_route_healed_in_place (green); test_session_store_never_persisted_stub.py: inherited ImportError (`SessionResetPolicy` missing from gateway/config.py, outside lane) |
| session_lifecycle.py::clear_resume_pending | PORTED | `**kw` with `marked_at=` CAS (FleetReview #1043: a re-mark during the turn is kept); clears via facade `_clear_resume_pending_entry` (all fork marker fields). Live callers gateway/run.py:10914/10932 pass `marked_at=` | test_session_recovery.py (green) |
| session_lifecycle.py::clear_turn_active | PORTED | same persist-then-publish shape as mark_turn_active; live token kept until the clear is durable | test_active_turn_recovery.py (green) |
| session_lifecycle.py::_is_session_ended_in_db | NO-PORT-NEEDED | fork delta was docstring-only (routing moved to `_routing_entry_staleness_in_db`, which the facade already carries); upstream body is a superset (`session_key=` owner lookup, hard-deleted row == ended) and `run_turn_runner.py:1055` calls the upstream signature | test_multiplex_session_db_profile_scope.py, test_reaped_session_recovery.py (green) |
| session_persistence.py::_save_sessions_json | PORTED | `retired_keys=`; cross-process `.sessions.lock` (`gateway.status._try_acquire_file_lock`), `assert_unique_routing_entries(data, existing, retired_keys=)` before replace, body via facade `_write_sessions_json_unlocked` (now uses upstream's `_SESSIONS_JSON_README`; `atomic_json_write` import dropped — unused) | test_sessions_json_write_off_loop.py, test_session_route_invariant.py (green) |
| session_persistence.py::_snapshot_routing_locked | PORTED | `_assert_unique_session_routes()` + `_last_full_snapshot_generation = generation` (read by `_publish_persisted_entry` to re-save when a full snapshot superseded a single-entry write) | test_c5_session_races.py::test_clear_override_keeps_newer_set (green) |
| session_recovery.py::_generate_session_key | PORTED | `source_resolver(source)` hook before `build_session_key` (gateway's adapter-derived canonicalization for direct store writers). NOTE: the fork's wiring `self.session_store.source_resolver = self._canonicalize_session_source` sat in run.py `__init__` (b6c8bfe8ab:8603) and is absent from the merged `_init_session_store` → **XFOLLOWUP gateway/run.py (F02): set `source_resolver` in `_init_session_store`** | test_session_key_producer_contract.py (1 inherited red, see below) |
| session_persistence.py::_save | PORTED | `require_primary=`, `retired_keys=` kwargs forwarded (was the live `TypeError: _save() got an unexpected keyword argument 'retired_keys'` on 3 tests) | test_manual_reset_sticky_route.py, test_discord_chat_identity.py (TypeError gone) |

## B. Unwired fork helpers — now wired

`_persist_captured_entry` (← `_save_entry`), `_prune_stale_sessions_off_lock` (← `_prune_stale_sessions_locked`),
`_read_routing_sources` (← `_ensure_loaded_locked`), `_redirect_legacy_alias_routes_locked` (← `_ensure_loaded_locked`),
`_rewind_via_undo_core` (← `rewind_session`), `_seed_chat_model_pins_locked` (← `_ensure_loaded_locked`),
`_with_lock_released` (← load/reconcile/prune), `_write_sessions_json_unlocked` (← `_save_sessions_json`),
`stop_sessions_json_writer` (← `close_all_db_handles`), `_claim_turn_marker_revision` (← mark/clear_turn_active).
`lookup_chat_model_pin`: public API, callers are gateway/run.py (F02) — left as is. `_rewind_retryable_composite`: deleted (see rewind_session).

## C. Extra fix in-lane (merged-only red, own target)

`session.py::migrate_discord_session_keys` passed the key's namespace slot (`main`) as `profile=` to `build_session_key`;
upstream's `_session_key_namespace` marks a profile literally named `main` as `main~`, so every migrated route re-keyed to
`agent:main~:...` (test_discord_chat_identity 3 reds). Now `profile=profile_from_session_key_namespace(parts[1])`.

## D. Verification

py_compile + `ruff --select F821,F811,F823` clean on all 5 files (pre-existing F401s untouched: `uuid` in session.py,
TYPE_CHECKING `SessionSource` in session_lifecycle.py — both at HEAD before this lane).
Run via e45-pt.sh (file-scoped), 63 files: `tests/gateway/test_discord_chat_identity.py test_manual_reset_sticky_route.py
test_session_*.py (46, never_persisted_stub excluded: inherited ImportError) test_undo_error_honesty.py test_retry_replacement.py
test_active_turn_recovery.py test_routing_save_fast_path.py test_c5_session_races.py test_sessions_json_write_off_loop.py
test_multiplex_session_db_profile_scope.py test_reaped_session_recovery.py` → **427 passed, 57 failed** (`/tmp/e45-F03/merged-wide.txt`).
Named-trio before this lane: 22 failed / 56 passed; after: 19 failed / 59 passed (3 fixed: retired_keys TypeError ×3 → now
the discord trio green; the manual_reset trio moved to other causes, below).

Remaining 57 reds, classified against base (`/tmp/e45-F03/base-wide.txt`, fork/main):
- XFOLLOWUP gateway/run_turn.py (F02): test_session_hygiene.py ×16, test_session_hygiene_turnhold_adoption.py ×3 — root cause
  `TypeError: GatewayRunner._pinned_channel_inputs() got an unexpected keyword argument 'internal'` (run_turn.py:2235) and
  `_hmwa_hygiene_build_agent` `'NoneType' object has no attribute 'get_session'` (run_turn.py:1298); the "Something went wrong" reply is the symptom.
- XFOLLOWUP gateway/run.py / run_turn.py (F02): test_session_model_reset.py ×21 (reset banner ignores preserved route
  preferences: `'global-model' == 'pinned-model'`, `SessionRouteUnavailableError` in run_turn.py:235), test_manual_reset_sticky_route.py
  ×3 (`_manual_reset_invalid_persisted_identity` banner, `test_real_sqlite_transcript_stays_on_old_session` test lambda
  `hermes_state.SessionDB` monkeypatch no longer matches upstream's `hermes_state_registry.acquire(db_path=)` seam — stale test, see below;
  `test_gateway_config_parse_warning_does_not_log_source_snippet` = config_read_errors wording, hermes_cli lane).
- XFOLLOWUP gateway/session_state.py (not in lane list? — it IS `session_*`, but the delta is upstream-only: `KeyError:
  'agent:main:telegram:dm:c1'` at session_state.py:163) : test_session_context_inheritance.py ×5 — run_agent rebinding
  through `gateway/run.py` (F02 `_run_agent` siblings); the KeyError is a missing per-session state slot created by the
  runner, not by the store. Leave for F02.
- XFOLLOWUP gateway/run_shutdown.py (F02): test_c5_session_races.py::test_stuck_loop_suspend_holds_store_lock — upstream's
  `_suspend_stuck_loop_sessions` mutates entries / calls `_save` outside `_lock` (fork held it; PR #1043).
- XFOLLOWUP gateway/run.py (F02) : test_session_collision_notice.py ×2 (`on_session_key_conflict` notifier wiring `_notify_session_key_conflict`,
  b6c8bfe8ab:run.py:8602, absent from `_init_session_store`) — same spot as the `source_resolver` wiring above.
- XFOLLOWUP hermes_cli / providers lanes: test_session_model_override_persistence.py ×4 (llamacpp/opencode/codex rehydrate — `rehydrate_model_override` lives in gateway/run_*; relay URL heal), test_session_list_allowed_sources.py ×1,
  test_session_key_producer_contract.py ×1 (allowlist names `TeamsAdapter._on_message`/`TelegramAdapter._build_message_event` no longer in source — platform lane).
- XFOLLOWUP-TEST tests/gateway/test_session_continuity.py::TestLoadTranscriptReroutes::test_load_transcript_raises_when_message_read_fails:
  upstream test's `_malformed(_session_id, *, repair_alternation)` stub rejects the fork's `include_timestamp=True` kwarg
  (fork test test_undo_redo_half_turn.py / test_idle_compaction_gateway_gap.py assert that kwarg IS passed). Proposed:
  `def _malformed(_session_id, *, repair_alternation, include_timestamp=False)`.
- XFOLLOWUP-TEST tests/gateway/test_manual_reset_sticky_route.py::test_real_sqlite_transcript_stays_on_old_session (and the
  27 other tests patching `hermes_state.SessionDB` with a 0-arg lambda): upstream opens via `hermes_state_registry._open_session_db(path)` →
  `SessionDB(db_path=path)`; the fork-era `lambda: db` seam now raises `unexpected keyword argument 'db_path'` and falls to JSONL.
  Proposed: patch `hermes_state_registry._open_session_db` (`lambda path: db`) or accept `**_`.

## E. Base run (fork/main, `e45-ptd.sh`, same file list minus 14 files that only exist merged-side)

43 files → **418 passed, 0 failed** (`/tmp/e45-F03/base-wide.txt`). So every red above passes on base = merged-only
regression; none has its cause in `gateway/session*.py` after this lane (each traced to a gateway/run*.py / run_turn.py /
run_shutdown.py / session_state.py runner path, a test seam moved by upstream, or a non-session lane), hence XFOLLOWUP not
fixed here. test_session_collision_notice ×2 and test_session_model_reset also trip `tests/home_io_guard.py` on
`run_voice._load_voice_modes` / `resume_requests` writing to the sandbox `.hermes` (`_hermes_home` monkeypatch seam moved
in run.py `__init__` → F02).
Merged-only test files (not classifiable on base; all green except continuity ×1 and turnhold ×3 listed above):
test_session_chat_approval, _continuity, _db_corrupt_fallback, _db_handle_sharing, _db_replaced_fallback,
_db_warning_recheck, _degraded_db_continuity, _hygiene_turnhold_adoption, _identity, _identity_restore, _recovery,
_split_brain, _time_persistence, test_reaped_session_recovery.
