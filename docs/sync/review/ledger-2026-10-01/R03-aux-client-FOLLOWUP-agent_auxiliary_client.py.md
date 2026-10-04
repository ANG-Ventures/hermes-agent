# agent/auxiliary_client.py — R03-aux-client handoff (2026-10-01, run 17803, stopped at context ceiling)

State: UNRESOLVED. Worktree file still carries 56 diff3 hunks, unstaged (`git ls-files -u` non-empty).
Stages extracted here BEFORE any staging: base.py (:1, 11,285 l), ours.py (:2 fork, 12,411 l), theirs.py (:3 upstream, 8,255 l).

## Measurements (do not redo)
- Fork commits in range: 23 real (+2 parity merges). Upstream: 270 commits incl. five "densify" passes
  (7946921ff6, 369bfe4a87, 002d5125f3, b848a36eab, 5e15b982d5) that inline single-use helpers and pack
  signatures, plus e326520d50 (one codex credential authority), 56490ca109 (drop _read_codex_access_token),
  aa40c1d21b (Messages adapter on declared api_mode), ee17fff193 (one bypassing create in _relay_sync_stream).
- fork_delta.diff = `diff -u base.py ours.py`: 75 hunks, 674 fork-added code lines, 667 fork-only
  (not present upstream). 34 fork-only defs (list in REJECT-INDEX.txt / aux_measure.py output).
- Upstream removed 42 base defs (shims `_AnthropicChatShim`/`_Async*Adapter`, `_read_main_*` partials
  → `functools.partial(_read_main_field, ...)`, `_resolve_single_provider`, `_timed_dispatch`, ...).
  None of them is a fork symbol; they are upstream's own refactor.
- `patch -l --fuzz=3 theirs.py fork_delta.diff` → try1.py: 19/75 hunks applied (16 regions), 56 REJECTED
  (try1.rej). REJECT-INDEX.txt lists each reject: header, anchor def/ctx, defs added, first added line.
  ⚠ the 19 fuzz-applied hunks must be eyeballed too (fuzz=3 on a densified file can land in the wrong
  twin: sync vs async call_llm paths are near-duplicates).

## Method (same as moa_loop.py / prompt_builder.py in this lane — see /tmp/e45-R03-aux-client/moa_resolve.py)
theirs.py is the structure. Write `aux_resolve.py` as a sequence of `rep(old, new)` exact-anchor
replacements (assert hit count) starting from theirs.py (NOT try1.py unless each of its 19 hunks is
verified), re-threading the fork delta feature by feature. Write to the worktree only when all
features are in; then markers=0, py_compile, conflict_resolution_gate.py, `git add`.

## Fork features in the 56 rejects (feature → fork commit → hunks by base line in try1.rej)
A. Relay-pool lane headers / aux route id (d197480cb0, de490b8d6d; fork feature "relay-pool session
   affinity + lane headers"): @230 event_hooks on build_keepalive_http_client (process_bootstrap already
   re-added the kwarg, ledger L08-agentb); @8801 `_aux_route_scope() as aux_route` inside call_llm's
   context stack; `_record_relay_auxiliary_response_model`/`_relay_auxiliary_route_snapshot` recording.
   `pool_headers` must end up on the response object (agent/moa_loop.py reads `response.pool_headers`).
B. gemini-bridge claim headers (30c9f6479f): @4155 and @9054 `merge_extra_headers` instead of
   `dict(extra_headers)` (fork_ext/gemini_bridge_claims; aux_accounting.get_accounting_context kept for it).
C. Cancel-per-frame + worker thread state (da809fe10b, 49244e92c8): @562 `_on_aux_anthropic_stream_event`
   (upstream now has `_anthropic_aux_stream_event_hook()` at theirs:368 — thread the cancel check into it),
   @614 `_CombinedAuxCancelSignal`/`capture_aux_thread_state`/`raise_if_aux_cancel_requested` + the aux
   cost sink block (D), @1804 hook wiring, @8600 `_raise_if_aux_cancel_requested()` in stream `feed`.
