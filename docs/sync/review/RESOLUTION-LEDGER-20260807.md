# Parity Merge Resolution Ledger — 2026-08-07

**Worktree:** `/Users/alexgierczyk/parity-merge-20260807` (branch `parity-merge-20260807`, off fork/main).
**Base (fork/main):** ee2fce287653d8ed13b1244d1cf424e8985852a6
**FROZEN upstream target:** 1e5b50744094959db5536eca9df3881d13fd28d8 (`/tmp/parity-upstream-sha.txt`)
**Merge staged:** `git merge --no-commit --no-ff <target>` → 238 conflicts (225 UU, 5 AA, 6 DU, 2 UD), 4643 clean-merged, 609 hunks.
**merge.conflictStyle:** zdiff3 (set in this worktree).

## Topology (ground-truthed 2026-08-07, fetched fresh)
fork BEHIND upstream by 3466 (to ingest); AHEAD by 709 (must survive). merge-base a7a696ba = 2026-07-24.
(Brief prose said "692 behind/3393 ahead" — labels inverted + stale. Apollo trial comment 709/3463 matches; gh api confirms ahead_by:3466 behind_by:709 diverged.)

## RELAY CONTRACT — read this first, every run
This is an oversized sync (per `upstream-parity-merge`/`references/oversized-sync-delegation.md`), grinding across multiple worker runs on the SAME task id (crash/ceiling → dispatcher requeue → new run continues).
1. Re-orient: `cd /Users/alexgierczyk/parity-merge-20260807 && git status --porcelain | grep -cE '^(UU|AA|DU|UD)'` for remaining conflicts. Read this ledger's DONE list. Do NOT redo resolved files.
2. Resolve mechanical→semantic. `git add` each file the moment it is marker-free + py_compile-clean (staging persists across process death = durable progress).
3. After EVERY resolved .py: `py_compile` + grep the resolved symbols for the auto-merge DUPLICATE trap (non-adjacent same-region edits kept BOTH outside markers → 0 markers but broken).
4. Append to the DONE list below (file · choice-legend · why · residual risk). Legend: U=union, UP=took-upstream, F=took-fork, B=hybrid/both-survive, TEST=test-contract-as-spec.
5. If YOU near ceiling: stop clean, ensure ledger current, report honestly. Do NOT commit the merge. Do NOT run finish/deploy. Do NOT touch ~/.hermes/runtime/hermes-agent (live gateways import it).
6. Checkpoint a kanban_comment every ~50 files.

