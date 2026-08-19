# Reasoning/Model Switch Visibility — Implementation Plan

> **For Hermes:** Implement task-by-task via `subagent-driven-development` (fresh subagent per task, two-stage review) OR serially. Each task = one RED→GREEN→commit cycle.

**Goal:** Make deliberate `/reasoning`+`/model` switches honest (footer) and visible (channel announce), make session overrides survive gateway restart, and announce server-side model reroutes.

**Architecture:** Four independent phases against `gateway/run.py`, `gateway/runtime_footer.py`, `gateway/slash_commands.py`, `gateway/session.py`, `agent/conversation_loop.py`, `agent/turn_context.py`. P1 (footer) is the reported-bug fix and ships first. P4 gates on a Phase-0 live probe.

**Spec:** `~/.hermes/plans/2026-07-07_reasoning-model-switch-visibility-SPEC.md` (APPROVED v2.0, Momus 5-pass).

**Tech Stack:** Python 3.11, pytest, Hermes gateway. Target repo `Kyzcreig/hermes-agent` fork/main → PR → deploy.

**Test env:** `HERMES_HOME=$(mktemp -d)` for any test touching config/sessions. Run from the dev checkout `~/.hermes/hermes-agent`.

---

## PHASE 1 — Honest footer (the reported-bug fix)

### Task 1.1: RED — footer reflects session reasoning override
**Files:** Test `tests/gateway/test_runtime_footer_session_reasoning.py`
**Step 1:** Write failing test: build a `GatewayRunner` (or minimal harness) with `_session_reasoning_overrides[key] = {"enabled": True, "effort": "high"}`, config default `agent.reasoning_effort: xhigh`; call the footer path; assert the rendered footer contains `r:high`, not `r:xhigh`.
**Step 2:** `pytest tests/gateway/test_runtime_footer_session_reasoning.py -v` → FAIL (footer shows xhigh).
**Step 3:** GREEN — add `GatewayRunner._reasoning_effort_for_footer(self, *, source, session_key)` (spec §5B): resolve via `_resolve_session_reasoning_config`; `None`→`""`, `{enabled:False}`→`"none"`, else the effort. Thread it into the footer call at `gateway/run.py:~12097` as `reasoning=(_footer_reasoning or None)`.
**Step 4:** `pytest ... -v` → PASS.
**Step 5:** Commit `feat(gateway): footer reflects session reasoning override (P1)`.

### Task 1.2: RED — footer falls back to config when no override
**Step 1:** Test: no override set, config `xhigh` → footer shows `r:xhigh` (proves `None` passthrough preserves existing config-fallback behavior).
**Step 2-4:** Should already pass after 1.1 (the `or None` keeps the fallback). If not, fix the passthrough.
**Step 5:** Commit `test(gateway): footer config fallback when no override`.

### Task 1.3: RED — disabled reasoning shows r:none
**Step 1:** Test: override `{enabled: False}` → footer shows `r:none`.
**Step 2-4:** GREEN per 1.1's `"none"` branch.
**Step 5:** Commit `test(gateway): footer r:none for disabled override`.

### Task 1.4: E2E — real build_footer_line
**Step 1:** Test drives the real `gateway.runtime_footer.build_footer_line` through the runner's threading; assert `r:high` end-to-end (no mocking the footer builder).
**Step 2-4:** GREEN.
**Step 5:** Commit `test(gateway): e2e footer session-reasoning`.
**SMOKE:** `pytest tests/gateway/test_runtime_footer_session_reasoning.py -v` → all pass.

---

## PHASE 2 — Deliberate-switch channel announce

### Task 2.1: RED — _announce_switch helper + reasoning announce
**Files:** `gateway/run.py` (helper), `gateway/slash_commands.py` (call), test `tests/gateway/test_switch_announce.py`
**Step 1:** Test: stub adapter capturing `send(...)`; invoke `_handle_reasoning_command` with `/reasoning high` on a session at effective `xhigh`; assert exactly one `🔀 Reasoning: xhigh → high`.
**Step 2:** FAIL (no announce).
**Step 3:** GREEN — add `GatewayRunner._resolved_effort_label(self, *, source, session_key)` (spec §5C RC-1: config-fallback-inclusive, never `""` for a defaulted session) + `async _announce_switch(self, source, kind, old, new)` (spec §5C: no-op-silent, `model.announce_switch` gate default-on, best-effort try/except, `adapter.send`). In `_handle_reasoning_command`: capture `old = _resolved_effort_label(...)` before apply, `new` after, `await self._announce_switch(event.source, "Reasoning", old, new)` on effort-change branches only.
**Step 4:** PASS.
**Step 5:** Commit `feat(gateway): announce deliberate /reasoning switch (P2)`.

