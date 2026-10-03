# CI5 ledger — lane L2-agent-core (parity PR #1624, CI round 5)

Branch `sync/upstream-2026-10-01-ci-L2-agent-core` off `117c1c249d60f49b57c476a3d8ec049e2c2d78b3`.
Card t_c2b76c07. Reds from CI run 36967435601 (37 files / 143 reds); manifest
`tools/lanes-r5/L2-agent-core.md`. Verified narrowly per file through
`~/.hermes/scripts/test-gate` with a sandboxed HOME (never the full suite); "green" below is the
file's narrow run after the fix. Commits: 67c9bd23e63 / d432849d6bf (round 1),
d7ee0d1b2be / 8765835476d (round 2).

Legend: FIXED-CODE (fork behaviour restored onto upstream's structure) · FIXED-TEST (test pinned a
moved symbol / stale shape) · INHERITED (fails on fork/main too) · OPEN.

| red file | disposition | what / why | narrow result |
|---|---|---|---|
| tests/agent/test_aux_cache_isolation.py | FIXED-TEST | `tests/_fixtures/env_filter.py`: bare `_KEY` credential suffix stripped (fork conftest contract, T-misc appendix A) so `TERMINAL_DOCKER_SHARED_CONTAINER_KEY` never leaks into the aux env | 13 passed |
| tests/agent/test_background_review_cache_parity.py | FIXED-CODE + FIXED-TEST | `background_review.build_cache_parity_fork` reads `parent._conversation_root_id` via getattr (upstream SimpleNamespace parents); gateway-key test gc-collects earlier Recorder forks before claiming the per-live-fork "review" tag | 16 passed |
| tests/agent/test_background_review_session_tag.py | FIXED-TEST | double accepts upstream's extra kwargs | 2 passed |
| tests/agent/test_codex_owner_selection_review.py | FIXED-CODE | `hermes_cli.auth._auth_lock_path` fork facade symbol restored (68 fixtures monkeypatch it); `CredentialPool._auth_owner` class default; **codex_owner.refresh on a 429 reserves the token's probe slot** (`auth_codex._reserve_codex_quota_probe_slot`) so `_refresh_expired_codex_probe_token` (#89415) does not re-POST the same single-use refresh token on the next selection — the merge produced a real double-POST (`['refresh-bad','refresh-bad']`) | 48 passed |
| tests/agent/test_credential_pool.py | FIXED-TEST | `codex_sync_pool` fixture patches `httpx` on `hermes_cli.auth_codex` and carries `SyncByteStream` (upstream response-body cap subclasses it) | 72 passed |
| tests/agent/test_credential_pool_adopt_rotated_entry.py | FIXED-CODE | `try_refresh_matching` id-bound adopt-rotated-entry branch re-threaded (L05 ledger had ported the other two call sites only) | 8 passed |
| tests/agent/test_credential_pool_codex_singleton_isolation.py | FIXED-TEST | upstream test expected a `manual:device_code` row to adopt a re-authed singleton; fork contract (agent/codex_owner, #673): a row's authority is its own row in the owning store, never an inferred singleton alias — asserts the manual row keeps its pair and the seeded `device_code` row carries the re-auth | 2 passed |
| tests/agent/test_credential_pool_terminal_refresh_visibility.py | FIXED-CODE | same `_auth_owner` default / auth facade restore as above | 8 passed |
| tests/agent/test_curator_disk_accounting.py | FIXED-TEST | subject is on-disk accounting (#699); with no usage rows the real candidate list is empty and the pass is skipped, so the test hands the fork a non-empty `_render_candidate_list` | 2 passed |
| tests/agent/test_entitlement_fail_closed.py | FIXED-TEST | fork replaced upstream's unconditional "Primary model restored" notice with the shared, `model.announce_recovery`-gated "Model recovery (restore): …" announcement; test enables the gate and asserts that line | 2 passed |
| tests/agent/test_inline_edit_persistence.py | FIXED-TEST | `get_messages_as_conversation(name, include_timestamp=True)` — fork F01 keeps the default projection byte-stable (same shape as test_replay_cleanup) | 11 passed |
| tests/agent/test_interrupt_close_owns_transcript.py | FIXED-CODE + FIXED-TEST | relay-lane interrupt-close omission (`provider_owns_transcript` / `is_interrupt_close_row`, t_f40dc54a) restored in `turn_context.build_api_messages` (the fork send loop moved there); contract test reads that function | 6 passed |
| tests/agent/test_message_sequence_repair.py | FIXED-TEST | compares the wire-visible shape (upstream's persistence-only `_merged_turn_prefix` marker) | 73 passed |
| tests/agent/test_non_chat_primary_restore.py | FIXED-TEST | as test_entitlement_fail_closed (recovery announcement) | 2 passed |
| tests/agent/test_openai_ultrafast.py | FIXED-TEST | stub carries the first-party Codex route (fork tiers are route-gated) | 11 passed |
| tests/agent/test_plugin_stream_hooks.py | FIXED-CODE | `_StreamingCall._attempt_pool_headers` read via getattr for bare `__new__` instances | 13 passed |
| tests/agent/test_prompt_cache_ttl_propagation.py | FIXED-TEST | #84733 restart-discipline guard rewritten for upstream's phase-helper split (`run_conversation` no longer contains the retry loop): every `_try_activate_fallback` site in `agent/turn_*` must be an `if` test whose verdict is `"break"` (retry-loop phases, armed via `_arm_fallback_restart`/`_fallback_break`) or `"continue"` (outer-loop phases). Mutation-checked: flipping `recover_empty_response` to `"break"` fails the guard | 10 passed |
| tests/agent/test_provider_client_seam.py | FIXED-TEST | fixture restores through `provider_seam._restore` (facades are additive) | 4 passed |
| tests/agent/test_relay_error_class.py | FIXED-TEST | shares the `_run` rig of test_pool_capacity_503_retry: upstream c5b99a3ee5 keeps direct-call contexts on the streaming wire, so the rig disables streaming to land the primary on `_interruptible_api_call` | 32 passed |
| tests/agent/test_replay_cleanup.py | FIXED-TEST | `include_timestamp=True` (F01); `_SendAgent` double carries `provider` for the interrupt-close filter | 6 passed |
| tests/agent/test_route_id_correlation.py | FIXED-TEST | fake Anthropic stream yields a `message_stop` event — upstream `_drain_stream` treats a stream without it as a drop (`EmptyStreamError`) | 39 passed |
| tests/agent/test_shell_hooks.py | FIXED-CODE | spawn EACCES/ENOENT named exactly, no OSError detail (argv can be the credential); unparseable/empty stdout is its own fail-closed reason "unparseable stdout" (fork #656 shape), distinct from an unknown directive | 83 passed, 2 skipped |
| tests/agent/test_stream_serving_provider.py | FIXED-TEST | helper imports → `tests.run_agent._run_agent_helpers` | 2 passed |
| tests/agent/test_system_prompt.py | FIXED-TEST | patches `agent.prompt_builder.build_environment_hints` / `build_context_files_prompt` (where the code reads them) | 75 passed |
| tests/agent/test_turn_ended_task_id_log.py | FIXED-CODE | `turn_finalizer` `_diag_msg`/`_diag_args` keep the fork source contract (end on `task=` / `effective_task_id`); upstream's `origin=` tag rides as a suffix on both logger calls | 3 passed |
| tests/agent/test_usage_pricing.py | FIXED-TEST | models.dev leg exercised with an id absent from the fork's bundled Grok snapshot (grok-4.3 is in `_OFFICIAL_DOCS_PRICING`); `xai-oauth` and `api.x.ai` over HTTPS price at xAI list rates by fork design (notional relay / host match) — relay host and plain-http origins stay `unknown` | 138 passed |
| tests/agent/transports/test_plugin_transport_api_mode.py | FIXED-CODE + FIXED-TEST | fixture teardown via `provider_seam._restore`; **`provider_seam._restore` carries forward containers registered after the snapshot** (lazy `HERMES_OVERLAYS`) — otherwise every later read raised `KeyError` | 4 passed |
| tests/honcho_plugin/test_pin_peer_name.py | FIXED-CODE + FIXED-TEST | fork's byte-keyed memo (coarse-mtime filesystems served a stale pinPeerName, C7 k102) restored in `HonchoMemoryProvider.identity_signature`; test reads the key where upstream routes it (`memory.pin_user_identity`) | 27 passed |
| tests/plugins/blackbox/test_loader_e2e.py | FIXED-TEST | collection error from the moved `tests.agent.test_run_agent` helpers → `tests.run_agent._run_agent_helpers` | 4 passed |
| tests/plugins/memory/test_mem0_thread_lifecycle.py | FIXED-TEST | same helper repoint | 5 passed |
| tests/plugins/test_plugin_paths_follow_profile.py | FIXED-TEST | mem0-qdrant case dropped: fork retired upstream's mem0 OSS backend (`_oss_providers`/`VECTOR_PROVIDERS`; standing decision docs/sync/review/mem0-resolution-decision.md, L11 ledger) — no qdrant default path exists to resolve | 3 passed |
| tests/providers/test_auth_registry_mid_discovery.py | FIXED-TEST | isolation via `provider_seam._reset` / `_restore` (auth-side `PROVIDER_REGISTRY` mirror is part of the same generation) | 3 passed |
| tests/providers/test_oauth_pkce_plugin.py | FIXED-TEST | finalizers via `provider_seam._restore` (pop refused on additive facades) | 7 passed |
| tests/run_agent/test_run_agent_api_kwargs.py | FIXED-TEST | fresh-build assertion checked before `_build_system_prompt` — upstream 921ab7a163 seeds the workspace pin from the session row inside the build, which is not the stored-prompt lookup under test | 89 passed |
| tests/run_agent/test_run_agent_init_memory.py | FIXED-CODE + FIXED-TEST | **`model.max_tokens` config override restored** in `agent_init._resolve_context_length` (dropped in the phase split; populates `agent.max_tokens` + `_session_init_model_config`); nudge-counter / on_turn_start pins moved to `turn_context` | 18 passed |
| tests/run_agent/test_run_agent_misc.py | FIXED-TEST | `_SafeWriter` / `_is_destructive_command` via their modules; `TestSessionJsonSnapshotOptIn` / `TestSaveSessionLogRedactsSecrets` dropped — subject `_save_session_log` removed upstream 7a5fc1b2a9e and replaced by tests/agent/test_session_snapshot_removal.py; upstream `_enable_native_compaction` helper added for the merged checkpoint tests. `test_aiagent_reuses_existing_errors_log_handler` fails only under the sandbox harness (home-io guard on `$SB/.hermes/logs`), not a CI red | 56 passed, 1 local-only |
| tests/run_agent/test_usageless_response_accounting.py | FIXED-CODE | `turn_usage`: compressor update gated on a MEASURED prompt count from the pre-fold aggregator usage (r6 finding 7 / round-4 finding 2) plus the one-shot Codex 272K tier notice (#1567) — both lived in the fork's conversation_loop usage block and were dropped at the `turn_usage` site | 20 passed |

Cross-lane notes:
- `jittered_backoff` → `agent.retry_utils` repointed in 5 files; L3b's `test_route_change_turn_contract` left to L3b. `tests.agent.test_run_agent` helper imports repointed in 9 files; L1's `test_length_continuation_thinking_exhaustion` left to L1.
- `tests/hermes_cli/test_provider_auth_seam.py` (L5 manifest) was re-run as a neighbour of the provider_seam change: it errors identically with and without this lane's diff — L5's red, untouched here.
- Source files touched that other lanes may also touch: `agent/turn_context.py`, `agent/credential_pool.py`, `hermes_cli/auth.py`, `hermes_cli/provider_seam.py` (minimal edits; orchestrator resolves overlap).
