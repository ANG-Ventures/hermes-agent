# FOLLOWUP manifest — gateway/session.py (lane L02-gw-run, parity 2026-10-01)
Fork tip (:2) = b6c8bfe8ab, upstream (:3) = 612d8e44a2, base (:1) = 26350357d7.
Upstream decomposed SessionStore into mixin siblings (gateway/session_persistence.py, session_recovery.py,
session_lifecycle.py, session_transcript.py, session_prompt_pin.py, session_identity.py). Per brief
('upstream structure wins; re-thread fork behaviour'), session.py keeps upstream's facade + mixin MRO; the
fork's edits to every method upstream MOVED must be re-threaded into the sibling that now owns it. Those
siblings are outside this lane (added by upstream, no conflict), so nothing below is applied yet.
Recover fork bodies with `git show b6c8bfe8ab:gateway/session.py` and diff against
`git show 26350357d7:gateway/session.py` for the fork delta.

## What WAS re-threaded in the facade (done, not followup)
- SessionEntry fork fields restored on the dataclass + to_dict/from_dict (had_any_turn, resume_kind/handoff/
  request_id, user_stopped_at/boot_id/message_id, reasoning_override, model_override_identity +
  _model_override_identity_invalid, last_served_identity); legacy model_override -> identity upgrade on load.
- build_session_key: canonical_chat_type() slot (legacy Discord 'channel' producers) on upstream's rewrite.
- _get_or_create_session_impl (upstream split into _apply_route_checks/_route_recover/_route_create):
  _RouteChecks.is_stale now carries _routing_entry_staleness_in_db() reason (never_persisted_stub log),
  schedule_reset gates on had_any_turn, _route_create discards the turn handoff when the candidate wins.
- update_session(served_identity=, expected_session_id=) + had_any_turn latch; set/get_model_override
  chat-model-pins read/write outside _lock; reset_session(preserve_route_preferences=) carrying
  model_override_identity/reasoning_override; _discard_turn_handoff on reset/switch; _StoreLock as _lock;
  sessions.json writer-thread state in __init__; _StoreLock, PersistedSessionRouteLookup,
  PERSISTABLE_MODEL_IDENTITY_KEYS, _NEVER_PERSISTED_STUB_GRACE, atomic_replace/RewindWouldOrphanError imports;
  suspend_recently_active restored (upstream deleted it; fork added the /stop never-auto-resume guard).

## A. Methods upstream MOVED to a sibling that the fork had MODIFIED (re-thread the fork delta there)
Format: `FOLLOWUP <qualname> -> <sibling> (fork lines vs base lines)`. 'ABSENT' = upstream deleted the
method outright; check what replaced it before porting. Highest value first: _save/_save_entry/
_save_sessions_json/_ensure_loaded_locked carry the t_cc8533d1 lock-free persistence + coalescing
sessions.json writer + chat-model-pins seeding + discord alias redirect; mark_turn_active/clear_turn_active
carry _claim_turn_marker_revision; rewind_session carries the retryable composite rewind.

```
FOLLOWUP SessionStore._append_to_transcript_serialized           -> gateway/session_transcript.py  (fork 185L vs base 176L)
FOLLOWUP SessionStore._ensure_loaded_locked                      -> gateway/session_persistence.py  (fork 118L vs base 101L)
FOLLOWUP SessionStore._persist_routing_data                      -> gateway/session_persistence.py  (fork 94L vs base 56L)
FOLLOWUP SessionStore._save_entry                                -> gateway/session_persistence.py  (fork 92L vs base 122L)
FOLLOWUP SessionStore.recover_interrupted_turns                  -> gateway/session_lifecycle.py  (fork 65L vs base 61L)
FOLLOWUP SessionStore._reconcile_recovered_routing_locked        -> gateway/session_persistence.py  (fork 57L vs base 42L)
FOLLOWUP SessionStore.rewrite_transcript                         -> gateway/session_transcript.py  (fork 56L vs base 46L)
FOLLOWUP SessionStore.load_transcript                            -> gateway/session_transcript.py  (fork 50L vs base 49L)
FOLLOWUP SessionStore.rewind_session                             -> gateway/session_transcript.py  (fork 47L vs base 98L)
FOLLOWUP SessionStore.mark_turn_active                           -> gateway/session_lifecycle.py  (fork 41L vs base 33L)
FOLLOWUP SessionStore.suspend_recently_active                    -> ABSENT  (fork 41L vs base 35L)
FOLLOWUP SessionStore.close_all_db_handles                       -> gateway/session_persistence.py  (fork 40L vs base 28L)
FOLLOWUP SessionStore.mark_resume_pending                        -> gateway/session_lifecycle.py  (fork 30L vs base 28L)
FOLLOWUP SessionStore._prune_stale_sessions_locked               -> gateway/session_persistence.py  (fork 29L vs base 101L)
FOLLOWUP SessionStore.clear_resume_pending                       -> gateway/session_lifecycle.py  (fork 28L vs base 19L)
FOLLOWUP SessionStore.clear_turn_active                          -> gateway/session_lifecycle.py  (fork 27L vs base 24L)
FOLLOWUP SessionStore._is_session_ended_in_db                    -> gateway/session_lifecycle.py  (fork 22L vs base 25L)
FOLLOWUP SessionStore._save_sessions_json                        -> gateway/session_persistence.py  (fork 16L vs base 39L)
FOLLOWUP SessionStore._snapshot_routing_locked                   -> gateway/session_persistence.py  (fork 12L vs base 7L)
FOLLOWUP SessionStore._generate_session_key                      -> gateway/session_recovery.py  (fork 11L vs base 8L)
FOLLOWUP SessionStore._save                                      -> gateway/session_persistence.py  (fork 9L vs base 4L)
```