### Task 2.2: RED — config-default no-op stays silent (RC-1 regression)
**Step 1:** Test `test_no_announce_on_noop_reasoning_config_default`: NO override, config `xhigh`, `/reasoning xhigh` → zero sends. Plus `test_reasoning_switch_old_side_is_config_default`: no override + config `xhigh`, `/reasoning high` → `🔀 Reasoning: xhigh → high` (old side is config default, not empty).
**Step 2-4:** GREEN (the `_resolved_effort_label` config fallback makes old==new → silent).
**Step 5:** Commit `test(gateway): reasoning announce no-op + config-default baseline (RC-1)`.

### Task 2.3: RED — baseline resolver matches footer resolver (RC-A)
**Step 1:** Test `test_announce_baseline_matches_footer_resolver`: for config states `xhigh`/`high`/unset/`none`, the announce baseline == footer's resolved effort (no spurious announce on unset).
**Step 2-4:** GREEN.
**Step 5:** Commit `test(gateway): announce/footer resolver agreement (RC-A)`.

### Task 2.4: RED — /model announce + no-op silent
**Step 1:** Tests: `test_model_switch_announces` (`🔀 Model: <old-prov/model> → <new>`); `test_no_announce_on_same_model` (silent); `test_no_announce_on_display_toggle` (`/reasoning show` → silent).
**Step 2:** FAIL for model.
**Step 3:** GREEN — in `_handle_model_command`: capture `old = f"{current_provider}/{current_model}"` before switch, announce `old → f"{result.target_provider}/{result.new_model}"` (compare `(provider,model,api_mode)` per BL-1 compounding fix). Skip on display toggles.
**Step 4:** PASS.
**Step 5:** Commit `feat(gateway): announce deliberate /model switch (P2)`.

### Task 2.5: RED — gate + best-effort
**Step 1:** Tests: `test_announce_switch_gate_off_suppresses` (`model.announce_switch=false` → zero sends); `test_announce_send_failure_does_not_break_handler` (raising adapter → handler still returns its confirmation string).
**Step 2-4:** GREEN.
**Step 5:** Commit `test(gateway): switch-announce gate + best-effort`.
**SMOKE:** `pytest tests/gateway/test_switch_announce.py -v` → all pass.

---

## PHASE 3a — Persist reasoning override across restart

### Task 3a.1: RED — SessionEntry.reasoning_override field + serialize
**Files:** `gateway/session.py`, test `tests/gateway/test_reasoning_override_persistence.py`
**Step 1:** Test: set `entry.reasoning_override = {"enabled": True, "effort": "high"}`, `to_dict()`/`from_dict()` round-trip preserves it; `test_old_sessions_json_without_field_loads` (absent → None).
**Step 2:** FAIL (no field).
**Step 3:** GREEN — add `reasoning_override: Optional[dict] = None` to `SessionEntry`; serialize in `to_dict` (beside `suspended`); read in `from_dict` via `data.get("reasoning_override")`.
**Step 4:** PASS.
**Step 5:** Commit `feat(gateway): persist SessionEntry.reasoning_override (P3a)`.

### Task 3a.2: RED — write-through in the setter
**Step 1:** Test `test_reasoning_override_persists_to_sessions_json`: `_set_session_reasoning_override(key, cfg)` sets `entry.reasoning_override` AND triggers `_save()`; `_set_session_reasoning_override(key, None)` nulls it.
**Step 2:** FAIL.
**Step 3:** GREEN — extend `_set_session_reasoning_override` (run.py:4916) to write the entry field + `store._save()` (single-door — all existing clear-points already call this).
**Step 4:** PASS.
**Step 5:** Commit `feat(gateway): reasoning-override write-through to sessions.json (P3a)`.

### Task 3a.3: RED — boot rehydrate
**Step 1:** Test `test_reasoning_override_rehydrated_on_boot`: real `SessionStore` over temp `HERMES_HOME`, set override + `_save`, construct fresh runner from same dir (simulated restart), run rehydrate → `_session_reasoning_overrides[key] == {"enabled": True, "effort": "high"}`.
**Step 2:** FAIL.
**Step 3:** GREEN — after in-memory dict init (run.py:~3121), iterate session entries via a public accessor (add `SessionStore.entries()` returning `self._entries.values()`); for each with `reasoning_override`, populate `_session_reasoning_overrides[key]`. Guard non-dict → skip.
**Step 4:** PASS.
**Step 5:** Commit `feat(gateway): rehydrate reasoning override on boot (P3a)`.

### Task 3a.4: RED — cleared on reset (C5)
**Step 1:** Test `test_reasoning_override_cleared_on_reset`: `/new`, `/reset`, `/reasoning reset` each null both the in-memory dict AND the persisted field.
**Step 2-4:** GREEN (setter already nulls both since 3a.2; verify each clear-point routes through the setter).
**Step 5:** Commit `test(gateway): reasoning override cleared on conversation boundary (C5)`.
**SMOKE:** `pytest tests/gateway/test_reasoning_override_persistence.py -v` → all pass; then a manual: set `/reasoning high`, `cat $HERMES_HOME/sessions.json | jq '.[].reasoning_override'`.