D. Blackbox aux cost sink (2d8c56015c, df5eb16c72, a6934eeee0): `_aux_cost_sink` ContextVar,
   `new_aux_cost_sink`/`aux_cost_sink`/`aux_cost_sink_total`/`_mark_sink_unknown`/`_record_aux_call_cost(_locked)`
   (inside @614) + call sites after every physical completion (sync, async, stream → streamed=True).
   Consumers (verified): `aux_cost_sink` ← agent/conversation_compression.py; `capture_aux_thread_state`/`raise_if_aux_cancel_requested` ← plugins/context_engine/lcm/parallel_summary.py; `resolve_compression_threshold` ← agent/agent_init.py, agent/context_compressor.py.
E. `{provider: auto}` fallback entry = session main route (86394bbdc5, c72ab03fd8): @5896 (+128:
   `AUTO_CHAIN_PROVIDER`, `_is_auto_chain_entry`, `resolve_auto_chain_entry`, `resolve_task_fallback_chain`,
   `_select_main_fallback_entry`), @6000 `_main_runtime_route` (+60) and @6052/@6093 its use in
   `_resolve_auto_route`, `main_runtime=` threaded through `_try_configured_fallback_for_unavailable_client`,
   `_try_main_fallback_chain` (4-tuple return incl. entry; skip_indices/skip_routes) and all callers
   (@5724..@5978, @8971, @9629/@9639, @9493/@9503, @9872), @4992 `_is_auto_chain_entry(fallback_entry)`.
   Consumers (verified in merged tree): `resolve_task_fallback_chain` ← agent/context_compressor.py, agent/conversation_compression.py; `resolve_auto_chain_entry` is called only inside auxiliary_client itself (+ tests).
F. Context-length rejection → fallback before LCM truncation (8f65041e23): `_is_context_length_error`
   def applied in try1; sites @9314/@9226 (max_tokens retry guard), @9563/@9586/@9439/@9454 (fallback
   predicate), @9602/@9466 (`elif _is_context_length_error`).
G. Relay deploy-drain 503 wait (8ec714aeed): defs applied in try1 (`_relay_drain_wait_for`, `_is_relay_drain`,
   `_call_through_relay_drain`, `_acall_through_relay_drain`); call-site wraps are the two big rejects
   @9649 (+82/-7 sync fallback candidate) and @9519 (+80/-7 async), plus @9126.
H. Compression observability: `_log_compression_call_duration` + outcome bookkeeping (a309b9d8f1) @8825 (+71),
   @8779 `outcome = "failed"`; refusal outcome on refusal-shaped 200s (0cd04ede2d) and
   `response_validator` kwarg (3e2ac5b581) @8767, @8919 (+35 compression route diagnostics).
I. Codex: shared `get_codex_account_id` accessor (b8d5fe20e6) @1373 — CHECK upstream e326520d50 first
   (upstream may have its own single accessor; converge on upstream if equivalent), `codex_uses_large_window`
   (06e8732c70, model.codex_context_policy — model_metadata side already resolved, ledger L05) @837,
   `_context_probe_api_key` @5814.
J. Parity-merge-era deltas (4bbad92dbf/aa27fd8be4): `_scope_client_for_task` @8282 (+42), `provider_data=_nr.provider_data` @2339,
   `_aux_task` kwargs block @2278 (+31, Anthropic adapter sampling-params), @8445 api_mode resolution (+22;
   compare with upstream 807d7a77f1/aa40c1d21b — likely parallel invention, converge on upstream),
   @8286 `normalized_caller_model`, @9184 `_record_route_info(..., task)`, @9773 async impl route_info (+33/-15).
K. `_AUTH_JSON_PATH = Path(get_hermes_home()) / "auth.json"` (hermeticity: get_hermes_home may return str in fork).

Applied by fuzz in try1 (verify placement): `_raise_if_aux_cancel_requested`, `_model_vendor_family`,
`_pinned_model_incompatible_with_provider`, `resolve_compression_threshold`, relay_headers module import,
`_relay_auxiliary_route_snapshot`, `_is_context_length_error`, relay-drain defs, `_context_probe_api_key`,
gemini claim_headers import, `_first_attempt` async wrapper.

Parallel-invention candidates to converge on upstream (verify before porting): I (codex account id),
J@8445 (api_mode canonicalisation), H refusal handling vs upstream 22753744fb (HTTP-200 router timeout shim).