## B. Fork-ADDED SessionStore methods kept on the class in session.py but UNWIRED (their callers were in
methods from section A, which upstream moved). Reachable as `SessionStore.<name>`; wire them back when
porting the section-A owner.

```
UNWIRED SessionStore._persist_captured_entry                    merged L2038-2097
UNWIRED SessionStore._prune_stale_sessions_off_lock             merged L2768-2791
UNWIRED SessionStore._read_routing_sources                      merged L2710-2737
UNWIRED SessionStore._redirect_legacy_alias_routes_locked       merged L1903-1970
UNWIRED SessionStore._rewind_retryable_composite                merged L1833-1901
UNWIRED SessionStore._rewind_via_undo_core                      merged L1972-2036
UNWIRED SessionStore._seed_chat_model_pins_locked               merged L2793-2813
UNWIRED SessionStore._with_lock_released                        merged L2856-2873
UNWIRED SessionStore._write_sessions_json_unlocked              merged L2427-2465
UNWIRED SessionStore.lookup_chat_model_pin                      merged L3113-3119
UNWIRED SessionStore.stop_sessions_json_writer                  merged L3059-3070
UNWIRED _claim_turn_marker_revision                             merged L1017-1031
```

## C. Upstream-DELETED base methods the fork still carried (unmodified unless noted)
Dropped with upstream (no fork delta, no live caller): _is_session_expired (weixin.py matches are its own
local helper), _is_session_key_unsafe, _should_reset (replaced by _route_reset_reason in
session_lifecycle.py), is_session_finalizable, set_expiry_finalized (evals/ only).
RESTORED: suspend_recently_active (fork-modified: user_stopped_at guard, ruling 2026-09-21); its fork
caller sat in run.py's unclean-restart recovery (run.py FOLLOWUP section A).

```
_is_session_expired (method:SessionStore, in_base=True, L3527-3566)
_is_session_key_unsafe (top, in_base=True, L150-172)
_should_reset (method:SessionStore, in_base=True, L3672-3717)
is_session_finalizable (method:SessionStore, in_base=True, L3568-3597)
set_expiry_finalized (method:SessionStore, in_base=True, L3480-3525)
suspend_recently_active (method:SessionStore, in_base=True, L4905-4945)
```

## D. Verification done in-lane
py_compile pass; ruff F821/F811/F841 clean; 0 dangling self.* references against the facade + session_*
mixins; conflict_resolution_gate AC1/2/4/5 pass, AC3 = sections A+C above. Import smoke on a scratch tree
(fork HEAD + upstream session_* siblings): `import gateway.session` OK, SessionStore MRO = facade +
Persistence/Recovery/Lifecycle/Transcript/PromptPin mixins, SessionEntry.to_dict/from_dict round-trip of
every fork field OK, api_key never serialized, malformed identity -> _model_override_identity_invalid.
SessionStore construction/get_or_create/reset runtime smoke could NOT run: the shared worktree's other
lanes are still unresolved (hermes_cli/config_defaults.py etc.), so no coherent import tree exists yet —
orchestrator should run tests/gateway/test_session*.py after all lanes land.