---

## PHASE 3b — Persist model override across restart (secret-safe, config-backed-only)

### Task 3b.1: RED — model_override_identity field (NO secret)
**Files:** `gateway/session.py`, test `tests/gateway/test_model_override_persistence.py`
**Step 1:** Test `test_persisted_model_override_has_no_secret`: serialize an entry with `model_override_identity` → the dict contains `model`/`provider`/`api_mode` and NO `api_key` AND NO `base_url`.
**Step 2:** FAIL.
**Step 3:** GREEN — add `model_override_identity: Optional[dict] = None` (only `{model,provider,api_mode}`); serialize + `from_dict`.
**Step 4:** PASS.
**Step 5:** Commit `feat(gateway): persist SessionEntry.model_override_identity, no secret (P3b/RC-3)`.

### Task 3b.2: RED — single-door setter routes all clear-sites (RC-2)
**Step 1:** Test `test_all_model_clear_sites_route_through_single_door`: grep-based — no bare `_session_model_overrides.pop(` outside `_set_session_model_override`.
**Step 2:** FAIL (bare pops exist at run.py 8060, 11196, 12224, 10621, 3563).
**Step 3:** GREEN — add `_set_session_model_override(session_key, override_or_None)` (sets/pops in-memory dict + writes/nulls `entry.model_override_identity` config-backed-only + `_save()`); route the `/model` store (slash_commands.py:~1569) AND all clear-sites through it.
**Step 4:** PASS.
**Step 5:** Commit `refactor(gateway): single-door model-override setter (P3b/RC-2)`.

### Task 3b.3: RED — rehydrate re-resolves credentials, config-backed-only + ad-hoc skip (RC-3/RC-4)
**Step 1:** Tests: `test_rehydrated_model_override_reresolves_credentials` (key AND base_url from provider config, not disk); `test_adhoc_model_override_not_persisted_and_noted` (inline-key/unresolvable → not written + confirmation note); `test_rehydrate_runs_after_provider_config_load`; `test_rehydrate_skips_when_provider_gone`.
**Step 2:** FAIL.
**Step 3:** GREEN — at boot rehydrate (AFTER provider-config load), for each `model_override_identity`, re-resolve `api_key`+`base_url` from provider config (reuse `/model` resolution); on failure skip + debug log. In `_set_session_model_override`, persist only if config-resolvable; else skip + note. Add `test_model_command_has_no_inline_credential_flag` (C7 — `parse_model_flags` accepts no key/url arg).
**Step 4:** PASS.
**Step 5:** Commit `feat(gateway): rehydrate model override with credential re-resolve (P3b/RC-3/RC-4)`.
**SMOKE:** `pytest tests/gateway/test_model_override_persistence.py -v` + `grep -c api_key $HERMES_HOME/sessions.json` → 0.

---

## PHASE 4 — Server-side reroute announce (Phase-0 GATED)

### Task 4.0: Phase-0 probe (GATE — do not wire until this passes)
**Objective:** Empirically prove `response.model` diverges from `agent.model` on a real Anthropic safety reroute AND is non-null.
**Step 1:** Add temporary instrumentation at `agent/conversation_loop.py:~4183` (right after `assistant_message = normalized`): `logger.info("REROUTE_PROBE requested=%s served=%s", agent.model, getattr(response, "model", None))`.
**Step 2:** Deploy to a throwaway/sibling gateway; fire a known safety-flagged prompt at `claude-fable-5`; read the log.
**Step 3:** **DECISION GATE:** if `served` is non-null AND diverges (e.g. `fable-5` → `opus-4-8`) → proceed to 4.1. If `served` is null or never diverges → **STOP, report P4 un-buildable** (the reroute is server-internal/unobservable), remove the probe, do NOT ship a dead detector. Record the observed `(requested, served)` pair in a `PHASE-0-reroute-probe.md`.
**Step 4:** Remove the temporary log line.

### Task 4.1: RED — _emit_reroute_announce fires on served≠requested
**Files:** `agent/chat_completion_helpers.py` (or a small module) for `_emit_reroute_announce`; `agent/conversation_loop.py` (call); `agent/turn_context.py` (reset); test `tests/agent/test_reroute_announce.py`
**Step 1:** Test `test_reroute_announces_on_served_ne_requested`: agent with `model="claude-fable-5"`, stub `status_callback`; feed a response with `.model="claude-opus-4-8"`; assert one `🔀 Model rerouted (server-side): claude-fable-5 → claude-opus-4-8`.
**Step 2:** FAIL.
**Step 3:** GREEN — implement `_emit_reroute_announce(agent, requested, served)` (spec §5F: `_strip_vendor_prefix`+lower normalize, `_last_reroute_announced` dedup, `model.announce_reroute` gate default-on, `agent._emit_status`). Call it UNCONDITIONALLY at `conversation_loop.py:~4183` (OUTSIDE the `has_hook("post_api_request")` gate) with `agent.model` + `getattr(response,"model",None)`.
**Step 4:** PASS.
**Step 5:** Commit `feat(agent): announce server-side model reroute (P4)`.