## PRESERVE-LIST (fork side MUST survive — non-negotiable, from Apollo comments)
1. hermes_cli/model_switch.py — inline provider:model parsing + `_AGGREGATOR_VENDOR_NAMESPACES` (~974-1065) + its docstring paragraph. Upstream designs AGAINST this (their docstring: "--provider flag exclusively; no colon-based syntax"; upstream PR #34293 withdrawn). NEVER take theirs on this file without re-grafting the feature.
2. hermes_cli/kanban.py — `_current_session_id`-via-`get_session_env` contract + `model_override` create arg (restored by #475 after the 11cffc4d50 clobber — do not re-clobber).
3. kanban.cli_auto_subscribe knob in config.py — fork home; upstream has it in config_defaults.py. Fold to wherever merge lands config structure, but the KNOB must survive.
4. gateway/runtime_footer.py — fork now matches upstream #80661 shape (provider_model/context_full/reasoning + latency) → prefer the UNION.

## POST-MERGE (after all conflicts resolved, before push)
Revert fork PR #470's #80564 fork-adaptations to upstream form per the parity NOTEs in source:
- config_defaults.py home (undo the fork's config_defaults extraction adaptation)
- add_notify_sub full params (chat_type + delivery_metadata restored)
- the test's chat_type/delivery_metadata assertions restored
Prove every preserve-list item with grep + targeted test post-merge. Full suite green (hermetic — sandbox HOME, see skill). Per-file bisect any regression vs fork/main baseline.

## RESOLUTION RULES (per class)
- tests/ + locales/ : union-or-upstream (base_empty→union; theirs-only additions→take upstream). A conflict-marked TEST is a tripwire for a feature silently dropped in an adjacent AUTO-MERGED impl — verify the impl.
- agent/ + gateway/ + hermes_cli/ : read BOTH sides. fork-only features survive; upstream refactors (TurnRunner, config_defaults extraction) TAKEN with fork features re-grafted.
- AA files: `diff <(git show :2:$f) <(git show :3:$f)` — often fork feature upstream ingested+evolved; base on :3, re-apply real fork fixes.
- DU: upstream deleted/relocated a file fork modified → find where logic moved & port, or keep file if fork runs the capability in prod.
- Auto-merge DUPLICATE trap: after any signature/call/import/SQL hunk, grep the kept symbol & count.

## FULL TRIAGE (all 238 conflicts, classified — resolve in this order)

### BUCKET A — safe independent (RESOLVE FIRST, no god-file coupling)
- root/scripts/misc, 10 files. 6 DONE this run (see DONE list). Remaining 4 DEFER (god-coupled): pyproject.toml (py-modules hermes_state split — couples to hermes_state.py), cli.py (undo display_kind predicate — couples to undo/slash_commands), .github/workflows/ci.yml (slice_count 10 vs 12 + fork test_scope line — small, take upstream 12 + keep fork test_scope), website/docs/user-guide/configuration.md (hygiene/verify_on_stop docs — union fork tip+resume_interrupted_turns with upstream's expanded hygiene+verify_on_stop blocks).

### BUCKET B — locales (16 files, 32h) — DEFER until slash_commands.py + cli.py undo resolved
Each locale: 79 fork-only keys + 21 upstream-only keys (UNION both in), + value-collisions on 5 keys (4 in non-en). Resolution METHOD (YAML-aware, NOT text-union — dup keys = invalid YAML + fails tests/agent/test_i18n.py key-parity):
  - Build merged dict = base-order upstream keys, add all 79 fork-only keys, for the 5 collisions apply the decision below.
  - COLLISION DECISIONS (verified against test contracts):
    * gateway.reasoning.status + gateway.reasoning.unknown_arg → TAKE FORK (no "ultra"). tests/agent/test_i18n.py::test_reasoning_help_advertises_max_but_not_ultra asserts "ultra" NOT in status/unknown. Upstream added ultra → would fail fork's test. VERIFY that test survived the merge in tests/agent/test_i18n.py (it's auto-merged, status M) before finalizing.
    * gateway.status.tokens → TAKE UPSTREAM ("Lifetime tokens billed... use /context"). Preserve-list #4: adopt upstream runtime_footer #80661 shape.
    * gateway.stop.stopped → couples to fork stop-handling (graceful "finishing current step"). Resolve AFTER gateway/run.py stop path; likely FORK.
    * gateway.undo.removed → placeholder sets DIFFER (upstream adds {preview}). Couples to cli.py/slash_commands.py undo call site (upstream passes preview=, fork doesn't). Resolve AFTER cli.py undo. test_catalog_placeholders_match_english asserts en-vs-each-locale placeholder parity — whichever side wins, ALL 16 locales must match en's placeholder set for that key.

### BUCKET C — DU/UD/AA specials (semantic, NOT blind-pickable)
- mem0 subsystem (DU: plugins/memory/mem0/_setup.py + tests/plugins/memory/test_mem0_{backend,providers,setup,v3}.py; UU: plugins/memory/mem0/__init__.py): FORK REPLACED upstream's mem0 with its own architecture (fork has plugins/memory/mem0/test_capture_*.py, test_qmd_*, test_gbrain_* — none in upstream; fork __init__.py = 108 def/setup symbols). Upstream's _setup.py + its 4 tests are for upstream's DESIGN. LIKELY resolution: honor fork deletion (`git rm` the upstream files) IF fork __init__.py doesn't import from _setup — VERIFY `grep -rn '_setup' plugins/memory/mem0/` on merged tree first. Then resolve __init__.py UU by hand. HIGH STAKES (live fleet memory) — do NOT rush.
- tests/run_agent/test_run_agent.py (DU): upstream kept+modified (fc05247be8 'preserve session history when turn crashes'); fork deleted. Check if fork's run_agent still needs this coverage / fork replaced it elsewhere.
- UD: apps/desktop/src/app/session/hooks/use-preview-routing.test.tsx + tests/run_agent/test_real_interrupt_subagent.py — FORK deleted, upstream absent in HEAD tree but modified in merge. Verify the feature-under-test still exists in merged prod; if fork deliberately removed the feature, honor deletion (`git rm`).
- AA (both added same path): apps/desktop/src/lib/reasoning-effort.{ts,test.ts}, tests/test_log_isolation.py, web/src/lib/gatewayClient.test.ts, contributors/emails/liruixinch@outlook.com. Method: `diff <(git show :2:$f) <(git show :3:$f)`; often fork feature upstream ingested+evolved → base on :3 re-apply fork fixes. contributors/emails/* → take either (identical mapping file) or union.

### BUCKET D — GOD-FILES (117h, ONE worker each, LAST, fresh context)
- gateway/run.py (49h): 10 base_empty (union), 3 ours_empty, 1 theirs_empty, rest both-semantic. Highest risk. PRESERVE fork gateway features.
- hermes_state.py (30h): 9 base_empty, 9 theirs_empty (upstream added), 0 ours_empty. Upstream SPLIT hermes_state into hermes_state_common/portability/schema/search (see pyproject.toml theirs) — MONOLITH-SPLIT migration; re-home fork's hermes_state_ext deltas. Couples to pyproject.toml + tests/test_hermes_state.py.
- tui_gateway/server.py (20h): 5 base_empty, 8 theirs_empty, 1 ours_empty.
- hermes_cli/web_server.py (8h): all both-semantic (0 empty) — careful.
- run_agent.py (7h), gateway/slash_commands.py (3h — but couples to locales B + cli.py undo; resolve BEFORE locales).

### BUCKET E — agent/ (46h,17f), gateway/ (10h,3f incl runtime_footer PRESERVE-LIST #4 UNION), hermes_cli/ (20h,12f incl model_switch.py PRESERVE-LIST #1, kanban.py PRESERVE-LIST #2, config.py PRESERVE-LIST #3), tools/ (34h,8f), cron/ (18h,3f). Read BOTH sides; fork features survive; upstream refactors taken + fork re-grafted.

### BUCKET F — tests (116f, 238h) + apps/desktop TS (32f, 71h) + web/website. Mostly union-or-take-upstream, but a conflict-marked TEST is a tripwire for a feature silently dropped in an adjacent AUTO-MERGED impl (verify impl). Desktop TS: run `npm install && npx tsc --noEmit && npx vitest run` in apps/desktop as its own lane BEFORE finish (tsc catches marker-free merge bugs). Resolve tests AFTER their impl module so the test contract can be verified green.

## DONE (resolved + staged)
- cli-config.yaml.example  [UP]  base-empty upstream-only compression-notice doc block · none
- scripts/run_tests.sh  [U]  both base-empty env-forward additions (TMPDIR/HERMES_TEST_WORKERS/SESSION_LIST_REAL_COPY_DB fork + PYTHONUTF8/HERMES_E2E_BROWSER upstream) unioned · none
- scripts/release.py  [U]  base-empty AUTHOR_MAP: fork Kyzcreig orchestrator identities (fork-only, gate-critical) + upstream contributor entries unioned · none
- optional-skills/security/1password/SKILL.md  [F]  fork's richer fleet-service-account description kept over upstream's generic one · none
- website/static/api/model-catalog.json  [UP]  generated artifact; took upstream (newer 2026-08-03 catalog + opus-5-fast entry) · fork regenerates on next catalog build
- scripts/run_tests_parallel.py  [B]  h1 HYBRID (fork try/except + PYTHONDONTWRITEBYTECODE env + upstream encoding=utf-8/errors=replace); h2 fork noop_exit5-tagging comment (code below is fork's mechanism) · py_compile OK
- contributors/emails/liruixinch@outlook.com  [UP]  upstream superset (fork line `HexLab98` + added `# PR #71205 salvage` comment) · none
- .github/workflows/ci.yml  [UP+F]  run48: upstream slice_count 12 (fork had 10) + KEEP fork test_scope line (tests.yml declares both inputs, verified) · none
- website/docs/user-guide/configuration.md  [U]  run48: 3 hunks all in compression/agent docs — unioned fork hygiene_timeout=null/hygiene_failure_alert_after/resume_interrupted_turns tip + upstream hygiene_total_ceiling/context_timeout/context_total_ceiling/escalating-ladder/Verify-on-Stop. Config example knob-set now matches the union prose. yaml block validated · none
- hermes_cli/model_switch.py  [B]  run48 PRESERVE-LIST#1: base-empty conflict, both sides add non-overlapping code. KEPT fork _AGGREGATOR_VENDOR_NAMESPACES + _parse_inline_provider_model + _user_provider_lists_model + _inline_provider_matches_exact_id + colon-syntax docstring (the daily-use inline provider:model feature) AND added upstream resolve_display_context_length_async. Single defs each (no dup trap), py_compile OK · none
- hermes_cli/kanban.py  [UP-docstring]  run48 PRESERVE-LIST#2+#3: only conflict was _check_dispatcher_presence docstring. Took upstream docstring (describes the resolve_gateway_liveness ladder that the auto-merged body now uses); resolve_gateway_liveness confirmed present in merged gateway/status.py:1253 (fork's #475 get_running_pid revert-NOTE was 'until next parity merge' = now, so stale). Added a fork-parity NOTE recording the re-adoption. VERIFIED auto-merged body preserved: get_session_env stale-safe contract in _maybe_cli_auto_subscribe (#475 restore intact), model_override create arg wired (kanban.py:1569), cli_auto_subscribe knob read (1516, PRESERVE#3). py_compile OK · none
- gateway/runtime_footer.py  [B]  run48 PRESERVE-LIST#4: fork is the SUPERSET (has upstream #80661 provider_model/context_full/reasoning/latency PLUS fork-own messages/message_count/message_limit). 5 hunks: docstring→upstream ordering; all 4 param/field/call hunks→FORK superset (upstream's latency-only subset is contained). Field handlers unique, message_count param in exactly 2 funcs (no dup trap), py_compile OK · none

### run49 batch (RECONSTRUCTED from tree state by run60 — run49 crashed before writing these)
run49 crashed on a provider 429/503 storm mid-`hermes_cli/config.py`. Its resolutions ARE staged and
durable; the ledger entries were never written. run60 re-verified each by direct inspection:
- locales/*.yaml (16 files: af de en es fr ga hu it ja ko pt ru tr uk zh zh-hant)  [U + UP-collisions]
  YAML-aware key union. VERIFIED by run60: all 17 locales (incl. ar.yaml, parity-filled) load clean and
  carry EXACTLY 430 keys with ZERO missing / ZERO extra vs en.yaml. `ultra` present 2x in every locale
  (status + unknown_arg) per the APOLLO ULTRA RULING (2026-08-07 10:18, supersedes the 03:30 option-a).
- tests/agent/test_i18n.py (auto-merged, edited)  [TEST-contract-change]
  `test_reasoning_help_advertises_max_but_not_ultra` → RENAMED `test_reasoning_help_advertises_ultra`
  with INVERTED assertions (`"xhigh|max|ultra|reset" in status`, `"high, xhigh, max, ultra" in unknown`).
  Owner-ordered contract change (Ace 2026-08-07 "bring ourselves to parity by adding ultra in"), NOT a
  test weakened to pass a merge — history recorded in the test's own docstring.
- apps/desktop/src/lib/reasoning-effort.ts + .test.ts  [UP wholesale]  AA-special, per the ultra ruling:
  full upstream API (REASONING_EFFORT_VALUES / reasoningEffortLabel / resolveReasoningEffort /
  isThinkingEnabled / SHORT_LABELS / DEFAULT_REASONING_EFFORT) INCLUDING `ultra`. Not stripped.
- hermes_state.py + hermes_state_common.py + hermes_state_schema.py + hermes_state_portability.py  [B]
  The 30-hunk MONOLITH-SPLIT migration. Upstream split hermes_state into common/portability/schema/
  search; fork deltas (trigram-disable config, effective_last_active backfill, fork columns) re-homed
  into the mixins. run49 heartbeat recorded a green SessionDB smoke (create/append/get/pin/read/counts/
  list_sessions_rich/search/recents) before staging.
- pyproject.toml  [UP]  py-modules list grew for the hermes_state split (coupled to the above).
- cli.py + gateway/slash_commands.py  [B]  undo display_kind predicate + the `{preview}` placeholder
  call site — the coupling Bucket B named as the gate on locales. Both resolved, unblocking locales.
- tests/conftest.py  [U]  unioned fixtures (gate on the whole test lane's importability).
- .github/workflows/ci.yml, website/docs/user-guide/configuration.md — already logged in run48.
run60 independent verification of the whole batch: `git ls-files -u` = 203 unmerged (238-35 ✓);
marker audit across all 4654 truly-staged files = ZERO markers; ast.parse clean on every resolved .py.

### run60 batch (24 source files — all verified marker-free + ast.parse clean AT RUN END)
Method note: this run used `/tmp/p60_resolve.py` (block parser + `resolve_all` + `verify` +
`dupscan` + `check_and_stage`) instead of hand byte-splices — run48's replay-artifact problem
came from long manual splices. Every file below was re-verified by re-reading the tree at run end.
- agent/agent_init.py  [U]  base-empty; fork `_persist_superseded`/`_suppress_user_turn_persist`
  + upstream `_hard_interrupt_requested` Event. All 3 attrs are READ elsewhere (run_agent:3390/3520,
  conversation_compression:2536/3180) — dropping either side would NameError a live path. `threading`
  already imported (:26).
- agent/anthropic_adapter.py  [B]  fork's commented non-whitespace-placeholder `result.insert` (the
  2026-07-25 deterministic-400 incident note) + upstream's TWO NEW functions
  `_fix_blank_text_blocks_in_list` / `_scrub_blank_text_blocks`. Upstream's block re-stated the same
  insert; dropped that duplicate (would have been 2 inserts). Both new fns are CALLED (:2334, :2830).
- agent/background_review.py  [U]  fork mem0-write clause + upstream `focus` steering; independent.
- agent/error_classifier.py  [U]  TWO DIFFERENT 400 guards, both real, non-overlapping:
  fork `_MALFORMED_CONVERSATION_PATTERNS` → malformed_conversation, should_fallback=False (structural,
  re-sending to every provider is pointless) and upstream `_INVALID_MESSAGE_BODY_PATTERNS` →
  format_error, should_fallback=True (empty-content stub). Fork's guard kept FIRST (narrower).
  Had to close fork's `result_fn(` inline since the trailing `)` after the marker belonged to one call.
- agent/redact.py  [F + NOTE]  PARALLEL INVENTION of the same ReDoS fix. MEASURED head-to-head this
  run (venv 3.11.15, adversarial dotted run failing at the end):
      n=      500      1000      2000      4000
      fork    0.00006  0.00012   0.00024   0.00047   -> LINEAR (2x/doubling)
      upstr   0.01121  0.04455   0.17458   0.69633   -> QUADRATIC (4x/doubling)
  Fork's lookbehind-anchored form is a better COMPLEXITY CLASS (~1480x at 4 KB): possessive
  quantifiers stop backtracking but not the per-offset RESTART cost. Took fork + wrote the measurement
  into the source as a NOTE so a future sync can't "simplify" it back without re-running the curve.
- agent/turn_context.py  [B — RE-GRAFT]  ⚠️ bidirectional feature-drop. Fork RELOCATED
  `messages.append(user_msg)` earlier (platform_message_id stamping); upstream added display_kind/
  display_metadata stamping at the OLD site. Taking upstream verbatim would have appended the user
  message TWICE. Resolved to fork's structure and RE-GRAFTED upstream's stamping onto the relocated
  site. Params were already wired (turn_context:373-376, run_agent:8289, conversation_loop:1474) and
  tests/agent/test_synthetic_turn_display_kind.py is the contract — a plain "take fork" would have
  silently dropped a feature whose test still exists. `messages.append(user_msg)` count = 1.
- agent/turn_finalizer.py  [B]  fork's blackbox per-turn telemetry fold kept; its FIRST line was the
  old `from hermes_cli.plugins import invoke_hook` — re-homed to upstream's `hermes_cli.lifecycle`
  (the module exists and wraps plugins + observability). 0 remaining plugins-import call sites here.
- agent/usage_pricing.py  [U]  fork's metered-xAI route + upstream's GENERALIZED google route.
  Verified upstream is correct to take: `_OFFICIAL_DOCS_PRICING` keys on `("google", …)`, so fork's
  `provider="gemini"` return would have missed the table entirely.
- agent/system_prompt.py  [UP + RE-GRAFT]  upstream RELOCATED the skills index stable→volatile band
  (prefix-cache locality, auto-merged at :551). Keeping fork's `stable_parts.append(skills_prompt)`
  would have emitted the index TWICE. Took the removal, re-grafted fork's `_skills_prompt_text`
  telemetry stash (consumed by conversation_loop._resolve_skills_prompt_text + test_system_prompt_restore).
- agent/skill_utils.py  [B]  fork QUEUE_NOTE_PREFIXES/is_queue_note_name + upstream ORG_* mirror API,
  unioned; def line took UPSTREAM's `*, root: Optional[Path] = None` (callers already pass root=,
  tools/skills_hub.py:3377/3392/3702). ⚠️ fork's body ended `return is_skill_support_path(path)` —
  threaded `root=root` through or every relative-path caller would silently mis-resolve. Live smoke:
  queue-note True/True/False, ORG consts present, sig carries root=, excluded(pending)=True.
- hermes_cli/auth.py  [UP + RE-GRAFT]  upstream's #74339 source-aware write-back SUPERSEDES fork's
  `write_through_to_root = not _profile_has_own_xai_oauth_state(...)`. The auto-merge had already
  taken upstream's `_load_provider_state_with_source`, so fork's branch referenced an UNASSIGNED
  variable (NameError) and its unconditional save creates exactly the shadowing key #74339 removes.
  Took upstream + re-grafted fork's `state.pop("last_auth_error")` stale-error clear. 0 refs left.
- hermes_cli/banner.py  [U]  fork "head": head_sha field + upstream encoding="utf-8" kwarg.
- hermes_cli/commands.py  [U]  `_SLACK_VIA_HERMES_ONLY` is a 50-native-slash CAP BUDGET; both sides
  demoted different commands off the native list. Unioned (upstream 10 + fork boomerang/merge = 12);
  both fork commands exist as CommandDefs (:133,:135). Dropping either side re-clamps a command.
- hermes_cli/model_catalog.py  [U]  `import os` (fork) + `import threading` (upstream).
- hermes_cli/providers.py  [U]  base-empty; two independent new functions.
- hermes_cli/tools_config.py  [U]  `_DEFAULT_OFF_TOOLSETS` fork "moa" + upstream "a2a" unioned;
  upstream's `_CONFIG_ONLY_TOOLSETS` block kept.
- tools/send_message_tool.py  [B]  fork WRAPPED the home-channel fallback in the origin-routing guard
  (SECURITY: a bare send inside a messaging turn must go to the turn's channel, not global home —
  the v2 leak). Upstream only swapped raw json.dumps for `tool_error()`. Kept fork's structure,
  adopted upstream's helper. `used_origin_channel` + `_SEND_TARGET_*` intact.
- tools/skill_usage.py  [U]  docstring only; fork's is_shared_curatable_path chokepoint prose (live at
  :438/:487/:1115) + upstream's org-shared-skill prose. Both true of the merged code.
- agent/agent_runtime_helpers.py  [B]  h1: fork's tri-state `_recovered_after_swap` (keyless-client
  bug) kept, upstream's lazy-%s log line adopted. h2: base-empty, fork's fallback-announce reset +
  aux-routing re-sync UNIONed with upstream's `_rate_limit_backoff_count = 0`.
- agent/auxiliary_client.py  [B]  h1: took upstream's `on_stream_event` aux-progress hook BUT kept
  fork's route-scoped `_client` receiver (with_options timeout/max_retries=0 — the compaction
  30-min-wedge fix at :1873). Using upstream's `self._client` verbatim would have silently reverted it.
  h2: base-empty union of two independent helper groups.
- gateway/session_context.py  [U]  h1 docstring union (gateway-concurrency guard + delegated-child
  guard are BOTH live); h2 base-empty union of two independent module-level API groups.
- cron/lifecycle_guard.py  [B]  h1 union of regexes. h2: fork REPLACED the single `search()` with a
  per-match loop carrying ssh-remote + quoted-data exemptions; upstream normalizes shell line
  continuations first (#62891). Merged: run FORK's loop over UPSTREAM's `normalized` text (rewired
  both helper call sites). BEHAVIORAL PROOF (module loaded by path — `cron/__init__` still imports the
  unresolved jobs.py): plain restart BLOCK ✓, ssh-remote ALLOW ✓, ssh-localhost BLOCK ✓, quoted-data
  ALLOW ✓, line-continuation launchctl BLOCK ✓ — 0 failures, both sides' guards live in one function.
- hermes_cli/kanban_db.py  [B]  h1 base-empty union (fork's sticky-blocked event + upstream's
  `_inherit_notify_subs`). h2: fork's model_override parsing + spawn audit log kept; appended
  upstream's independent `--reasoning` branch at function level (verified NOT nested under the
  model_override `if`, so depth applies with or without an override).
- plugins/platforms/discord/adapter.py  [B]  h1 base-empty union. h2: upstream introduced the
  `seeded_extra` + `_skip_env_bridge` config-authority pattern; RE-HOMED fork's reaction_journal
  bridge onto that same pattern rather than leaving it the one line still writing os.environ blind.

run60 end-state: 179 unmerged / 4678 truly-staged / ZERO markers across the staged set.

### run60 batch 2 (11 more files) — see also the #470 REVERT section below
- hermes_cli/config.py + hermes_cli/config_defaults.py  [UP + PORT]  PRESERVE-LIST#3. Upstream
  EXTRACTED DEFAULT_CONFIG/OPTIONAL_ENV_VARS into config_defaults.py, so "take upstream" alone would
  have deleted every fork knob. AST-diffed fork-vs-extracted and ported 22 fork-only knobs (each WITH
  its comment block; 3 needed new sections: session_reset, session_store, dashboard.session_sync).
  ⚠️ ALSO caught 3 fork VALUE overrides on PRE-EXISTING keys that a fork-new-KEYS sweep misses
  entirely — the extraction had silently reverted them to upstream defaults:
  agent.restart_drain_timeout 0→180, compression.hygiene_hard_message_limit 5000→400,
  compression.hygiene_timeout_seconds 30→None. Each carries an inline `# fork parity` marker.
  NOT ported (verified deliberate upstream removals): checkpoints.delete_orphans (never read from
  config — cli.py:2207 + gateway/run.py hardcode delete_orphans=False by design) and
  display.tool_progress_overrides (deprecated; config_migrations.py:238 migrates it to
  display.platforms; config_defaults.py:1239 documents the removal).
  PROOF: 25/25 knobs read back through the public `cfg_get` surface, 0 failures.
- hermes_cli/models.py  [B COMPOSED]  fork `_canonical_model_key` (strip the provider's OWN slug) and
  upstream `_model_dedup_key` (fold picker-search aliases) are ORTHOGONAL dedup axes, each with a live
  contract test. Composed rather than picked: strip own-slug, then alias-fold. PROOF: 5/5 assertions
  from tests/hermes_cli/test_provider_models_namespace_dedup.py pass and `kimi/k3`→`kimi-k3`.
- tools/approval.py + tests/tools/test_request_tool_approval.py  [CONVERGE]  parallel fix of the SAME
  cron auto-approve hole; fork `_is_cron_session` and upstream `_is_cron_approval_context` are BOTH
  ContextVar-first, so either closes it. Converged on UPSTREAM's name (the auto-merged sibling test
  tests/tools/test_cron_approval_mode.py asserts it directly), grafted fork's loud-logging fallback,
  and DELETED fork's now-unused twin (two near-identical gates is a foot-gun). Updated the one test
  that patched the removed name. PROOF: both files 40 passed / 0 failed, hermetic sandbox HOME.
- tools/environments/base.py  [UP + DUP-TRAP FIX]  parallel mktemp invention, but upstream's export
  filter is a strict SECURITY superset: fork pipes `export -p | grep -vE` (LINE-based) while upstream
  UNSETS the bridged vars in a subshell BEFORE `export -p`. Upstream closes #71296 that fork still
  has — bash 3.2 renders a newline-containing value as a multi-line `declare -x NAME="…` block, so a
  line filter strips only the opener and the continuation lines land in the snapshot and EXECUTE on
  the next `source`. Took upstream on all 4 hunks. ⚠️ THEN found an AUTO-MERGE DUPLICATE TRAP:
  `parts.append(_snap_tmp_assign)` survived OUTSIDE any marker (fork added it ADJACENT to the hunk) →
  NameError on every snapshot write, with zero markers and clean py_compile. Removed it + the orphaned
  `_SNAPSHOT_EXCLUDE_PATTERN{,_Q}`. PROOF: 7/7 checks incl. the emitted unset-then-dump snippet.
- agent/conversation_compression.py  [B]  ⚠️ near-miss DOUBLE-CREATE: upstream replaced fork's separate
  `create_session` with the ATOMIC `publish_compression_child` (parent close + child row + handoff in
  ONE txn); defaulting to fork would have run an atomic publish AND a redundant second INSERT. Removed
  the follow-up create and RE-HOMED fork's orphan-rollback (#33906/#33907/#44794) onto the atomic call.
  Also spliced fork's TypeError signature-fallback inside upstream's new cancellation/commit-fence
  block, and repaired a truncated tuple literal the two conflict regions straddled (a SyntaxError a
  marker-only check would have missed).
- agent/model_metadata.py  [B]  upstream's per-message memo cached a SCALAR; fork's CJK dense/sparse
  estimator requires summing sparse CHARS across the whole list before ONE ceil(). Adapted the memo to
  cache the (dense, sparse, image) TUPLE — same key + soundness argument, additivity preserved. Also
  kept `max_tokens` OUT of upstream's context-window key list (on an Anthropic-style passthrough it is
  max OUTPUT tokens → collapses the window → premature auto-compaction; fork documents this exclusion
  on the sibling branch). PROOF: 6/6 behavioral checks (1msg=10 / 50msg=486 not 500; cjk=109 vs
  ascii=38; memo returns a 3-tuple and hits on repeat; cached==uncached; images counted).
- gateway/session.py  [B]  took upstream's update_session refactor (early-return, peer-field snapshot
  under _lock to avoid a torn row, single-row `_save_entry` UPSERT) + re-grafted fork's had_any_turn
  latching and last_served_identity. h2/h3: fork's has_platform_message_id_answerable kept, upstream's
  widened `rewrite_transcript(active_only=…)` signature adopted and threaded to replace_messages.
- agent/conversation_loop.py  [U]  4 base-empty unions (fork MoA pricing + _return_interrupted;
  upstream copilot helpers + _ensure_cached_system_prompt_static + the persist_user_display_kind/
  metadata param and passthrough — the CALL-SITE half of the feature re-grafted into turn_context).
  Repaired a union seam that cut a `return (` expression in half (SyntaxError).
- hermes_cli/inventory.py  [U]  3 base-empty unions. Verified BOTH new params are consumed in the
  auto-merged body (`for_picker` :217, `apply_picker_prefs` :287) so neither could be dropped.
- cron/jobs.py  [UP]  same profile-scoping fix (#69377) invented on both sides; upstream reuses the
  `store` local already resolved at the top of the function (fork re-calls `_current_cron_store()`
  per line, so two writes in one tick could straddle a profile switch) and adds extra helpers.

## #470 FORK-ADAPTATION REVERT (mandate item 4) — 2 of 3 DONE
The merge lands upstream's full `add_notify_sub(… chat_type, delivery_metadata)` signature
(kanban_db.py:10199) and both schema columns (chat_type TEXT :1401, delivery_metadata TEXT :1405),
so #470's adaptations are stale. Reverted to upstream form:
- hermes_cli/kanban.py:2891 — NOTE retired; plain upstream-form call.
- tools/kanban_tools.py:1537 — NOTE retired; delivery_metadata enrichment reconnected.
- ⛔ tests/hermes_cli/test_kanban_cli_auto_subscribe.py — **BLOCKED, NOT DONE.** #470 stripped the
  `chat_type` / `delivery_metadata` assertions and left a NOTE saying to restore them at the parity
  merge. The profile's test-guard refuses edits to committed test files ("Committed tests are the
  contract — fix the implementation, not the test"). The guard is RIGHT to fire in general and I did
  not route around it. This is a legitimate exception (the test's own comment authorizes the
  restoration, and the merge supplies the columns) but it needs a human/orchestrator to amend.
  Exact edit required at ~line 106, replacing the fork-parity NOTE block with:
      assert sub["chat_type"] == "private"
      assert sub["delivery_metadata"]["telegram_reply_to_message_id"] == "42"
  (keep the trailing `assert "cli_auto_subscribe" in out`). Verify the fixture's session env supplies
  chat_type=private and message_id=42 before asserting those literals.

## ⚠️ OPEN MERGE REGRESSION — needs an OWNER DECISION (found run60 by test, not by conflict)

`tests/agent/test_curator_shared_scope.py::TestGateRewiring::test_background_write_guard_permits_in_scope_shared`
FAILS on the merged tree and PASSES on the fork/main baseline (ee2fce2876) — so by the doctrine's
binary classification this is a **MERGE REGRESSION, not a stale test**. It did NOT come from any
hand-resolution: the region is `tools/skill_manager_tool.py:397`, which git AUTO-MERGED (my 4
conflicts in that file were at ~1107/1143/1866/2142). Reproduced deterministically in isolation
(0.40s), so it is not cross-file pollution.

WHAT COLLIDED — two correct features, each with a live test:
- FORK (`is_shared_curatable_path`, the PRESERVE-adjacent chokepoint): a skill under
  `skills-shared/` with `curator.include_shared_dirs=true` IS curatable by the background
  reviewer. The fork-only test above asserts the guard returns None (permitted) for exactly that
  shape. Note the fixture never writes a usage record — under fork/main it didn't need one.
- UPSTREAM (#67140): the ownership guard used to key on `isinstance(usage_rec, dict)`, so a skill
  with NO usage record passed once, the write's own `bump_patch()` then created a
  `created_by: null` record, and the identical write was refused thereafter. Upstream tightened it
  to fail-closed on BOTH shapes (missing record and explicit null). Guarded by
  `tests/tools/test_skill_manager_tool.py::TestBackgroundOwnershipPolicyConsistency`
  (`test_repeated_identical_write_gets_the_same_answer`) — which is CLEAN (auto-merged, unconflicted)
  and currently PASSES.

Net: the merged guard now refuses the fork's in-scope shared skill because the fixture has no usage
record. Both tests cannot pass as written. This is a POLICY question (does shared-dir curatability
imply curator-managed?), not a merge mechanic, so it is deliberately NOT resolved here.

Candidate resolutions, for the owner to pick:
 (a) Treat in-scope shared as curator-managed: in `_background_review_write_guard`, allow when
     `is_shared_curatable_path(skill_dir)` is true even if the usage record is missing — i.e. move
     the shared check to short-circuit BEFORE the curator-managed gate. Keeps both #67140 (still
     fail-closed for LOCAL skills, which is the shape that bug was about) and the fork feature.
     This is my recommendation, but it widens the autonomous write surface, so it needs a human.
 (b) Update the fork test's fixture to `hermes curator adopt` the skill (write a
     `created_by: "agent"` usage record) before asserting. Preserves upstream's policy verbatim and
     narrows the fork feature to adopted skills only.
 (c) Declare the fork's shared-curator feature superseded and delete the fork test — only if Ace
     confirms the shared-dir curation lane is no longer wanted.
DO NOT silently weaken `TestBackgroundOwnershipPolicyConsistency`; it encodes a real race fix.

## AA-SPECIALS ANALYSIS (verified this run — DEFER, coupled)
- apps/desktop/src/lib/reasoning-effort.ts + .test.ts: NOT mechanical. Upstream REWROTE the module — adds `ultra` level + new API (REASONING_EFFORT_VALUES, reasoningEffortLabel, resolveReasoningEffort, isThinkingEnabled, SHORT_LABELS, DEFAULT_REASONING_EFFORT). Fork API = ENABLED_REASONING_EFFORTS/isEnabledReasoningEffort, NO ultra. ⚠️ COHERENCE: fork BACKEND deliberately excludes ultra (tests/agent/test_i18n.py::test_reasoning_help_advertises_max_but_not_ultra). Taking upstream's desktop `ultra` desyncs frontend from backend. DECISION NEEDED (frontend follows backend): either (a) adopt upstream's richer API but DROP `ultra` from the arrays to match backend, or (b) confirm fork intends to add ultra backend-side too. Resolve WITH the reasoning-level backend decision (hermes_constants.py VALID_REASONING_EFFORTS), not in isolation. Grep consumers of both API shapes across apps/desktop before picking.
- web/src/lib/gatewayClient.test.ts: fork's dashboard-source-attribution contract (source:"dashboard" tagging, spies on JsonRpcGatewayClient.request) vs upstream's full FakeWebSocket + dashboard-auth-reload rewrite. Couples to web/src/lib/gatewayClient.ts (auto-merged, status M — VERIFY it kept fork's source-tagging). Resolve after confirming the impl side.
- tests/test_log_isolation.py: both sides guard the SAME hermeticity property (no test writes to real ~/.hermes/logs) with different impls. Upstream's version is more thorough (documents the module-scope setup_logging + HERMES_HOME-at-import problem). Couples to tests/conftest.py (conflicted). LIKELY take upstream (superset guard) after conftest resolved — but VERIFY fork's `_root_file_handler_paths` assertion is preserved or superseded.


---

## run68 batch — THE UPSTREAM TEST-PRUNE CLASS (78 files resolved: 167 → 89 conflicts)

### ★ THE FINDING THAT DEFINES THIS RUN
The single largest remaining conflict class was NOT semantic drift — it was upstream's own
**test-suite pruning**. Two upstream commits in the ingest range delete ~27k test functions:
- `6b81590c55` "test: prune low-value tests suite-wide (wave 1) — 46,820 → 28,106 test functions"
- `39975613b1` "test: prune wave 2 + speed fixes — 28,106 → 19,757" (1,172 files, −124,824 lines)

Where the fork had ADDED its own tests inside a region upstream deleted, git renders a conflict
whose **`theirs` side is EMPTY**. Measured: **176 such hunks across 101 files** — 91 of those files
are touched by exactly those two prune commits.

**This is a trap in BOTH directions, which is why it could not be batch-resolved either way:**
- blind `--theirs` (the "upstream deleted it, take the deletion" reflex) **silently deletes
  fork-authored tests**. Measured before acting: **226 fork-authored test functions sat inside
  upstream-deleted regions** across 46 files — including the fork's pool-exhaustion classifier
  guards, the kanban model_override contract, the trigram-config suite, and the fast-command
  contract tests.
- blind `--ours` **resurrects ~22.6k tests upstream deliberately pruned**, re-litigating an
  upstream decision inside a parity merge and re-inflating suite wall-time by ~2x.

### THE RULE APPLIED (union of INTENTS, decided per test function)
For every test name inside a theirs-empty hunk, the drop set is **triple-constrained** — each
constraint alone is unsafe:
1. the name appears INSIDE a theirs-empty (upstream-deleted) region — a name outside was
   auto-merged and is not ours to touch;
2. the name existed at the **MERGE BASE** `a7a696ba` — if absent, the fork authored it after the
   fork point and it MUST survive;
3. the name is **absent from upstream's version of the file** — proves upstream actually deleted
   it rather than RELOCATING it (this gate fired on 4 real cases where upstream moved a test).

Everything that is not a positively-named, provably-upstream-owned `def test_*` is kept verbatim
(imports, fixtures, helpers, class headers). Conservative by construction.

Tooling: `/tmp/p68_prune2.py` (AST-based). **Method note for the relay: a TEXT splitter is not
sufficient here and produced 7 real defects before being replaced.** Conflict regions routinely
straddle a `def` boundary — a region can open mid-docstring (its header auto-merged away above the
marker) or close immediately after a `def` line whose body continues below. Use `ast` spans
(`decorator_list` for the true start, `end_lineno` for the true end); fall back to text only for the
2 files whose fork side is unparseable mid-region, and verify those by name-set instead.

### VERIFICATION (measured at run end, not remembered)
- MERGE_HEAD = `1e5b50744094959db5536eca9df3881d13fd28d8` — intact.
- **89 unmerged / 4768 truly-staged.**
- MARKER AUDIT: 4768/4768 truly-staged files read byte-wise → **0 conflict markers**.
- SYNTAX: 2925 staged `.py` → **0 broken** (`ast.parse`).
- ★ **FORK-FEATURE LOSS AUDIT — the one that matters.** Every `def test_*` name in fork/main
  compared against the staged tree across all 2,901 staged test files:
  **fork-authored tests lost = 0.** Upstream-owned tests dropped = 22,630 (upstream's own prune,
  which is the intended ingest).
- SELF-AUDIT that caught my own damage: every SURVIVING test's body was compared byte-wise against
  both fork/main and upstream. 7 tests matched NEITHER side — each was a survivor that had
  ABSORBED a dropped neighbour's body (the text-splitter defect above). All 7 restored verbatim
  from the owning side; re-audit now reports **0**. This check is cheap and non-optional — a
  marker-free, ast-clean, green-looking file can still carry a silently merged test body.

### TEST EVIDENCE (hermetic: sandbox HOME + HERMES_HOME, `-p no:randomly`) — RE-RUN AT RUN END
`918 passed, 1 xpassed, 20 failed` over the 29-file resolved set.
Hermeticity PROVEN empirically, not assumed: `lsof ~/.hermes/state.db | grep -c python` = **0**
during the run (the export alone is not sufficient evidence — see the skill's round-2/3 warnings).

ATTRIBUTION BY DIFFERENTIAL, not by narrative. The SAME 29 files were run in a throwaway baseline
worktree at fork/main `ee2fce2876` and the failure sets compared by normalized nodeid:
- baseline fork/main: **61 failed / 2281 passed**
- merged tree:        **20 failed /  918 passed**
- **fail on BOTH → 18. These are PRE-EXISTING on fork/main, NOT merge damage.**
- **fail ONLY on the merged tree → 2**, both diagnosed below.

The 18 are cross-file state pollution, root-caused by bisection to a single polluter,
`tests/hermes_cli/test_kanban_cli_dispatch_passthrough.py`, which leaks provider/catalog module
state into later files sharing an interpreter. Each passes in isolation on BOTH trees. This is a
fork/main defect that predates the merge — it must NOT be charged to this sync, and it wants its
own card.

THE 2 MERGED-ONLY FAILURES:
1. `test_context_compressor.py::TestCalibration::test_calibrated_real_over_threshold_compacts`
   — a genuine STALE TEST caused by an upstream refactor. Fully root-caused below. BLOCKED on the
   test-guard.
2. `test_kanban_db.py::test_connect_works_when_wal_is_silently_refused` — an UPSTREAM-NEW test
   (absent from fork/main, so it cannot be "a regression"). It PASSES in isolation and the whole
   file is **62 passed / 0 failed** file-local. Same cross-file pollution class as the 18; it only
   shows up merged-only because the test does not exist on the baseline side to fail there. No
   action for this merge.

⚠️ CONCURRENCY NOTE FOR THE RELAY (bit this run): a CONCURRENT session resolved
`tools/async_delegation.py` in this shared worktree mid-verification (file mtime 16:26, unmerged
count moved 89 → 88 under me). 7 `tests/tools/test_kanban_tools.py` failures I had attributed to
that conflicted file evaporated when it landed — re-verified after: **30 passed / 0 failed**.
Lesson: re-read `git ls-files -u` immediately before publishing any conflict count or attribution;
this tree has more than one writer.

### ⛔ BLOCKED — needs a human to amend a committed test (2nd instance of the run60 test-guard case)
`tests/agent/test_context_compressor.py::TestCalibration::test_calibrated_real_over_threshold_compacts`
FAILS on the merged tree, PASSES on the fork/main baseline — but it is a **STALE TEST, not a
regression.** Root-caused to an upstream architectural change, measured not guessed:
- Upstream made `ContextCompressor.context_length` a **lazily-resolved property whose SETTER
  invalidates the derived budgets** (`self._threshold_tokens = None`) and re-derives
  `threshold_percent` for the new window. Baseline fork/main has no such property.
- The test sets `threshold_tokens = 750_000` and THEN `context_length = 1_000_000`, so the setter
  discards the 750k and recomputes 0.85 × 1M = **850k**. Verified live on both trees:
  merged → `threshold_tokens=850000`, `should_compress_calibrated(800k)=False`;
  baseline → `threshold_tokens=750000`, `=True`.
- The fix is a ONE-LINE ORDER SWAP (assign `context_length` BEFORE `threshold_tokens`) in 4 sibling
  tests of `TestCalibration` that share the pattern (`test_calibrated_motivating_644k_776k_defers`,
  `test_calibrated_real_over_threshold_compacts`, `test_no_ratchet_multi_turn`,
  `test_raw_ceiling_fires_regardless_of_skew`). The other 3 pass today only coincidentally.
- The profile's test-guard refuses edits to committed test files, and it is RIGHT to fire. I did
  not byte-splice past it. The test is fork-only (absent upstream), so no upstream contract is at
  stake — this is a pure test-ordering artifact of an upstream refactor.

### ★ ONE REAL MERGE REGRESSION FOUND AND FIXED (impl change, not a test change)
`tests/hermes_cli/test_model_switch_custom_providers.py::test_keyed_custom_provider_bare_custom_fallback_uses_stable_key`
(an UPSTREAM-NEW test) failed: `resolve_provider_full("custom", …)` returned `id="custom"` instead
of the stable `custom:local-127.0.0.1:11434`.
TWO CORRECT FEATURES COLLIDED, and only the ORDER was wrong:
- FORK added step **1b** to `hermes_cli/providers.py` — resolve provider-module plugins
  (`plugins/model-providers/<name>/`) so a plugin-only provider stops failing with
  "Unknown provider" on `/model`.
- UPSTREAM's step **2b** resolves saved `custom_providers:` to a stable `custom:<provider_key>` id.
The bundled `custom` plugin profile is a GENERIC STUB (empty `base_url`, aliases
`ollama/local/vllm/llamacpp/…`), so step 1b matched bare `"custom"` first and shadowed the user's
real configured provider, returning an unusable ProviderDef.
FIX (`hermes_cli/providers.py`): try `resolve_custom_provider` BEFORE the plugin lookup, keeping the
plugin lookup as the fallback it was written to be. Both features survive.
PROVED, not assumed: `custom` is the ONLY plugin-only provider in the tree (enumerated all 40+
`plugins/model-providers/*` against `get_provider`), so the reorder's blast radius is exactly the
intended case. With no `custom_providers` configured, bare `custom` and its aliases still fall
through to the plugin stub. **147 passed** across
`test_model_switch_custom_providers` + `test_upstage_provider` + `test_kimi_cn_provider_listing` +
`test_runtime_provider_resolution` + `test_model_validation` + `test_list_picker_providers`.

### FILES RESOLVED THIS RUN (78)
`tests/gateway/conftest.py` [U] — base-empty; fork's `_restore_os_environ_after_test` autouse
env-snapshot fixture UNIONed with upstream's session-scoped `_bind_lark_sdk_globals_when_installed`.
Independent; both fixtures are live. This file GATES the entire `tests/gateway/` lane (a conflicted
conftest is an import error for every file under it), which is why it was resolved first.

The other 77 are the prune class above, all `[TEST-PRUNE]`: fork-authored kept, upstream-owned
dropped, per the triple-constrained rule.

### REMAINING 89 conflicts / 321 hunks (next worker, cheapest-first)
- `apps/desktop/**` 33 files / 71 hunks — its own lane; needs `npm ci && npx tsc --noEmit && npx vitest run`.
- god-files: `gateway/run.py` 49h, `tui_gateway/server.py` 20h, `cron/scheduler.py` 12h,
  `agent/chat_completion_helpers.py` 9h, `agent/tool_executor.py` 9h, `hermes_cli/web_server.py` 8h,
  `tools/async_delegation.py` 8h, `run_agent.py` 7h, `tools/cronjob_tools.py` 6h, `tools/delegate_tool.py` 6h.
  **Resolve `tools/async_delegation.py` + `agent/chat_completion_helpers.py` EARLY — between them they
  block 10 of the 16 attributed test failures above.**
- tests: `tests/tools/` 39h, `tests/agent/` 24h, `tests/test_hermes_state.py` 11h, `tests/gateway/` 16h.
  NOTE: several of these still carry theirs-empty prune hunks MIXED with semantic hunks — reuse
  `/tmp/p68_prune2.py` for the prune hunks, then hand-resolve the rest.
- mem0 DU/UU subsystem (HIGH STAKES, live fleet memory) + the AA-specials, unchanged from run60.

DURABLE: worktree `/Users/alexgierczyk/parity-merge-20260807`, branch `parity-merge-20260807`,
MERGE_HEAD `1e5b5074`. NOT DONE — no commit, no PR. Do NOT merge.
Do NOT touch `~/.hermes/runtime/hermes-agent`.


---

## RUN 69 — RETRACTED SECTION (written by a TIMED-OUT worker, superseded)

**This section was written by the run69 worker AFTER its run had already been declared
`timed_out` by the dispatcher.** Its file-level resolution claims were WRONG: it attributed
`tools/async_delegation.py`, `agent/chat_completion_helpers.py`, `hermes_state.py`,
`run_agent.py` and `tools/delegate_tool.py` to itself, when those files were in fact resolved by
the LIVE run70 worker (kanban run id 70, pid 21387) working the same worktree concurrently. The
timed-out worker never resolved a conflict; its only tree mutation was this ledger file.

The authoritative record for that work is the **`## run69 batch — the ASYNC/PERSIST seams + the
gateway/run.py METHOD (92 → 79 conflicts)`** section below, written by the worker that actually
did it.

WHAT REMAINS VALID from the retracted pass (audits, independently re-runnable, no resolution claim):
- Full-tree fork-test-loss audit over **2363 staged test files**: every `def test_*` in fork/main
  HEAD diffed against the staged tree, with merge-base `a7a696ba` membership distinguishing
  fork-authored from upstream-owned. Result: **fork-authored tests lost = 0**; upstream-owned
  dropped = 22,707 (the intended prune). The single flagged name,
  `test_reasoning_help_advertises_max_but_not_ultra`, is NOT a loss — it is the owner-ordered ULTRA
  ruling, present at `tests/agent/test_i18n.py:126` renamed to `test_reasoning_help_advertises_ultra`.
- Marker audit: 4796/4796 truly-staged files byte-scanned -> 0 conflict markers.
  Syntax: 2936 staged `.py` `ast.parse`d -> 0 broken.
- Attribution method worth reusing: a residual test failure caused by a still-conflicted import
  shows as `SyntaxError: invalid decimal literal` (the `>>>>>>> <sha>` marker line parsing as a
  number). Parse the `File "..."` line preceding it to attribute a failure to its conflicted file
  rather than to your own edit.

## run69 batch — the ASYNC/PERSIST seams + the gateway/run.py METHOD (92 → 79 conflicts)

### FILES RESOLVED + STAGED THIS RUN (13)

- `tools/async_delegation.py`  [B]  8 hunks. ADOPTED upstream's stale-progress monitor
  (`_ensure_stale_monitor`/`_stale_monitor_loop`/`_finalize_stalled`, the #60203 wedged-runner fix)
  and its `_begin_finalization`/`_finish_finalization` split; RE-GRAFTED the fork's durable-terminal
  path (`recover_on_shutdown` gate + `_store.append_terminal` + `completion_queue.put`) into
  `_finalize`.
  ⚠️ **AUTO-MERGE NameError TRAP, ZERO MARKERS.** git placed the fork's
  `expected_attempt = attempt_id or record.get("attempt_id")` line INSIDE upstream's new
  `_begin_finalization(delegation_id)`, which has no `attempt_id` parameter → NameError on EVERY
  finalize. `py_compile` clean, marker-grep clean, would have failed only at runtime. Fixed by
  threading `attempt_id` through as a kw-only param and moving the durable branch back up into
  `_finalize` where its locals live.
  Hunk 5 (`list_async_delegations`) COMPOSED: upstream builds a list, fork builds a dict keyed by
  delegation_id so the durable-registry augmentation can merge onto live handles. Kept upstream's
  per-item computed fields + out-of-lock sampler pass, re-keyed to a dict so the fork's
  cancel-attribution merge still works. PROOF: 87 passed across 8 async-delegation test files
  (2 remaining failures both cite the still-conflicted gateway/run.py).

- `agent/chat_completion_helpers.py`  [B COMPOSED]  9 hunks. The load-bearing one is the 429
  cooldown, where a blind pick REDS one side's test either way:
    * FORK: `_primary_cooldown_seconds(error_context)` — honours the provider's `reset_at`
      (test_primary_runtime_restore asserts a 7200s reset_at yields >7100s, AND that a refusal
      arms exactly `_primary_cooldown_seconds(None)`==60 "not a literal").
    * UPSTREAM: exponential escalation on CONSECUTIVE 429s, 4h cap
      (test_24996 asserts 60/120/240 and a 14400 cap).
  COMPOSED: `base = _primary_cooldown_seconds(error_context)` becomes the BASE of the escalation,
  `min(base * 2**n, 14400)`. Both suites pass unchanged (96 passed with test_stream_surrogate_splicer
  + test_pool_affinity_header).
  Anthropic stream teardown: took upstream's `accumulator`/`output_modified`/`_close_managed_stream`
  restructure and re-applied the fork's `_repair_anthropic_message_surrogates` on BOTH return paths
  (base_final_message shortcut AND the accumulator path) — taking upstream verbatim silently drops
  surrogate repair on the native Anthropic final message.

- `run_agent.py` + `hermes_state.py`  [B — ADDITIVE API EXTENSION]  the hardest seam of the run.
  Upstream replaced the per-row `append_message` turn flush with an ATOMIC `append_messages_batch`
  (one BEGIN IMMEDIATE per turn instead of one per row). The fork DEPENDS on the per-row return
  value for two live features:
    1. `interrupt_close` re-persist (needs the row id to `update_message_finish_reason` later);
    2. `_db_persisted_row_id`, which `tui_gateway/server.py:7794` reads to stamp the desktop's
       OPTIMISTIC rows with committed ids — without it the live session-sync poll fails to
       recognise them and appends DUPLICATES (every message rendered twice).
  Neither side is pickable. Added an **additive opt-in `row_ids_out` out-param** to
  `append_messages_batch` + `_insert_message_rows` (capturing `cursor.lastrowid`), extended onto the
  caller's list ONLY after the transaction commits so a retried/rolled-back attempt leaves no
  phantom ids. Back-compat: omitting the param changes nothing.
  PROVED AGAINST A REAL SQLITE DB (not mocks): returned ids `[1,2,3]` == `SELECT id ... ORDER BY id`;
  chunked path (`chunk_rows=2`) returns all 5 ids in order; no-param call unaffected.
  ⚠️ **CAUGHT MY OWN DEFECT:** the first resolution left BOTH the theirs-side batch call and my
  replacement in place → `append_messages_batch.call_count == 2`, i.e. EVERY TURN PERSISTED TWICE.
  Marker-free and ast-clean; caught only because tests/run_agent/test_tool_name_db_persistence.py
  asserts `call_count == 1`. Relay lesson: when a hunk's `theirs` side already contains the call you
  are re-adding, grep the call count after resolving.
  Also re-grafted the fork's `trigger_reason=` kwarg into upstream's rewritten compression `_run`
  closure (upstream's restructure dropped it; `compress_context` still declares the param).

- `tools/delegate_tool.py`  [B]  took upstream's `_build_child_preserving_parent_tools` (a LOCKED
  save/restore around each child construction, replacing the fork's manual global save + try/finally)
  and re-grafted the 5 fork kwargs upstream's rewrite drops: `inherit_context`,
  `materialized_prefill_messages`, `recovery_max_spawn_depth`, `recovery_orchestrator_enabled`,
  `skills`. A plain take-theirs silently disables boomerang inherit_context and per-task skills.
  Also `delegated_child_context(str(child.session_id))` (upstream's new arg) kept together with the
  fork's `_clear_child_send_origin`/`_clear_child_cron_session` finally-cleanup.

- 6 test files [TEST/U]: test_credential_pool_routing (fork tri-state SwapOutcome + upstream's new
  `_credential_pool_entry_id`), test_system_prompt_restore + transports/test_chat_completions
  (base-empty unions of independent NEW classes), test_complete_path_at_filter (both sides neutralize
  the same dev-config leak by different levers — unioned, belt+braces),
  test_compression_session_id_persistence (fork REPLACED the class walker with a module-level
  `_visit_block`/`_descend` that recurses body/orelse/finalbody/handlers — a strict superset of the
  exact gap upstream's hunk patches), and tests/ci/test_classify_changes.py.
  `test_classify_changes`: the fork's "json fixture → python (fail-open)" case PREDATES upstream's
  new `python_prod` lane. Converged it to `python_prod=False` to AGREE with upstream's own sibling
  case ("tests-only → python without python_prod") rather than weakening either side. 120 passed.

- `docs/sync/review/tools/closure_to_method.py` — new, see below.

### ★ gateway/run.py — ANALYSIS COMPLETE, RESOLUTION NOT YET STAGED (still UU, 49 hunks)
Upstream did TWO structural refactors in this file. Both are mechanical, and knowing that is what
makes the file tractable:
  (a) **SessionState consolidation** — every per-session dict (`_running_agents`,
      `_session_model_overrides`, `_queued_events`, ...) moved into ONE `self._sessions:
      Dict[str, SessionState]` container. **CRITICAL: upstream kept `legacy_dict_property` shims**
      (gateway/session_state.py:419 + the `LEGACY_FIELD_SPECS` table), so fork call sites spelling
      `self._session_model_overrides[k]` STILL WORK against the new container. That means most
      SessionState hunks can take EITHER side. But the FORK-ONLY dicts have NO shim entry
      (`_running_agent_tasks`, `_draining_turns`, `_session_initiated_restart`, `_resumed_this_boot`,
      `_session_model_override_unavailable`) — their `__init__` initializers MUST survive.
  (b) **TurnRunner extraction** — the three giant closures moved out of `_run_agent_inner` into
      `class TurnRunner` (already auto-merged into the file at :4116).

TWO REAL DEFECTS FOUND (both were re-verified this run; the in-file fixes were reverted along with
the WIP by a `git checkout -m`, so the NEXT worker must re-apply them — they are NOT yet staged):
  1. **C1 safe-restart detection is ABSENT from upstream's extracted TurnRunner.progress_callback.**
     The fork's F2 self-completing-loop guard (observe the safe-restart skill's `terminal` call →
     set `_session_initiated_restart[session_key]`) exists ONLY on the fork side. It is guarded by a
     SOURCE-CONTRACT test: `tests/gateway/test_restart_cascade.py::
     test_c1_detection_present_in_real_progress_callback` asserts the LITERAL string
     `if _command_invokes_safe_restart(_cmd):` appears in gateway/run.py. Re-graft it into the method
     form immediately after the `if event_type not in {"tool.started",}: return` guard, rewritten as
     `ctx.session_key` / `self._runner._session_initiated_restart`.
  2. **Dead-alias NameError with zero markers.** Upstream RETIRED the `_`-prefixed re-export aliases
     in the `from agent.replay_cleanup import (...)` block (they now import the canonical names).
     The auto-merge took upstream's import block but left TWO fork call sites still spelling
     `_strip_stale_dangerous_confirmations(...)` (in the live-history fallback path, #59607 stale
     dangerous-confirmation expiry) → NameError at runtime on that path. Fix = rewrite both call
     sites to the canonical `strip_stale_dangerous_confirmations`. Verified the sibling aliases
     (`_strip_interrupted_tool_tails`, `_strip_dangling_tool_call_tail`, `_is_interrupted_tool_result`,
     `_is_dangerous_confirmation`) have ZERO remaining references, so this is the only instance.

  3. **METHOD for the 987-line run_sync hunk — TOOLED, reproducible from a clean checkout:**
     `docs/sync/review/tools/closure_to_method.py` (staged). It applies upstream's own mechanical
     extraction rewrite (closed-over local → `ctx.<field>` per gateway/turn_context.py; `self` →
     `self._runner`; `nonlocal` neutralized) to the FORK side and the MERGE BASE, then runs a real
     `git merge-file --diff3` of base-as-method / fork-as-method / upstream-method.
     **Result: 0 conflicts, 1475 lines, AST-clean** — re-verified by running the staged tool.
     Symbol audit of the product confirms every fork-only feature survives:
     `_build_resume_pending_message`, `_resume_summary_only`,
     `_clear_resume_summary_only_for_human_turn`, `_announce_reinit_recovery`,
     `_announce_and_persist_served_route`, `persist_user_platform_id`, `_startup_resume_modes`,
     `_suppress_user_turn_persist`, `_resume_reason_phrase`, `_pause_typing_before_finalize`,
     `_clarify_callback_sync` — alongside upstream's streaming-TTS (`_stts`) wiring.
     ⚠️ `StreamConsumerConfig` reads as "lost" on a naive symbol count — it is NOT: upstream
     RELOCATED it into `_build_stream_consumer_config` (:23822). Verify relocations before
     reporting a drop.
     ⚠️ **Do NOT write this transform with a regex.** A regex version rewrites keyword-argument
     names, attribute accesses and comment prose, manufacturing ~8 bogus conflicts; the tell is a
     conflict body containing `def f(ctx.message: str)`. Only rewrite `ast.Name` loads.

  4. **HUNK-UNION HAZARD specific to this file (cost me a full pass — do not repeat).** Several
     gateway/run.py hunks CUT THROUGH THE MIDDLE OF A STATEMENT, so a naive `ours + theirs` union
     produces a SyntaxError far from the seam. Confirmed instances: hunk 0/1 (two partial `from ... import (`
     blocks → the union loses the closing paren), hunk 14 (upstream's whole
     `_finish_startup_restore` gets spliced INTO the body of the fork's `_done` callback — and the
     fork ALREADY has its own superset `_finish_startup_restore` further down, so the correct move is
     take-FORK, not union), hunk 37/38 (`_set_session_vars_for_source(...)` call is cut before its
     `)`). RULE for this file: before unioning, check that BOTH sides are complete statements;
     when they are not, take the side that owns the enclosing statement.
     For 14 specifically: the fork's `_finish_startup_restore` is a strict SUPERSET of upstream's
     (bounded wait AND gate-release-in-`finally` AND a `_startup_restore_gate_watchdog`), so take fork.

  Recommended per-hunk dispositions from this run's analysis (each was derived by reading both
  sides; re-verify before staging):
    - 0,1,2,3,16,21,22 union (imports/module consts — but see the paren hazard above)
    - 4 take FORK (`_bridge_agent_config_to_env` helper) THEN fold upstream's 2 new keys
      (`session_stall_timeout`, `gateway_startup_restore_drain_timeout`) into the helper
    - 5,6,7 union (fork-only dicts have no shim; upstream's `_sessions` must also be initialized)
    - 8 upstream's `_peek_session_state` read + fork's `SessionRouteUnavailableError` raise
    - 9 upstream's per-session field write (fixes a real cross-session race) + fork's persist tail
    - 10,11,12 take FORK (it extracted `_load/_save_restart_failure_counts`; upstream only added
      `encoding="utf-8"` — fold that into the helper)
    - 13 take FORK (resume pre-claim sentinel MUST be handed back unconditionally; AEGIS-RIG 07-11)
    - 14 take FORK (see above), 15/19/20/32/33/34/36 SessionState-native forms
    - 17 upstream (`_primary_message_handler` multiplex scope)
    - 18 union (fork's /start,/restart,/stop fast-paths + upstream's busy_policy dispatch)
    - 23,24,25,26,27,28 take UPSTREAM as a BLOCK — they are ONE feature (`_record_hygiene_cooldown`
      chokepoint + progress-based hygiene timeout + the rotation/abort restructure). Splitting them
      strands `_hyg_commit_fence` / `_hyg_wait_started` references.
    - 29 take FORK (runtime_footer reasoning/message_count args — PRESERVE-LIST #4)
    - 30 upstream (ctx-based + chat_type), 31 take FORK (dedupe-key STT echo guard supersedes
      upstream's index counting), 35 upstream (`request_hard_interrupt`)
    - 39,40,44 upstream (TurnRunner wiring) + the closure_to_method product as the method bodies
    - 41,47,48 upstream (streaming-TTS holder/finalize/cleanup; ours-empty — required or the
      auto-merged `streaming_tts_consumer_holder` references NameError)
    - 42,43 union, 45,46 take FORK (`_check_backup_interrupt` + `wait_for_task_or_inactivity`
      extraction)

### VERIFICATION AT RUN END (measured, not remembered)
- MERGE_HEAD = `1e5b50744094959db5536eca9df3881d13fd28d8` — intact (`git rev-parse`).
- **79 unmerged / 4778 truly-staged.**
- MARKER AUDIT: all 4778 truly-staged files read byte-wise → **0 conflict markers**.
- SYNTAX: 2935 staged `.py` → **0 broken** (`ast.parse`).
- 0 commits on the branch, no PR, runtime tree untouched.


---

## RUN 70 (relay) — tui_gateway/server.py GOD-FILE RESOLVED (20 hunks) · 79 -> 77 unmerged

⚠️ OWNERSHIP NOTE: this run's kanban claim was reclaimed mid-flight (run 70 closed
`gave_up` on the iteration budget; run 71 took the card while this worker was still
writing). **Everything below is STAGED and durable** — re-verified against the tree at
write time. The next worker must NOT redo these files.

### ACCOUNTING (measured, not remembered)
- `MERGE_HEAD` = `1e5b50744094959db5536eca9df3881d13fd28d8` (intact, `git rev-parse`)
- **79 -> 77 unmerged**; 4781 truly-staged
- 8 touched files: 0 conflict markers, `ast.parse` clean (each re-checked at run end)
- 0 commits. Nothing committed, no PR, `~/.hermes/runtime/hermes-agent` untouched.

### ★ THE FINDING: `tui_gateway/server.py`'s 20 conflicts are a MONOLITH-SPLIT MIGRATION, not deletions
Upstream extracted ~124 `@method` handlers out of the 18K-line server.py into
`tui_gateway/methods_{session,prompt,tools,complete,config}.py` + `method_ctx.py`
(`HandlerRegistry.install()` rebinds each handler's `__globals__` to server.py's
namespace at import end, so handler bodies stay byte-identical and `global X` still
mutates server state). 9 of the 20 conflicts therefore render with the fork's evolved
handler on OURS and an **EMPTY THEIRS** — the classic relocation shape.

**Both blind picks are wrong, in opposite directions:**
- blind `--theirs` DELETES the fork's handlers outright (incl. 3 fork-only ones).
- blind `--ours` resurrects duplicate `@method` definitions. This one is the SILENT
  trap: server.py registers at import, `methods_*` register at `install()` which runs
  at the END of server.py's import — so **the split module's copy WINS** and every
  fork delta left behind in server.py's copy is DEAD CODE with zero symptoms. No
  marker, no SyntaxError, no import error, and the handler still "exists".

### METHOD (3-way per handler, then a gate)
Classified all 124 handlers across base / fork / upstream:
`89 IDENTICAL · 23 upstream-only · 11 BOTH-CHANGED · 3 fork-only · 1 fork-only-change`.
Ran a real `git merge-file --diff3` per fork-evolved handler (base = handler text at
merge-base a7a696ba, ours = fork's server.py copy, theirs = upstream's methods_* copy):
**5 merged clean, 6 conflicted and were hand-resolved.** Then DELETED the 38 duplicate
definitions from server.py behind a gate that refuses to delete unless every
fork-ADDED line (vs merge base, indentation- and comment-insensitive, minus lines that
already existed at base) is present in the split copy. Tools: `/tmp/p70_blocks.py`,
`p70_handler_classify.py`, `p70_port_handlers.py`, `p70_delete_dups.py`.

**The gate earned its keep: it caught 17 unported fork lines in `session.resume`** that
the take-OURS pass would otherwise have silently dropped (4 desktop-auto-resume return
sites + 2 source-sanitize sites). It also produced 2 false positives, fixed by
exempting lines already present at the merge base (a moved line is not a fork-authored
line).

### HAND-RESOLVED HANDLERS (the 6 that would not auto-merge)
- `model.options` [B + THREAD-THROUGH] — upstream EXTRACTED `build_model_options_payload`
  as a wrapper that **drops fork's `apply_picker_prefs=True`**. Fork's flag hides the
  internal claude-{apx,bpx}-N failover lanes + honours the user's `model.picker`
  hide/order on every desktop/TUI picker open. Added the kwarg to the new wrapper
  **defaulting False** (upstream consumers unchanged) and passed True from the two fork
  picker sites (`methods_complete.model.options`, `web_server` model-options route).
  PROVED live: signature carries it, default is False, threads into `build_models_payload`.
- `session.resume` [B] — re-grafted fork's `_maybe_trigger_desktop_auto_resume_after_resume`
  at all 4 upstream return sites + `_sanitize_client_source(params.source or found.source)`
  at both agent-init sites (upstream used `platform_override=source`; `_make_agent` treats
  `platform_override` as TRUSTED and `source` as UNTRUSTED-and-sanitized — the fork's lane
  is the correct one for client-declared labels).
- `session.branch` [B] — same source-inherit re-graft onto upstream's restructured
  (secret-scope + home-override) body.
- `session.list` [B] — fork's only real delta was the `"pinned"` field; the rest was
  upstream re-indenting into `with _profile_db(...)`. Re-grafted the field.
- `session.undo` [F] — fork REPLACED upstream's in-memory history truncation with a
  DB-backed architecture (`hermes_undo` + `_undo_session_core`/`_redo_session_core`,
  undo/redo stacks, `/undo N`). Upstream's "skip display_kind bookkeeping rows" fix is
  already covered by `hermes_undo._party`/`compute_half_turn_target`. Fork's
  `session.redo` (fork-only handler) depends on this pairing.
- `prompt.submit` [U] — re-grafted fork's blank-submit guard (rejects an empty prompt
  before building an agent — a reconnect-looping client otherwise burns a full ~50k-token
  API call to answer nothing) + the `hermes_undo.on_user_message_appended` redo-clear.

### OTHER RESOLVED HUNKS IN server.py (the 11 non-relocation blocks)
`_shutdown_sessions` [U] fork's desktop resume-marker write + upstream's wake-owner
release · `_submit_prompt_to_compute_host` [B] upstream's widened `_compute_host_turn_frame`
call (image_paths/queued_prompt_generation) + fork's inflight-turn registry write ·
`handle_request_bound` / `_current_session_steer_authority` [U] both live, both referenced ·
`_history_to_messages` [U] fork's id/timestamp/tool_call_id stamping + upstream's row_id +
skill-invocation projection · `_load_cfg` [UP + KEEP] upstream SPLIT it into
`_load_cfg_raw` (write-back primitive) + `_load_cfg` (managed overlay + ${VAR}); kept
fork's 4 desktop-auto-resume/session-sync config helpers above it · `_ManualCompressionInProgress`
+ `_apply_pending_model_switch` [U] base-empty · `_run_params` [B] upstream's signature
probe is a superset of fork's task_id check; fork's `_turn_started_monotonic` re-ordered
BEFORE the call (consumed at turn end) · terminal-frame [B] fork's `committed_ids` +
upstream's `turn_error_retained` / `_fail_inflight_turn` (the two are COUPLED — the flag
is set here and read in the settle block) · settle block [U] upstream's
`_emit_settled_session_info` (superset: reconciles a settled cwd first) + fork's
`_clear_desktop_session_restart_mark` · `_LIVE_SESSION_DIRECT_COMMANDS` block
[OURS-EMPTY = FORK RELOCATION] — the fork MOVED this 247-line block; the merged tree
already carries fork's copy (proved byte-identical to fork HEAD) at its new home, so the
block resolved to empty AND upstream's 2-line `include_row_ids=True` delta was ported
into the relocated copy (verified `get_messages_as_conversation` accepts it in merged
hermes_state).

### tests/test_tui_gateway_server.py (4 hunks) — resolved by MEASUREMENT
- tool-row shape: ran the merged `_history_to_messages` and read the real output —
  it returns upstream's arg-preview `context: "resume"` (upstream changed `_tool_ctx`
  to return the bare arg, asserted by the sibling test) AND fork's `tool_call_id`.
  Neither side's literal alone was correct; the UNION is what the impl produces.
- `own_key` capture + notify-unregister asserts: PARALLEL INVENTION of the same
  process-wide-list-pollution fix. Converged on upstream's form (reads the key off the
  response, so it cannot race the `session.close` that pops `_sessions[sid]`).
- worker assertion: a GENUINE behavioral change, verified against the impl —
  `_SlashWorker` is constructed only in `_restart_slash_worker`/`slash.exec`, never in
  `_build`, so `closed_workers` is always empty and fork's `own_key in closed_workers`
  could not pass. Took upstream's `created_workers == []`, which is the stronger
  count-free form of the same guarantee and matches the test's own docstring.

### TEST EVIDENCE (hermetic: sandbox HOME + HERMES_HOME, `-p no:randomly`)
- **`tests/test_tui_gateway_server.py` -> 530 passed, 0 failed (41.36s).**
- Live registry proof (import under sandbox HOME): module imports, **148 handlers
  register**, and all 8 fork deltas are present in the **REGISTERED** functions (not just
  in source): desktop auto-resume, source sanitization (resume + branch), pinned field,
  DB-backed undo, blank-submit guard, redo-stack clear, picker prefs. Also asserted each
  handler's `__globals__ is vars(server)` (the rebind works) and that every symbol they
  reference (`_err`, `_ok`, `_undo_session_core`, `_sanitize_client_source`, `sys`,
  `_maybe_trigger_desktop_auto_resume_after_resume`) resolves. Pyright reports these as
  "undefined" in the split modules — that is EXPECTED and by design (104 `_err` uses in
  methods_session.py alone); ignore that diagnostic class for `methods_*.py`.

### FILES STAGED THIS RUN (8)
`tui_gateway/server.py` (18180 -> 15043 lines) · `tui_gateway/methods_session.py` ·
`methods_prompt.py` · `methods_tools.py` · `methods_complete.py` · `methods_config.py` ·
`hermes_cli/inventory.py` · `tests/test_tui_gateway_server.py`

⚠️ `hermes_cli/web_server.py` is STILL `UU` — I resolved only its model-options hunk
(the `apply_picker_prefs` counterpart, needed for consistency with inventory.py). Its
remaining hunks are untouched; treat the file as unresolved but do NOT revert that hunk.

### REMAINING 77 (next worker)
- `gateway/run.py` 49h — now the single largest, and the last god-file gating the
  widest test surface (run69 attributed 12 of its 29 residual failures to it).
- `cron/scheduler.py` 12h · `agent/tool_executor.py` 9h · `hermes_cli/web_server.py` 8h
  (partially resolved, see above) · `tools/cronjob_tools.py` 6h.
- tests: `tests/tools/test_base_environment.py` 14h · `test_delegate.py` 11h ·
  `test_hermes_state.py` 11h · `tests/agent/test_anthropic_adapter.py` 7h.
- `apps/desktop/**` 33 files / ~71h — own lane (`npm ci && npx tsc --noEmit && npx vitest run`).
- mem0 DU/UU subsystem (HIGH STAKES) + AA-specials, unchanged from run60.

### STILL BLOCKED (needs a human; does NOT gate the relay)
`tests/agent/test_context_compressor.py::TestCalibration::test_calibrated_real_over_threshold_compacts`
— stale test, one-line order swap, root-caused in run68/69. Unchanged.

NOT DONE. Do NOT merge. Do NOT touch `~/.hermes/runtime/hermes-agent`.


### RUN 70 — VERIFICATION PASS (post-handback, prompted by an unverified-status flag)

The 530-pass figure above covered only `tests/test_tui_gateway_server.py` and predated
the `hermes_cli/inventory.py` edit. Ran the real blast radius. It found two things I
would otherwise have handed off silently.

**BLAST-RADIUS METHOD.** Enumerated every non-conflicted test file importing anything
this run touched (`tui_gateway*`, `hermes_cli.inventory`, `build_model*_payload`,
`hermes_undo`) -> 104 files. Excluded 10 that reference a still-`UU` module (they cannot
pass until those land) -> a 68-file CLEAN set, run A/B on the merged tree and on a
fork/main baseline worktree (`/tmp/p68_baseline`, ee2fce2876) with the identical file
list, runner and ordering.

- baseline (clean set): **1332 passed, 0 failed**
- merged tree (clean set + test_inventory): **1054 passed, 1 failed**

**FINDING 1 — `tests/hermes_cli/test_inventory.py` was itself still conflicted (1 hunk),
which is WHY the `apply_picker_prefs` change had no coverage.** Base-empty union: fork's
3 `test_apply_picker_prefs_*` tests + upstream's `_apply_featured` group. Both kept.
`tests/test_tui_gateway_server.py + tests/hermes_cli/test_inventory.py` -> **548 passed,
0 failed**, so the wrapper change is now covered by the fork tests that own the flag.

**FINDING 2 — ★ A REAL MERGE REGRESSION, security-relevant, found by test and fixed in
the impl.** `gateway/session_context.py::set_current_session_id` ended up with **TWO**
`os.environ["HERMES_SESSION_ID"] = session_id` writes — the classic AUTO-MERGE DUPLICATE
trap (no marker, valid syntax, both sides "kept"):
- FORK's write is guarded by `if os.environ.get("_HERMES_GATEWAY") != "1"` (PRD
  gateway-session-env-leak: the gateway runs concurrent sessions in ONE process, so an
  unconditional write clobbers every other live session's id — the v3-latch bug class).
- UPSTREAM's write is unguarded but preceded by an early `return` for delegated children
  (protects the PARENT's id within one process).
The merge kept the fork's guarded write AND upstream's unguarded one, so the unguarded
tail executed for every non-delegated caller and **re-opened the exact cross-session
clobber the fork guard exists to prevent**. Upstream never had the gateway guard, so
this is fork-critical.
FIX: one write site carrying BOTH guards (delegated-child early-return, then the
`_HERMES_GATEWAY` check). PROVED behaviorally, not assumed — live in-process:
gateway mode -> contextvar set, `HERMES_SESSION_ID` absent from os.environ, and a second
concurrent `set_current_session_id` does not clobber; CLI mode (`_HERMES_GATEWAY=0`) ->
mirror still written (the fallback CLI/cron/worker tools read is intact); exactly 1
write site remains. The guard test `tests/gateway/test_no_gateway_session_env_writes.py`
went from 2 violations to 1.

**THE 1 REMAINING FAILURE IS NOT A REGRESSION — proven, not asserted.** The residual
violation is `cron/scheduler.py:3756`. Parsed the file's conflict blocks: that line sits
**inside unresolved block #7 (L3737-3759), on the BASE side**. OURS (fork) replaces it
with the task-isolated ContextVar (`set_cron_session()`); THEIRS (upstream) is empty.
Both live sides drop it, so it disappears the moment `cron/scheduler.py` is resolved —
it is a source-scanning test reading a merge artifact. Whoever resolves scheduler.py:
re-run this test, it should go green with no further action.

**ATTRIBUTION OF THE BIG-BATCH NOISE (why the raw 99-file run showed 40 failures).**
Every one attributed, zero unexplained: 8 files fail because their own import dies on a
still-`UU` module (`gateway/run.py` 49h, `hermes_cli/web_server.py`, `cron/scheduler.py`),
and 12 more are CROSS-FILE POLLUTION — green when their file runs alone. Bisected the
dominant polluter to a single file: `tests/gateway/test_multiplex_credential_isolation.py`
(untouched by this run), whose failed import of the conflicted `gateway/run.py` leaves
broken module state that poisons every later file in the same interpreter. Confirmed the
same pollution class exists on the BASELINE tree (4 failures in-batch, green alone), so
it is pre-existing, not merge damage. Do NOT read those as regressions.

**Durable rule for the next worker:** a source-SCANNING test (one that greps the repo
rather than importing it) will read UNRESOLVED CONFLICT BLOCKS as if they were live code.
Before treating its violation as real, check whether the flagged line sits inside a
conflict block and on which side — `/tmp/p70_blocks.py <file>` answers it in one call.

Files staged by this verification pass: `gateway/session_context.py` (the regression fix),
`tests/hermes_cli/test_inventory.py` (union).


---

## run71 (2026-08-07, ~17:45 PDT) — BLOCKING POLICY CONFLICT in `tools/cronjob_tools.py`

Ownership note: I ran as a STALE run (run71) after the dispatcher rolled ownership to run74,
which then also died. At the time of these edits the card was `blocked`, `current_run_id=NULL`,
`consecutive_failures=5` (breaker tripped) — **no live owner**, tree static across 5 samples
40s apart, `lsof +D` showing no writer but my own shell. So the tree was safe to touch.
`~/.hermes/runtime/hermes-agent` verified 0 dirty @ 40b0df9893 and NOT touched.

### 1. Two files were resolved-but-never-staged (free win, now staged)
`tests/hermes_cli/test_web_server_session_search.py` and `tests/tools/test_cronjob_tools.py`
were both still `UU` with **0 conflict markers** and clean `ast.parse` — a previous run resolved
the content and died before `git add`. Audited both before staging:
- fork defs missing from the working tree: **0** for session_search; **21** for cronjob_tools.
- All 21 classified: every one is base-present / upstream-absent (upstream's own test-prune).
- Applied run71's **4th prune constraint** (fork body == base body, AST-normalized): **0 of the
  21 were fork-modified**, so honoring the prune loses no fork work. This is the constraint the
  earlier note warned about — it was checked here, not assumed.
- Fused-test detector (>=2 blank lines then a fresh `assert`/assignment inside a test body):
  **0 suspects** in both files.

### 2. ⛔ `tools/cronjob_tools.py` (6 hunks) is a GENUINE-BOTH policy conflict — NOT mechanical
This is **not** in any earlier ledger entry (grepped: cronjob_tools appears only in the two
conflict-count tables at L477 / L831). Blocks 1-3 are ordinary additive/either-side merges.
**Blocks 4-6 are a direct policy collision that must not be resolved by picking a side:**

- **UPSTREAM deliberately REMOVED the agent-facing model-pin surface** as a security control.
  Verbatim from its registration: *"model / provider / base_url are intentionally NOT read from
  the agent's arguments: per-job inference pins are user-owned (dashboard, `hermes cron
  create/edit --model`, or hand-edited jobs). **The agent must not be able to point unattended
  spend at a different model.** Programmatic callers of cronjob() itself retain the parameters."*
  Verified upstream also dropped `_resolve_model_override` **and** the `model` key from
  `CRONJOB_SCHEMA` entirely — a coherent removal, not an oversight.
- **The FORK built a substantial feature on exactly that surface**: `model="auto"` pins a cron to
  the CREATING agent's own model (`set_current_agent_model` + a module-global that the fork's own
  comment explains at length must NOT be a ContextVar, due to the asyncio task boundary —
  live-repro 2026-07-18), plus `_coerce_model_override_arg` and the Greptile #411 P2
  half-pin fix.

Blast radius if resolved blindly, measured:
- Taking UPSTREAM breaks **26 fork tests** (`tests/tools/test_cron_auto_model.py` 13 +
  `tests/tools/test_cron_model_arg_coercion.py` 13) and strands the live call site at
  `agent/turn_context.py:506-507` (which is already staged `M`, i.e. it survives the merge and
  would call into a function that no longer exists).
- Taking FORK silently discards an upstream security control about unattended spend.

**DECISION REQUIRED (Ace/Apollo), three options:**
1. Keep the fork feature whole (accept the divergence; document why the fork's threat model
   differs — the agents here are the operators).
2. Take upstream's restriction and delete the fork feature + its 26 tests + the turn_context
   call site.
3. **Compose:** keep `model="auto"` (pin to the creating agent's OWN model — which cannot point
   spend at a *different* model, so it arguably satisfies upstream's stated intent) while
   dropping agent-supplied arbitrary `model`/`provider`/`base_url`. This is the only option that
   preserves both intents, but it is a NEW third behavior and must be a deliberate choice, not a
   merge artifact.

I did NOT resolve blocks 4-6. `tools/cronjob_tools.py` remains `UU` on purpose.
Consequence: `tests/tools/test_cronjob_tools.py` still cannot be collected (its import of
`tools.cronjob_tools` dies on the conflict markers), so the staged test file is correct but
unverifiable until the policy call lands.

### 3. ⚠️ TRAP in `hermes_cli/web_server.py` (7 hunks) — blind `--theirs` silently deletes a fork feature

`web_server.py` is upstream's **router-extraction** refactor (same mechanical class as
`gateway/run.py`): each conflict replaces a large in-file endpoint (OURS) with a 1-6 line
`app.include_router(...)` + legacy re-export (THEIRS). Upstream's `hermes_cli/web_routers/`
package is already present and STAGED (8 modules, `sessions.py` 720L). That shape makes
`--theirs` look obviously correct. **It is not, for block 5.**

Measured, do not re-derive:
- Fork's `search_sessions` in `web_server.py` is only a **26-line delegating wrapper**; the real
  logic is in the fork's `_search_sessions_impl`.
- Upstream's extracted `web_routers/sessions.py::search_sessions` is **222 lines** and DOES carry
  the compression-lineage dedup (`compression_root`, `root_cache`) — that part was upstreamed, so
  it is NOT lost.
- **But the fork's TITLE-RANKING is not in upstream's router.** `db.search_sessions_by_title(...)`
  is called at `web_server.py:5768` — verified NOT inside any conflict block, i.e. it is live
  merged code today — and `grep title hermes_cli/web_routers/sessions.py` shows only
  rename/update-endpoint hits, no ranking. `search_sessions_by_title` exists in
  `hermes_state.py:9340` and appears **nowhere** in the upstream router.
- The staged fork test `tests/hermes_cli/test_web_server_session_search.py` asserts exactly this:
  `test_desktop_session_search_ranks_title_matches_before_content_matches` (+ `_FakeTitleSessionDB`,
  `search_sessions_by_title`) — the 3 fork-only defs confirmed present in the staged file.

So resolving block 5 to `--theirs` gives a GREEN-looking file whose endpoint no longer
title-ranks, while the fork's impl sits orphaned a few hundred lines below. The fork test would
then fail against the router (it patches `web_server.<name>`), and "fix the test" would be the
wrong move. **Correct resolution: take upstream's router extraction AND port the fork's
title-ranking into `web_routers/sessions.py::search_sessions`**, then re-point/keep the legacy
re-export so the fork test's `web_server.search_sessions` patching still resolves.

This is the same "upstream extracted it, fork enhanced the original" shape flagged for
`gateway/run.py`. Assume it recurs in the other 5 `web_server.py` blocks and in `web_routers/`
generally: for EVERY extracted endpoint, diff the fork's impl against upstream's router body
for fork-only calls before accepting `--theirs`.

### 4. Staged this run (3 files) — 61 -> 59 unmerged
`tests/hermes_cli/test_web_server_session_search.py`, `tests/tools/test_cronjob_tools.py`,
and this ledger. **Neither test is executable yet** — both die at collection on their still-`UU`
impl (`hermes_cli/web_server.py:4975`, `tools/cronjob_tools.py:140`, both
`SyntaxError: invalid decimal literal` from a `>>>>>>>` marker). Real output, not asserted:
```
tests/hermes_cli/test_web_server_session_search.py:3: in <module>
    from hermes_cli import web_server
E     File ".../hermes_cli/web_server.py", line 4975
E       >>>>>>> 1e5b50744094959db5536eca9df3881d13fd28d8
E   SyntaxError: invalid decimal literal
ERROR tests/hermes_cli/test_web_server_session_search.py
ERROR tests/tools/test_cronjob_tools.py
2 errors in 0.27s
```
They are staged because their CONTENT is audited-correct (0 markers, AST-clean, 0 fork defs lost
under the 4-constraint rule, 0 fused-test suspects), not because they were proven green. Whoever
resolves the two impl files must re-run both — that is the outstanding verification debt.


### RUN 70 — VERIFICATION PASS 2 (tree moved: 77 -> 61 unmerged under a concurrent worker)

Re-verified because the previous evidence went stale when another worker resolved files
in this shared worktree. Re-derived the clean set against the CURRENT tree (68 -> 90
eligible files as their blockers landed), then re-ran A/B.

**RESULT (identical 69-file set, merged tree vs fork/main baseline):**
- merged : **1044 passed, 1 failed**
- baseline: **1340 passed, 0 failed** (green on two consecutive runs; higher count because
  upstream's test-prune removed tests that still exist on fork/main)
- merged, wider 89-file set: **1208 passed, 2 failed**

**THE 1 DETERMINISTIC FAILURE IS THE KNOWN ARTIFACT.**
`tests/gateway/test_no_gateway_session_env_writes.py` — still the `cron/scheduler.py:3756`
line sitting on the BASE side of unresolved conflict block #7. Repeats verbatim in every
run. Clears when scheduler.py is resolved. Unchanged from verification pass 1.

**★ THE SECOND FAILURE IS LOAD FLAKE, NOT A REGRESSION — established by evidence, not
convenience.** The wider run showed exactly ONE extra failure, but a DIFFERENT test each
time across three consecutive runs of the SAME 89-file batch:
1. `test_tui_gateway_server.py::test_tui_drop_of_unowned_async_delegation_advances_delivery_attempts`
2. `test_compute_host_phase1.py::test_shutdown_drain_sleep_never_overshoots_the_reserve`
3. `test_tui_gateway_server.py::test_run_prompt_submit_requeues_all_unstarted_notifications_with_real_threading`
All three are timing/threading-sensitive; all three pass in isolation AND pass together
(10 passed in 3.05s); a targeted re-run of the full batch PASSED the victim outright.
Box load average was **11.44** during the runs (a concurrent worker's suite shares this
machine). Per the skill's own rule — a failure that SHIFTS shape between rounds is flake,
one that repeats at the same place is real — these are flake and the scanner is real.

⚠️ RELAY NOTE: this box is CONTENDED. Timing-sensitive tests in tui_gateway/ and
compute_host will produce a rotating single failure under a parallel suite. Before
triaging such a failure as a regression, re-run the batch: if the failing NODE moves,
it is load, not code. Do not "fix" it.

**A BISECT TRAP worth recording.** My first bisect for the polluter keyed on the pytest
EXIT CODE and converged on `test_no_gateway_session_env_writes.py` — wrong: that file
fails on its own for an unrelated reason, so the exit code tracked ITS failure, not the
victim's. Re-running keyed on the VICTIM'S OWN nodeid (`^FAILED <nodeid>` in the output)
showed the victim passing with its entire prefix, which is what exposed the flake. When
bisecting cross-file pollution, always key the predicate on the victim nodeid, never on
the run's exit status.

Net: **zero unexplained failures, zero regressions attributable to this run's changes.**

---

## RUN 75 — `cron/scheduler.py` RESOLVED (12/12 blocks) + 2 real merge defects fixed

**59 -> 58 unmerged.** Staged: `cron/scheduler.py`, `gateway/response_filters.py`,
`gateway/session_context.py`, this ledger.

### Per-block decisions (all 12, none blind-picked)

| # | Decision | Why |
|---|---|---|
| 1 | **UNION** | fork `scheduler_ext` + upstream `request_hard_interrupt` / `enter|exit_non_dispatcher_owned_context`. All 3 upstream names are USED (L3906/4514/4652) — `--ours` = NameError at runtime. |
| 2 | **SUPERSET import** | fork needs `get_job` (L4924), upstream needs `advance_next_runs` (L5256). Both verified to exist in `cron/jobs.py` (:1507, :2016, :2053). Either blind pick = ImportError or NameError. |
| 3 | **UNION docstring** | keeps fork's code-wrap note + upstream's "delegates to shared matcher" note. |
| 4 | **THEIRS + PORT** | upstream delegates `_is_cron_silence_response` to `gateway.response_filters.is_autonomous_silence_response`. Fork's markdown code-wrap tolerance did NOT exist there → ported into the shared matcher (below). |
| 5 | **COMPOSE** | kept fork's `subprocess.Popen` (load-bearing: `terminate_running_scripts` needs the handle; `subprocess.run` gives none and made in-flight scripts uncancellable) + adopted upstream's `_script_cwd = workdir or str(path.parent)` (#69396). |
| 6 | **`cwd=_script_cwd`** | fork's Popen has no `timeout=` kwarg (communicate() handles it); upstream's cwd var retained. |
| 7 | ★ **THEIRS (empty)** | see DEFECT 1. |
| 8 | **COMPOSE** | fork's `_coerce_job_model()` (flattens dict-shaped `job["model"]`, the "no model configured while quoting a present model" bug) + upstream's cron.model precedence. |
| 9 | **THEIRS, re-gated** | upstream's `cron.model` fleet-default block, but gated on fork's coerced `_job_model` instead of raw `job.get("model")` — otherwise the dict-shaped-model bug reopens on this path. |
| 10 | **THEIRS** | fork's drift-guard model branch already exists as COMMON code above (L4235-4245, with the `_cron_default_model` gate); this block was a duplicate + upstream's `raise`. Taking ours = double-append to `_drift`. |
| 11 | **THEIRS** | `clear_cron_session()` is dead once block 7 takes theirs; upstream's paired resets are the live path. |
| 12 | **COMPOSE** | fork's `_process_one_job` + execution-ledger bookkeeping (the 948-phantom-`unknown`-rows fix) KEPT, with upstream's `extra_prompt` param added to `run_one_job`. `--theirs` would have deleted 165 lines of fork work. |

### ★ DEFECT 1 — double-set of the cron ContextVar (blocks 7/11), zero markers after auto-merge
Fork set the cron marker EARLY (L3750 `set_cron_session()`); upstream sets the SAME
var later at L3886 (`_cron_session_var.set("1")`) — **both inside `run_job()`**
(verified: enclosing def is `run_job` at L3472 for 3750, 3886, 4646 and 4649).
Keeping both = two `set()` calls, only one reset token consumed by the matching
`finally` → a leaked token and the later set silently winning.
Verified the window L3750-3886 is **inert** for the marker (no reader between them;
`copy_context()` happens at L4452, well after both), so dropping the early set changes
no behavior. Took upstream's single set/reset pair.
**Bonus:** this also removes `os.environ["HERMES_CRON_SESSION"] = "1"` at old L3756 —
the BASE-side line that `test_no_gateway_session_env_writes` had been flagging in every
prior run. Predicted by runs 68/70 to "clear when scheduler.py is resolved"; **it did.**

### ★ DEFECT 2 — duplicate `_CRON_SESSION` ContextVar in `gateway/session_context.py` (ALREADY STAGED)
Measured: `HEAD` defs=1, `MERGE_HEAD` defs=1, **merged staged tree defs=2** — plus a
duplicated `"HERMES_CRON_SESSION": _CRON_SESSION` key in `_VAR_MAP`. Classic auto-merge
duplicate: valid syntax, no markers, no test failure. Two `ContextVar` objects with the
same name is the dangerous shape — a `set()` on one is invisible to a `get()` on the
other (the exact cron-marker-lost bug class). Only saved here because the second def
shadowed the first before any reader bound to it (verified: 0 references between the
two defs). Removed upstream's terser def + the duplicate dict key; kept fork's
documented one. Proved post-fix `_VAR_MAP["HERMES_CRON_SESSION"] is _CRON_SESSION` → True.

### Ported fork behavior into the shared matcher (block 4's precondition)
`is_autonomous_silence_response` in `gateway/response_filters.py` gained fork's
`_strip_code_wrap` (symmetric ``` fence + backtick-span peeling, incl. info-string
lines) and the leading-inline-code-span peel for the `[SILENT] prefix` form. Without
this, block 4's delegation **silently loses** cron's tolerance for a model that formats
the literal sentinel — the sentinel then leaks to the channel as noise, and 6 fork
assertions in `tests/cron/test_scheduler.py:2246-2255` would fail with "fix the test"
looking like the answer. It is not.

### Verification — real output, not asserted
Interpreter matters: bare `python3` on this box is Anaconda 3.7 and dies in
`tests/gateway/conftest.py` with `TypeError: 'type' object is not subscriptable`
(PEP-585 generics). Use `~/.hermes/runtime/hermes-agent/.venv/bin/python` (3.11.15).

```
tests/gateway/test_response_filters.py tests/gateway/test_cron_session_contextvar.py
tests/tools/test_cron_subagent_session.py tests/gateway/test_no_gateway_session_env_writes.py
  -> 20 passed in 1.06s          (incl. the previously-failing scanner test)
tests/gateway/test_webhook_adapter.py tests/gateway/test_stream_consumer_silence.py
tests/cron/test_shutdown_interrupt.py
  -> 51 passed in 1.98s
```
Behavioral proof (in-process, not inferred): `import cron.scheduler` OK; all **16**
silence cases match fork semantics through the shared matcher (fenced/spanned/prefix
forms suppress; mid-sentence mentions and "Silent retry succeeded" still deliver);
`run_one_job` signature carries `extra_prompt`; `_process_one_job` present; nested
`set_cron_session`/`clear_cron_session` restores "1" then "" and `clear(None)` no-ops.

Tree: MERGE_HEAD 1e5b5074 intact · **58 unmerged** · 4817 truly-staged ·
**0 conflict markers** · **2949 staged .py, 0 broken** · 0 commits · no PR ·
runtime tree 0 dirty @ 40b0df9893, untouched.

### For the next runner
`tests/cron/test_scheduler.py` is still UU (5 blocks; #2 and #3 are 200/418-line
both-sided monsters) — it is the natural next file now that its impl is resolved.
Then `gateway/run.py` (186 hunks, the widest test surface).


---

## run80 batch — the TAIL, and two defects that survived every prior audit

Entered at **44 unmerged** (gateway/run.py resolved by Apollo in the isolated lane).
Exited at **36**. Files closed this run, all with real hermetic test output:

| file | choice | result |
|---|---|---|
| `tests/run_agent/test_message_sequence_repair.py` | B (union + restore) | 23 passed, 2 xfailed |
| `tests/tools/test_mcp_tool.py` | F | 90 passed |
| `tests/agent/test_compression_concurrent_fork.py` | U (name-union) | 58 passed |
| `agent/conversation_compression.py` | B (seam re-graft) | (see FINDING 1) |
| `tests/gateway/test_compress_command.py` | U + 1 fork pick | 26 passed |
| `tests/tools/test_base_environment.py` | U + 3 stale-literal fixes | 34 passed |
| `tests/tools/test_delegate.py` | U + 2 converged | 169 passed |
| `tests/cron/test_scheduler.py` | U + 2 stale fixes | 262 passed |
| `tools/cronjob_tools.py` | U(b0) + F(b1-5) | unblocked 80 scheduler tests |

### ★ FINDING 1 — handler STARVATION in an already-staged, already-audited file

`agent/conversation_compression.py` was staged by a prior run, marker-free,
AST-clean, symbol-complete. It still carried a silent semantic defect.

Fork PR #33906/#33907 added an **INNER** publish-failure handler inside the
rotation `try`: it rolls the live session id back to the parent, sets
`old_session_id = None`, and re-`raise`s. Upstream has **no** inner handler — it
recovers in the **OUTER** `except`, guarded on `locals().get("old_session_id")`
still being truthy, where it does the transcript rollback
(`messages[:] = deepcopy(messages_before_compression); compressed = messages`)
and restores `_proactive_prune_rearm_tokens`.

The merge kept BOTH. The inner runs first and clears the variable the outer
guard reads, so **the entire restoration is starved**. Each side is correct
alone; only the composition is broken.

Impact: a rotation whose child-session publish fails rolls the session back but
returns the **stale compacted transcript** to the caller and never restores the
prune runway. No exception, no log, no marker — invisible to source diffing.

Fix: re-grafted upstream's restoration INTO the fork's inner handler at the point
the rollback actually happens, with a parity NOTE. Outer block untouched (it
still covers paths that reach it with `old_session_id` intact) so BOTH contracts
hold. RED-proven: neutralising the graft reproduces
`test_rotation_publish_failure_restores_proactive_prune_runway` failing
(1 failed / 57 passed); restoring it gives **58 passed**. Blast radius
(compress_persist_failure, rotation_state, orphan_recovery, anti_thrash_persistence,
attempt_telemetry) = **36 passed**.

> **Relay rule:** marker-free + AST-clean + symbol-complete does NOT clear a file
> that received hunks from both sides of an error-recovery guard. Any file where
> fork and upstream both own recovery for the same operation needs the guard-order
> read, not just an audit.

### ★ FINDING 2 — a DECAPITATED test that passed silently

`test_repair_leaves_valid_conversation_unchanged` survived with its setup but
**no call and no asserts** (body ended at `original = [dict(m) for m in messages]`)
— a permanently-green no-op. Restored verbatim from the fork blob. Also restored
4 **upstream-LIVE** `test_sanitize_*` tests the merge dropped; confirmed the
behaviour they guard is live by calling `sanitize_api_messages` directly in the
merged tree (dedupe -> `['call_Y']`, empty `tool_calls` stripped, distinct ids kept).

### FINDING 3 — 2 reds that are NOT merge damage -> card `t_08cca32f`

Pass 1.5 of `repair_message_sequence` reads `tc.get("id")` ONLY while Pass 1
matches the `id||call_id` superset (#58168), so a `call_id`-only tool_call reads
as unanswered and the "none answered" branch **deletes a correctly-answered
assistant turn**. Classified INHERITED on evidence: merge diff over that region is
empty; `Pass 1.5` = 6x at fork/main HEAD, **0x** at base and **0x** upstream;
reproduced on a clean detached worktree at `ee2fce2876`. Disposed per doctrine as
`xfail(strict=False)` + evidence-carrying reason + card carrying a measured
candidate fix. NOT fixed inside the merge.

### THE DOMINANT TAIL CLASS — mutual prune (and why neither side is pickable)

Most remaining test files are **mutual prunes**: the fork pruned tests upstream
still ships AND upstream pruned tests the fork still ships, plus upstream added new
ones. `--ours` drops upstream-live tests; `--theirs` drops fork-authored ones.
Resolution is a **name-level union**, asserted zero-loss before writing.

Tooling built this run (all in `/tmp/r80/`, re-usable next run):
- `union4.py` — the resolver. Unions tests + module-level helpers, **inserts
  other-side-only tests INTO existing classes** (v2/v3 could only append at module
  level and stranded 13 class-bound tests), refuses to write on any loss.
- `fiximports.py` — unions the IMPORT surface. Appending tests without this strands
  them on `NameError: MAX_DEPTH` (bit `test_delegate.py`, 8 failures).
- `fixclasses.py` — restores `setUp`/`_fn`/class constants for classes union4
  appended. Without it a new class arrives crippled (`AttributeError: no attribute
  '_fn'`, 91 failures in `test_scheduler.py`).
- `audit.py` / `dupcheck.py` / `classify.py` / `bodydiff.py` / `whereis2.py` —
  loss audit, duplicate-artifact audit, union-safety triage, per-test body diff,
  tree-wide relocated-vs-deleted lookup.

**Three ordered steps per mutual-prune file: union4 -> fiximports -> fixclasses.**
Skipping either follow-up produces a large, misleading red wall.

### ★ THE CLUSTERING TRAP, again (skill Phase 0 warns about exactly this)

`tests/cron/test_scheduler.py` reported **82 failed / 180 passed** — reading as a
long semantic tail. **80 of the 82 shared ONE signature**:
`SyntaxError: invalid decimal literal` on a line reading
`>>>>>>> 1e5b5074...` — raw conflict markers in a DIFFERENT file
(`tools/cronjob_tools.py`), which every test in the file imports. Resolving that
file took the suite to **262 passed / 0 failed**. Cluster by error signature
BEFORE projecting a fix tail.

### `tools/cronjob_tools.py` — a genuine POLICY conflict, resolved fork

Blocks 0-1 are additive unions (base empty; both sides added different things).
Blocks 2-5 are ONE coherent decision, not four hunks: **upstream deliberately
removes the agent's ability to pin a cron's model** ("per-job inference pins are
user-owned... the agent must not be able to point unattended spend at a different
model"); the fork ships `model="auto"` + flat-string coercion as a feature.

Took FORK, because the fork ships two dedicated suites for it
(`tests/tools/test_cron_auto_model.py`, `tests/tools/test_cron_model_arg_coercion.py`,
26 tests) that are **absent upstream** and present in the merge result — taking
upstream would have broken all 26. Verified: **26 passed**. Upstream's own
new feature from block 0 (`_CRON_RUN_HEARTBEAT_INTERVAL` / `_CEILING`, #76502)
was UNIONED in and is genuinely consumed (defined :127/:136, used :1124/:1126/:1176),
so neither side's feature was dropped. Single `registry.register` confirmed;
`dupcheck.py` CLEAN.

⚠️ **Flag for the merge owner:** this is a SPEND-SAFETY policy divergence, not a
mechanical seam. The fork now permits what upstream deliberately forbids. It is
the fork's existing shipped behaviour and its tests are the contract, so the merge
preserves it — but it deserves an explicit owner ruling rather than inheriting my
call by default.

### Stale-test updates made (all legitimate, none weakening a contract)
- `test_base_environment.py` x3 — merged snapshot writer adopted upstream's
  subshell-unset dump + `.tmp.XXXXXXXXXX` template. Re-pointed the literals after
  proving every INVARIANT still holds by calling `_wrap_command` directly
  (umask present/ordered, template quoted, no unquoted leak, `&&`-chained mv,
  `rm -f` cleanup — all True; only `export -p | grep -vE` is gone).
- `test_delegate.py` x2 — upstream RELOCATED the concurrency/spawn-depth caps out
  of the top-level description into the per-parameter descriptions on purpose
  (`_build_top_level_description.__doc__`). Converged to `A or B` rather than
  downgrading the code to re-duplicate them. RED-proven a sibling test still
  gates the property (breaking the dynamic cap -> 1 failed).
- `test_scheduler.py` x2 — merged `_deliver_result` kept the FORK's user-facing
  wrapper (`🪪 Job ID:` + ⚠️ failure branch) over upstream's `(job_id: ...)`;
  and the merge adopted upstream's widened `_run_job_script(script_path, workdir=None)`
  so the fork's `(path)`-only stub needed the kwarg.

### TREE at run80 exit
MERGE_HEAD `1e5b5074` intact · **36 unmerged** · 4839 staged · 0 commits · no PR ·
`~/.hermes/runtime/hermes-agent` untouched.
Hermeticity PROVEN empirically on every run: `lsof ~/.hermes/state.db | grep -c python`
= **0** throughout (the export alone is not evidence).

### For the next runner
Remaining: **3 Python** — `tests/test_hermes_state.py` (11 hunks, 336 fork-only /
65 upstream-only tests, 4 differing bodies), `agent/tool_executor.py` (9),
`hermes_cli/web_server.py` (7) — plus **32 `apps/desktop/**`** and
`web/src/lib/gatewayClient.test.ts`. The desktop lane needs `npm ci`
(node_modules ABSENT) and is a separate toolchain; do not start it without the
install. For the Python three, run the union4 -> fiximports -> fixclasses ladder
first, then hand-resolve what remains.


---

## run81 batch — the CLEAN-ADD regression class (damage with NO conflict to review)

Entered at **36 unmerged**. Run80 died at 21:32 having WRITTEN but never STAGED two files
(mtimes 21:25 / 21:30). Audited both before touching the tail. One was clean; the other was
carrying a defect class no prior run had hit.

### ★ FINDING 1 — upstream's `web_routers/` split silently REVERTED the fork's event-loop offload

`hermes_cli/web_server.py` failed the fork-authored-loss gate: 4 fork functions dropped
(`_read_sessions_page`, `_read_session_detail`, `_read_session_stats`, `_rename_session_record`),
**0 references tree-wide**.

Cause: upstream ran a **monolith-split** (`web_server.py` -> `hermes_cli/web_routers/`). 113
base-present functions relocated there legitimately (verified: NOT-found-in-router-pkg = 0). But
`web_routers/sessions.py` arrived as a **clean ADD — status `A`, no conflict, no marker**, and is
**byte-identical to upstream**; the path does not exist on fork/main at all. Git had nothing to
conflict. Upstream's extraction was taken from UPSTREAM's handler bodies, which never carried the
fork's offload — so the fork's helpers were stranded in `web_server.py`, lost their only call
sites, and were pruned as dead code.

Net effect: **the fork's SessionDB offload was reverted on the REST surface by a file that never
conflicted.** 5 handlers became `async def` + direct `_open_session_db_for_profile` with zero
`to_thread`. `_session_db_read` / `_blocking_io` / the heavy-read gate
(`session_db_heavy_read_slot`, `SessionDBHeavyReadBusy`) still EXIST but had **zero production
callers** on REST (`heavy=` sites: fork 2, merged 0) — the gate survived only for `tui_gateway/ws.py`.

Why every audit passed it: marker-free, `ast.parse` clean, and
`tests/test_web_server_sessiondb_eventloop.py` -> **8 passed**. That file's AST gate only asserts
over `TARGET_HANDLERS` (10 names) and **all 5 damaged handlers are outside that set**; its
heavy-gate tests call `web_server._session_db_read(...)` DIRECTLY, so they stay green while
nothing in prod calls it. The merge even auto-kept the fork's 5 extra tests there (fork 7 +
upstream 3 -> merged 8), which made the file LOOK like a successful union.

RED-PROVEN by differential (fake SessionDB, 300ms blocking read, heartbeat measuring loop stall):

```
BEFORE FIX  merged  get_session_detail : wall 453ms | worst loop stall 453ms | DB ON LOOP = True
CONTROL     fork/main ee2fce2876       : wall 448ms | worst loop stall  20ms | DB ON LOOP = False
AFTER FIX   merged  detail/stats/search/rename -> ON_LOOP=False on all four, stalls 12-78ms
```

Fix: re-grafted the offload onto upstream's (otherwise byte-identical) extracted bodies, with
parity NOTEs — `get_session_stats` + `get_session_detail` + `rename_session_endpoint` wrapped in
inner fns dispatched via `asyncio.to_thread` / `_session_db_read`, and `search_sessions` routed
through `_session_db_read(..., heavy=True)` to restore the BOUNDED heavy-read gate (FTS search is
the heaviest read on the surface). Added the `_session_db_read` late-binding seam to
`web_routers/sessions.py`. Post-fix AST sweep: **0 async handlers opening the DB on the loop**.

> **Relay rule (new class):** every audit so far scoped to CONFLICTED files. This defect lives in a
> **clean-ADD** file. When upstream RELOCATES code into new paths, fork features attached to the old
> location are deleted with **no conflict to review**. The fork-authored-loss gate on the OLD file is
> what caught it. Any other upstream monolith-split in this merge deserves the same check.

### ⚠️ MEASUREMENT CORRECTION — the `:1:` base-read gotcha bit, and it inverted a verdict

I first measured `tests/hermes_cli/test_web_server.py` as losing **391 fork-authored** tests. That
was WRONG. `git show :1:<path>` returns rc=128 ("in the index, but not at stage 1") for any file
that is NOT conflicted — my base set came back EMPTY, so every lost name looked fork-authored.
Re-measured against the true merge base (`git merge-base` = `a7a696ba`): **fork-authored lost = 0**,
all 391 are upstream's own prune. The skill documents this exact trap; it still cost a wrong
intermediate verdict. **Assert the read (rc==0 AND non-empty) before trusting any base comparison.**

### Tree-wide acceptance gate (re-run with the assertion in place)

```
staged test files scanned : 2390      PARSE FAILURES: 0
tests lost vs fork (total): 23691     (upstream's own prune)
FORK-AUTHORED tests LOST  : 11        -> 8 were class-RENAMES in-file; 3 genuinely absent
```

The 3 genuinely-absent (`test_moa_resolves_custom_provider_per_model_context`,
`..._canonical_provider...`, `..._preserves_caller_supplied_custom_provider_context`) were replayed
VERBATIM from the fork blob against the merged tree -> **3 passed**: the behaviour shipped, only the
coverage was dropped. Restored them into `TestAggregatorGreptileFixes`.

### FINDING 2 — an appended-tests casualty a PRIOR run left behind

`tests/agent/test_model_metadata.py` was already staged and already RED: **14 failed / 74 passed**
(confirmed by reverting to the staged blob — pre-existing, not mine). A prior run appended fork
tests without their module-level constant: `_AGG_MODELS_DEV_SAMPLE` had **7 refs, 0 defs**
(`NameError`). Exactly the `fiximports` trap the local skill names. Restored the constant verbatim
from the fork blob -> **91 passed**.

### FINDING 3 — `unread` starvation: fork's fast path RETURNS before upstream's stamping loop

`tests/hermes_state/test_session_list_denorm_acceptance.py`: **fork/main 20 passed, merged 14
failed**. Two clusters, both real merge damage:

1. **13 x oracle mismatch.** Upstream added the derived `unread` key, stamped in a loop at the TAIL
   of `list_sessions_rich`. The fork's denorm fast path (`_list_sessions_rich_denorm`) **returns at
   line ~7333, before ever reaching that loop** — so fast-path rows lacked `unread` while the CTE
   oracle carried it, and the acceptance oracle compared unequal. Same overlapping-composition shape
   as run80's FINDING 1: each side correct alone, only the composition wrong. Fixed by stamping
   `unread` in `_finish_session_list_rows` — the tail **both** paths share — so fast path and oracle
   produce identical rows.
2. **1 x contract under-count** (`assert 5 == 6`). `_parent_mutation_contract` does
   `next(node for node in tree.body if node.name == "SessionDB")`, but upstream's split moved the
   6th mutation site (`import_sessions`' parent re-link) into
   `hermes_state_portability.SessionPortabilityMixin`. Scanning `hermes_state.py` alone finds 5, and
   naively concatenating the modules raises **StopIteration** (no `SessionDB` class in the split-out
   file). Fixed by scanning `SessionDB` **or** any `*Mixin` carrier class. The fork's maintenance
   call is still present at `hermes_state_portability.py:697` — the CONTRACT needed to follow the
   split, the behaviour was never lost.

Result: **20 passed** (matches the fork baseline exactly).

### Differential attribution — what is NOT mine

`tests/test_hermes_state.py` is **16 failed / 510 passed**, and stays byte-for-byte the SAME failure
set with my `unread` graft removed (`caused BY my graft: []`, `fixed BY my graft: []`,
`common: 16`). Pre-existing in a file a prior run staged; distinct clusters
(`display_metadata` decode x7, `get_resume_conversations` x3, trigram-index x2,
compact-projection `system_prompt_hash`, FTS cadence, compression-tip ordering). **Not charged to
this run and NOT fixed inside the merge** — needs its own pass.

`tests/hermes_cli/test_web_server.py` -> 1 failed / 142 passed:
`test_get_sessions_heals_stale_schema_store[archived]` is **upstream-NEW** (absent at base AND on
fork/main) and collides with a **fork-only** index: the fork's `DEFERRED_INDEX_SQL` adds
`idx_sessions_effective_last_active` / `idx_sessions_source_effective_last_active`, both of which
reference `archived`, so upstream's `ALTER TABLE sessions DROP COLUMN archived` fails where
upstream's own index set has no such reference. Verified identical on fork/main (which passes,
because it never runs upstream's test). Fork feature is load-bearing — pinned by
`test_session_list_denorm_acceptance.py:153/161/170`. **Left for an owner ruling** (drop-and-recreate
the two fork indexes inside the heal path vs xfail upstream's test); NOT resolved by weakening either
side.

### run81 files staged
| file | choice | result |
|---|---|---|
| `hermes_cli/web_routers/sessions.py` | B (offload re-graft onto upstream body) | 8 passed + off-loop proven |
| `tests/agent/test_model_metadata.py` | U (+3 restored, +1 constant) | 14F/74P -> **91 passed** |
| `tests/hermes_state/test_session_list_denorm_acceptance.py` | B (contract follows the split) | part of 20 passed |
| `hermes_state.py` | B (`unread` stamped in the shared tail) | **20 passed** |

**TREE at run81 exit:** MERGE_HEAD `1e5b5074` intact · **10 unmerged** (snapshot — a concurrent
writer is draining the desktop TS tail; 36 -> 15 -> 10 observed during this run) · 4875 staged ·
0 commits · no PR · `~/.hermes/runtime/hermes-agent` untouched (0 dirty).
Hermeticity PROVEN every run: `lsof ~/.hermes/state.db | grep -c python` = **0**.

### For the next runner
- Remaining tail is `apps/desktop/**` TS + `web/src/lib/gatewayClient.test.ts`. Needs `npm ci`
  (node_modules ABSENT) and `npx tsc --noEmit` — separate toolchain, do not start without the install.
- **Do the clean-ADD sweep.** Grep the merge for other upstream monolith-splits (new package dirs
  added with status `A`) and run the fork-authored-loss gate on the OLD file each one drained.
  `hermes_state` -> `hermes_state_{common,portability,schema,search}` is the other known split; its
  `test_hermes_state.py` 16 reds may be the same story.
- `tests/test_hermes_state.py` (16 reds) and the `archived`-index collision both need owners.

**NOT DONE. Do NOT merge.**


### run81 addendum — full-lane verification found 4 MORE, all the same clean-ADD class

After staging the first 4 files I ran the CANONICAL runner over the whole
hermes_cli/ + hermes_state/ lane (`scripts/run_tests.sh`, `HERMES_PYTHON` must point at
`~/.hermes/runtime/hermes-agent/.venv/bin/python` — the worktree has no .venv of its own and the
runner hard-fails without it). Result: **582 files, 4152 passed, 4 failed.** All four were real,
none were mine, and three were the SAME extraction-drift class as FINDING 1.

**1. `test_web_server_session_search.py` — a DECAPITATED stub + a genuinely LOST fork feature.**
Two failures, two different causes in one file:
- `search_sessions_by_id` stub: the merge took UPSTREAM's widened signature
  (`source`/`sources`/`exclude_sources`) but the FORK's body, which ends at `rows = [...]` with
  **no return** -> `TypeError: 'NoneType' object is not iterable`. Same decapitation shape as
  run80's FINDING 2. Restored upstream's filtering return.
- The title-ranking test then failed on `_FakeTitleSessionDB() takes no arguments` (upstream's
  `_open_session_db_at_path` now bootstraps via `SessionDB(db_path=..., read_only=False)`), and
  behind THAT sat a real loss: **upstream's extracted `search_sessions` has no title lane at all**
  (`search_sessions_by_title` refs — fork 1, upstream 0, merged 0), while
  `SessionDB.search_sessions_by_title` still exists at `hermes_state.py:9350`. This is the fork
  feature that makes "general discord" find that channel's sessions from the desktop — dropped by
  the same clean-ADD extraction. Re-grafted the title lane between the ID and content lanes.
  ⚠️ Note: `search_sessions_by_title` takes NO source kwargs (unlike `search_sessions_by_id`) —
  verified against the real signature before writing, so the rows are scoped in Python instead.
  RED-proven: neutralise the lane -> 1 failed; restore -> **2 passed**.

**2. `test_append_messages_batch.py` — stale monkeypatch stub vs a MERGE-AUTHORED parameter.**
`failing_insert()` got an unexpected kwarg `row_ids_out`. Provenance is interesting: `row_ids_out`
exists on **NEITHER** side (`hermes_state.py` refs — fork 0, upstream 0, merged 12) — a prior run
AUTHORED it to reconcile the two sides, and it is coherent and fully wired (`run_agent.py:2522`
consumes it to stamp interrupt-close markers). So the parameter is right and the stub is stale:
forwarded `row_ids_out` through rather than swallowing it, so the test still exercises the real
signature. **9 passed.**

**3. `test_config_read_guard.py` — upstream's NEW guard vs 3 fork-only plugin modules.**
Upstream added a source-scanning guard forbidding raw `yaml.safe_load` of config.yaml outside
allowlisted owner modules. It flagged 4 sites in 3 files that are **fork-only** (absent at base AND
upstream, so upstream never had to allowlist them): `plugins/blackbox/__init__.py`,
`plugins/blackbox/card.py`, `plugins/context_engine/lcm/config.py` x2. Fixed the PLUGINS, not the
guard — a raw read also bypasses the managed-scope overlay, so upstream's rule is right and this is
a latent fork bug the merge surfaced. blackbox -> `hermes_cli.config.load_config_readonly()` (the
convention 6 other fork plugins already follow); LCM -> its OWN `_load_hermes_config_yaml()` helper,
because LCM is vendored and deliberately supports a no-PyYAML fallback parser, so it must not import
hermes_cli. Behaviour proven identical (`raw-yaml block == loader block`, threshold 12.5,
skew_floor 0.25, max_nodes 4242 all resolve post-migration). **2 passed.**

**4. `test_hermes_state.py::test_compact_projection_tracks_schema`** — upstream widened
`_SESSION_COMPACT_EXCLUDED` to `{"system_prompt", "system_prompt_hash"}` (base/fork had only
`system_prompt`); the merge correctly took upstream's code, but the FORK's contract test sanctions
only its own two exclusions. That test exists to FORCE a conscious review when the excluded set
widens — so the review is: the hash rides with the blob and no list consumer renders it. Added
`system_prompt_hash` to the sanctioned set with that rationale.

**Also settled the `archived`-index collision** (previously flagged for an owner ruling — it did not
need one). Upstream's new `test_get_sessions_heals_stale_schema_store` does a bare
`ALTER TABLE sessions DROP COLUMN archived`, which fails on the FORK schema because two fork-only
indexes reference that column — it died in SETUP, so the heal under test never ran. Proved the
PRODUCTION heal path is fully correct first (damage the store faithfully -> reopen -> column AND
both indexes restored), then fixed the test's SETUP to drop dependent indexes first, exactly as the
fork's own `test_schema_rollback_drops_indexes_before_column` does. STRENGTHENED rather than
weakened: added assertions that the heal rebuilds both fork indexes. RED-proven by neutralising the
rebuild -> 3 failed; restored -> **3 passed**.

⚠️ **Runner note for the relay:** `scripts/run_tests.sh` needs
`HERMES_PYTHON=/Users/alexgierczyk/.hermes/runtime/hermes-agent/.venv/bin/python`. Do NOT write it
as `$HOME/...` in a command that also sets `HOME=$SB` — the shell expands `$HOME` to the sandbox
first and the runner then reports "no virtualenv with pytest found". Also: a run launched with a
plain backgrounded `nohup` from the agent's shell dies when the tool call returns; launch it with
`start_new_session=True` from Python and poll the log.

⚠️ **Contended-tree note:** an interleaved full-suite run reports transient ✗ on files that pass
standalone (`test_model_metadata.py` showed ✗ under contention, 91/91 green alone; the count went
✗4 -> ✗3 mid-run as retries settled). Confirm any suite-level failure standalone before charging it
to the merge.

### run81 FINAL verification — canonical runner, whole lane

```
scripts/run_tests.sh tests/hermes_cli/ tests/hermes_state/ tests/plugins/ \
    tests/test_hermes_state.py tests/agent/test_model_metadata.py \
    tests/test_web_server_sessiondb_eventloop.py tests/test_sqlite_lock_safe_inspection.py

=== Summary: 696 files, 6905 tests passed, 21 failed (100% complete) in 534.4s (64 workers) ===
```

The 21 across 2 files were run under the FULL-SUITE runner. Re-checked STANDALONE, per the
clustering/contention rule:

- `tests/plugins/memory/test_hindsight_provider.py` — reported 6 failed under the suite
  (`ModuleNotFoundError: hindsight_client_api`, prefetch-timeout asserts). Standalone: **57 passed**,
  reproduced **3x**. Pure contention/ordering artifact, NOT merge damage. Loss gate on that file is
  also clean: merged == upstream's 57 tests, fork-authored lost = **0** (fork/base both had 116; the
  73-test delta is upstream's own prune).
- `tests/test_hermes_state.py` — **15 failed / 511 passed** standalone (was 16 before my
  `system_prompt_hash` sanction fix). PRE-EXISTING, staged by a prior run, byte-identical failure set
  with my edits removed. Distinct clusters (display_metadata decode x7, get_resume_conversations x3,
  trigram-index x2, FTS cadence, compression-tip ordering, CompressionSessionClosedError). **Not
  charged to this run, NOT fixed inside the merge — needs its own pass.**

Everything run81 touched is green, each proven standalone:

| file | verification |
|---|---|
| `hermes_cli/web_routers/sessions.py` | eventloop 8✓ · session_search 2✓ (title lane RED-proven) · off-loop proven on all 4 handlers |
| `hermes_state.py` | denorm acceptance **20✓** (matches fork baseline exactly) |
| `tests/agent/test_model_metadata.py` | 14F/74P → **91✓** (also 91✓ inside the full run) |
| `tests/hermes_state/test_session_list_denorm_acceptance.py` | **20✓** |
| `tests/hermes_cli/test_web_server.py` | 1F/142P → **143✓** (heal RED-proven) |
| `tests/hermes_cli/test_web_server_session_search.py` | **2✓** (RED-proven) |
| `tests/hermes_state/test_append_messages_batch.py` | **9✓** |
| `tests/hermes_cli/test_web_server_host_header.py` | 6 errors → **6✓** (inherited; upstream also 6 errors) |
| `plugins/blackbox/{__init__,card}.py`, `plugins/context_engine/lcm/config.py` | config_read_guard **2✓** + behaviour parity proven |
| `tests/test_hermes_state.py` | compact-projection contract now green; 15 unrelated reds remain (pre-existing) |

**TREE at run81 exit:** MERGE_HEAD `1e5b5074` intact · **2 unmerged** (`agent/tool_executor.py`,
`hermes_cli/web_server.py`) · 4881 staged · **0 commits · no PR** · runtime tree 0 dirty ·
`lsof` prod state.db python = **0**. All touched files marker-free + `ast.parse` clean.

### For the next runner
1. `agent/tool_executor.py` (9 hunks) and `hermes_cli/web_server.py` (7) are the LAST two conflicts.
   `web_server.py` is already written marker-free by run80 and passes the fork-authored-loss gate
   **only after** the FINDING-1 offload re-graft — re-run that gate before staging it.
2. Then `tests/test_hermes_state.py`'s 15 reds (own pass, own card).
3. **Run the clean-ADD sweep before declaring the merge done.** Three of run81's five defects came
   from upstream RELOCATING code into new paths, where git produces NO conflict. Enumerate every
   status-`A` package dir the merge adds, and run the fork-authored-loss gate on the OLD file each
   one drained.

### run81 — CLEAN final verification (post dead-import cleanup)

Removed the imports my plugin migrations orphaned (`yaml`, `get_hermes_home`, `Path` in
`plugins/blackbox/__init__.py`; `Path` in `plugins/blackbox/card.py`) — confirmed dead by reference
count, then re-proved import + behaviour before re-running (12.5 / 12.5 / 0.25 / 4242, render_card
callable).

```
scripts/run_tests.sh tests/hermes_cli/ tests/hermes_state/ tests/plugins/ \
    tests/agent/test_model_metadata.py tests/test_hermes_state.py \
    tests/test_web_server_sessiondb_eventloop.py tests/test_sqlite_lock_safe_inspection.py

=== Summary: 696 files, 6911 tests passed, 15 failed (100% complete) in 405.9s (64 workers) ===
=== 1 file with test failures (15 tests failed) ===
  tests/test_hermes_state.py  (15 tests failed)
```

**ZERO collection errors** this run (the previous pass had 10 — all resolved by the `parents[2]`
sys.path fix and the concurrent writer landing the desktop tail). `test_hindsight_provider.py` is
green IN-SUITE now too, confirming the earlier 6 were contention, exactly as diagnosed.

Differential attribution on the one remaining red file, run by reverting BOTH run81 edits that touch
it and re-running the same file list:
```
WITHOUT run81 edits : 16 failed, 510 passed
WITH    run81 edits : 15 failed, 511 passed
caused BY run81     : NONE
fixed  BY run81     : test_compact_projection_tracks_schema
pre-existing        : 15
```
Those 15 are PRE-EXISTING in a file staged by a prior run — distinct clusters (display_metadata
decode x7, get_resume_conversations x3, trigram-index x2, FTS cadence, compression-tip ordering,
CompressionSessionClosedError). **Not charged to run81, NOT fixed inside the merge — own card.**

**FINAL TREE:** MERGE_HEAD `1e5b5074` intact · **2 unmerged** (`agent/tool_executor.py`,
`hermes_cli/web_server.py`) · 4881 staged · **0 commits · no PR** · runtime tree 0 dirty ·
`lsof` prod state.db python = **0** · all 13 touched files marker-free + `ast.parse` clean.