### Task 4.2: RED — fires without the plugin hook (BL-P4-2)
**Step 1:** Test `test_reroute_fires_without_post_api_request_hook`: no `post_api_request` hook registered → the announce still fires (proves it's outside the hook gate).
**Step 2-4:** GREEN (already outside the gate from 4.1).
**Step 5:** Commit `test(agent): reroute announce fires without plugin hook (BL-P4-2)`.

### Task 4.3: RED — silence cases + normalize + readonly (C8)
**Step 1:** Tests: `test_no_reroute_announce_when_served_equals_requested`; `test_reroute_normalizes_vendor_prefix` (`claude-app/claude-opus-4-8` vs `claude-opus-4-8` → silent); `test_reroute_announce_is_readonly` (`agent.model` unchanged after announce); `test_reroute_silent_after_client_failover` (post-failover served==requested → silent); `response.model=None` → silent, no crash.
**Step 2-4:** GREEN.
**Step 5:** Commit `test(agent): reroute announce silence + normalize + readonly (C8)`.

### Task 4.4: RED — per-turn dedup + reset at turn_context (BL-P4-1)
**Step 1:** Tests: `test_reroute_dedup_per_turn` (same transition twice in a turn → one announce); `test_reroute_dedup_resets_next_turn` (mutation-proof: assert reset at the `_current_turn_id`-set boundary; neutering the reset must fail this test); `test_announce_reroute_gate_off_suppresses`.
**Step 2:** FAIL (no reset).
**Step 3:** GREEN — add `agent._last_reroute_announced = None` at `agent/turn_context.py:~211` (where `_current_turn_id` is set). NOT at agent_runtime_helpers.py:1361.
**Step 4:** PASS.
**Step 5:** Commit `feat(agent): per-turn reroute dedup reset at turn start (P4/BL-P4-1)`.
**SMOKE:** `pytest tests/agent/test_reroute_announce.py -v` → all pass, + the Phase-0 probe log showing a real reroute.

---

## FINAL — Config, deploy, live verify

### Task F.1: config.yaml.example knobs
**Step 1:** Document `model.announce_switch: true` and `model.announce_reroute: true` in `cli-config.yaml.example` (default-on behavioral settings — config.yaml, never env, per AGENTS.md).
**Step 5:** Commit `docs(config): announce_switch + announce_reroute knobs`.

### Task F.2: INV-PROV comments (Momus pass-3 belt)
**Step 1:** Add `# INV-PROV (C7): no inline credential material — see SPEC §C7` at the 3 sites: `parse_model_flags`, `_parse_inline_provider_model`'s `://` reject (model_switch.py:756), credential-sourcing block (model_switch.py:1257-1337).
**Step 5:** Commit `docs(model-switch): INV-PROV three-site contract comments`.

### Task F.3: Full suite + PR
**Step 1:** `pytest tests/gateway/ tests/agent/ -k "footer or switch_announce or override_persistence or reroute" -v` → all green.
**Step 2:** Open PR to `Kyzcreig/hermes-agent` fork/main per `hermes-fork-pr-contribution`. NEVER push to main directly.
**Step 3:** Greptile/CI green → merge.

### Task F.4: Deploy + staged live proof (LIVE — Ace present)
**Step 1:** `~/.hermes/fleet/deploy.sh` + safe-gateway-restart (per `safe-gateway-restart`).
**Step 2:** Live-verify, staged so Ace sees it: (a) `/reasoning high` → next footer shows `r:high` + a `🔀 Reasoning` line posts; (b) restart the gateway → `/reasoning high` override survives (footer still `r:high`); (c) the Phase-0 reroute proof (if P4 shipped) shows a real `🔀 Model rerouted` line on a flagged turn.
**Step 3:** Report the staged evidence to Ace.

---

## Notes
- **Blast radius:** shared gateway code, all fleet profiles. P1 display-only (post-cache). P4 is post-response read-only. No prompt-cache/routing behavior change.
- **Order:** P1 first (the reported bug). P2, P3a, P3b, P4 independent — can serialize or parallelize by file scope (P4 is agent-side, disjoint from P1/P2/P3 gateway-side).
- **Gotcha:** the `terminal`/bytecode-cache mutation footgun (prd-plan) — clear `__pycache__` if a "restored but still red" mystery appears when mutation-testing the C7/gate constants.
