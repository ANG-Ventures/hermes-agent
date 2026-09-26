# Tranche: agent — 240 rows

absorb = substantive added src lines found verbatim on upstream/main (hit/total); sym = added def/class names found. Signal only — read README §0.

| key | subsystem | kind | loc | files | conflict-files (syncs) | absorb src | sym | date | subject |
|---|---|---|---|---|---|---|---|---|---|
| #697 | other:hermes_state.py | code | 22162 | 9 | 3(3) | 0/0 | 0/0 | 2026-09-14 | test(state): split test_hermes_state.py so it fits the per-file timeout (#697) |
| #787 | agent | code | 5589 | 30 | 10(3) | 29/664 | 5/34 | 2026-09-23 | fix(usage): treat provider-declared null usage as UNKNOWN, never a measured 0 (#787) |
| #106 | agent | code | 5466 | 5 | 1(3) | 4/84 | 0/5 | 2026-06-26 | fix(compaction): reconcile in-turn stats via whole-tail sanitizer replay (#106) |
| nopr:036ae92ba1 | agent | code | 3213 | 13 | 5(3) | 35/593 | 4/37 | 2026-07-13 | feat(curator): bring skills-shared/ into curator scope + oversized-skill split |
| #673 | agent | code | 2078 | 13 | 1(2) | 25/295 | 28/31 | 2026-09-10 | fix(auth): preserve Codex refresh ownership and quarantine generations (#673) |
| #217 | other:hermes_state.py | code | 1704 | 11 | 9(3) | 20/46 | 2/2 | 2026-07-06 | Revert session.list denorm (Phase 1+2+fix) → back to safe CTE (#217) |
| #942 | agent | code | 1387 | 27 | 8(3) | 11/133 | 1/3 | 2026-09-24 | fix(agent): recover tool-call notices before empty response handling (#942) |
| #76 | agent | code | 1352 | 13 | 5(3) | 4/293 | 3/17 | 2026-06-21 | feat(compaction): granular reconciling compaction announce on all paths (#76) |
| #213 | other:hermes_state.py | code | 1231 | 6 | 5(3) | 41/224 | 3/23 | 2026-07-06 | feat(state): denormalize session.list recency (effective_last_active + two-stage query) (#213) |
| #220 | other:hermes_state.py | code | 1197 | 3 | 2(3) | 35/222 | 3/23 | 2026-07-07 | feat(state): gate session-list recency denorm (re-land behind default-off flag) (#220) |
| #136 | agent | code | 1190 | 15 | 6(3) | 15/120 | 2/9 | 2026-06-30 | fix(gateway): preserve-and-prompt on restart instead of silently skipping interrupted work (#136) |
| #597 | agent | code | 1143 | 11 | 7(3) | 22/266 | 3/22 | 2026-08-18 | fix(requests): preflight provider body byte limits (#597) |
| #541 | agent | code | 1118 | 8 | 5(3) | 11/195 | 1/19 | 2026-08-09 | feat(compaction): calibrate token-estimate skew per content class (#541) |
| #443 | other:run_agent.py | code | 1112 | 9 | 7(3) | 0/6 | 0/0 | 2026-07-26 | fix(state): maintain effective_last_active across import and message-flush paths (#443) |
| #671 | agent | code | 1047 | 5 | 1(1) | 3/46 | 0/1 | 2026-09-10 | fix(credential-pool): gate singleton seeding and stamp Anthropic producer (#671) |
| #764 | agent | code | 1015 | 11 | 3(3) | 6/92 | 0/3 | 2026-09-20 | feat(agent): consume the out-of-band hermes_confab_notice response extension (#764) |
| #978 | agent | code | 951 | 13 | 7(3) | 16/249 | 5/18 | 2026-09-24 | Blackbox: persist per-call cache tier and per-turn cache/compaction signals (#978) |
| #913 | agent | code | 942 | 6 | 3(3) | 12/107 | 1/12 | 2026-09-23 | feat(blackbox): C2 accumulator — every upstream call appends a turn_api_calls row with wire/pinned s |
| #621 | agent | code | 796 | 12 | 6(3) | 19/137 | 1/10 | 2026-08-20 | [verified] fix(failover): bound Tailscale transport outages (#621) |
| nopr:f20f0cc2cc | agent | code | 796 | 2 | 1(3) | 3/163 | 0/8 | 2026-07-16 | refactor(compaction): extract fork announce helpers |
| #1000 | agent | code | 784 | 8 | 2(3) | 11/165 | 5/18 | 2026-09-24 | fix(shell_hooks): absent hook files are an infrastructure failure — absence-only self-heal (t_1fb8de |
| #539 | other:hermes_state.py | code | 765 | 5 | 4(3) | 9/84 | 1/9 | 2026-08-09 | fix(compaction): key learned skew calibration by (provider, model), not session (#539) |
| #225 | agent | code | 757 | 4 | 2(3) | 4/96 | 1/4 | 2026-07-07 | feat(fallback): announce provider-only route changes + durable audit sink + reasoning-effort rider ( |
| #528 | agent | code | 717 | 25 | 7(3) | 11/147 | 0/4 | 2026-08-08 | fix(compression): honest /compress abort reporting, reachable fallback, live-route summarizer (#528) |
| #172 | agent | code | 635 | 20 | 5(3) | 6/94 | 0/0 | 2026-07-02 | fix(gateway): reconcile /compress feedback across both transcript axes (#172) |
| nopr:fba50537aa | agent | code | 633 | 6 | 2(3) | 2/62 | 0/4 | 2026-07-16 | Extract relay header fork extension |
| #445 | agent | code | 629 | 6 | 3(3) | 4/111 | 1/8 | 2026-07-26 | fix(gateway): hygiene compression deadline must not be tighter than the aux LLM timeout (#445) |
| #1001 | agent | code | 622 | 6 | 3(3) | 5/62 | 0/1 | 2026-09-24 | fix(agent): relay "no eligible sub" 503 = capacity; wait for a seat before a host move (t_9d25b5e1)  |
| #670 | agent | code | 608 | 3 | 1(2) | 5/73 | 0/2 | 2026-09-10 | fix(credential_pool): refuse stale singleton pairs in the xAI and Nous readers (#670) |
| #501 | agent | code | 604 | 4 | 1(3) | 6/128 | 1/5 | 2026-08-07 | fix(tokens): price documents per PAGE and audio per SECOND, not a flat constant (#501) |
| #341 | other:run_agent.py | code | 572 | 5 | 3(3) | 1/32 | 0/0 | 2026-07-14 | fix(persist): append-time generation gate — suppress a /stop'd turn's zombie writes (#341) |
| #63 | agent | code | 569 | 4 | 2(3) | 9/73 | 3/9 | 2026-06-20 | fix(stream): recover split-surrogate emoji instead of falling back to codex (#63) |
| #111 | agent | code | 558 | 9 | 7(3) | 4/82 | 0/7 | 2026-06-27 | feat(compaction): P2 compact-on-truth — calibrate the rough estimate by measured skew (#111) |
| #186 | other:hermes_state.py | code | 554 | 18 | 6(3) | 29/127 | 2/4 | 2026-07-03 | fix(desktop): server-side pinned sessions (#186) |
| #309 | agent | code | 551 | 3 | 1(3) | 2/5 | 0/0 | 2026-07-11 | fix(failover): thread the classified reason through all knowable failover sites (#309) |
| #21 | agent | code | 539 | 23 | 5(3) | 10/148 | 1/5 | 2026-06-08 | feat(telemetry): real per-request composition snapshot for /context /usage /compress (#21) |
| #768 | other:hermes_test_context.py | code | 538 | 5 | 2(3) | 16/77 | 4/6 | 2026-09-21 | fix(managed-scope): resolve the system scope through the one session-wide pytest predicate (#768) |
| #578 | other:hermes_state.py | code | 534 | 2 | 1(3) | 9/40 | 0/0 | 2026-08-11 | fix(sessions): make archiving retire the routing entry, not just flip a flag (#578) |
| #467 | agent | code | 522 | 5 | 3(3) | 0/30 | 0/1 | 2026-08-06 | fix(gateway+fallback): stale status adapter after reconnect; stop effort clobber on failover; honest |
| #530 | agent | code | 517 | 7 | 5(3) | 2/72 | 1/3 | 2026-08-08 | fix(failover): classify corrupt response bodies + name the exhausted quota window (#530) |
| #898 | agent | code | 513 | 13 | 5(3) | 8/41 | 0/1 | 2026-09-23 | feat(openai): add GPT-6 Sol + Luna baseline support (900K eligible, pricing, picker) (#898) |
| #84 | agent | code | 510 | 6 | 1(3) | 2/41 | 0/1 | 2026-06-22 | fix(compaction): fix silent granular-announce degrade on LCM hygiene + limit-vs-count clause (#84) |
| #100 | agent | code | 509 | 8 | 2(3) | 0/53 | 1/4 | 2026-06-22 | feat(compaction): structural summary tagging (_lcm_summary) + degrade observability (#100) |
| #110 | agent | code | 503 | 5 | 1(3) | 0/50 | 0/2 | 2026-06-27 | feat(compaction): Option B — provenance-stamped exact in-turn kept partition (#110) |
| #178 | agent | code | 500 | 6 | 1(3) | 0/34 | 0/1 | 2026-07-02 | feat(compaction): multi-pass provenance stamp, shadow-first (Option B extension) (#178) |
| #177 | agent | code | 492 | 2 | 1(3) | 1/49 | 0/1 | 2026-07-02 | fix(compaction): gate in-turn stats on announce render-eligibility + self-identifying markers (#177) |
| #37 | agent | code | 492 | 5 | 3(3) | 13/65 | 0/3 | 2026-06-20 | feat(budget): deny-by-default side-effect lockout on the grace turn (Guard D-core) (#37) |
| #610 | agent | code | 491 | 5 | 2(3) | 10/88 | 6/9 | 2026-08-19 | fix(anthropic): repair split surrogates in tool-input JSON deltas before the SDK accumulator (#610) |
| #109 | agent | code | 487 | 5 | 1(3) | 0/83 | 0/3 | 2026-06-27 | fix(compaction): A-floor exhaustive partition reconciles in-turn granular announce (PR #106 residual |
| nopr:02a357eaf5 | agent | code | 479 | 2 | 1(3) | 7/88 | 1/6 | 2026-06-20 | feat(compaction-announce): pure snippet extractor + engine-aware formatter + deduped emitter |
| #101 | agent | code | 478 | 5 | 0(0) | 0/25 | 0/2 | 2026-06-23 | fix(compaction): reconcile hygiene stats when LCM sanitizes the kept tail (#101) |
| #1010 | agent | code | 471 | 3 | 2(3) | 20/114 | 0/4 | 2026-09-24 | fix(blackbox): account billed HTTP-200s the loop rejects into turn totals (I4 class 2) (#1010) |
| #602 | agent | code | 470 | 13 | 10(3) | 13/81 | 2/5 | 2026-08-18 | fix(agent): expire prior-turn multimodal payloads (#602) |
| #81 | agent | code | 446 | 4 | 1(3) | 0/66 | 0/2 | 2026-06-21 | feat(compaction): tool-result sub-split in the granular announce (both paths) (#81) |
| nopr:7ac18ba0de | other:hermes_state.py | code | 443 | 7 | 2(3) | 1/37 | 0/6 | 2026-07-16 | refactor: extract pure hermes state helpers |
| #841 | agent | code | 435 | 6 | 1(1) | 0/64 | 1/2 | 2026-09-22 | fix(hooks): close two residual secret-disclosure channels to the model (#841) |
| #337 | agent | code | 435 | 20 | 2(3) | 7/42 | 0/1 | 2026-07-14 | fix(stop): honor /stop in the fallback loop + honest "Stopping" wording (#337) |
| #227 | agent | code | 434 | 9 | 5(3) | 4/86 | 1/4 | 2026-07-07 | fix(failover): don't mask a credential-pool rate-limit as a non-retryable keyless-client abort (#227 |
| nopr:3c413ca764 | agent | code | 426 | 5 | 1(2) | 0/5 | 0/1 | 2026-07-10 | test(gateway): RC3 executor-residue gates + tool-submit context-mismatch net |
| #876 | agent | code | 419 | 3 | 1(3) | 4/75 | 0/4 | 2026-09-22 | feat(compression): one measurable duration line per summariser call (#876) |
| #196 | agent | code | 419 | 2 | 2(2) | 3/52 | 0/0 | 2026-07-05 | fix(replay): repair mid-history unanswered tool_use before send, not just the dangling tail (#196) |
| nopr:29886643e0 | agent | code | 413 | 5 | 3(3) | 1/7 | 0/0 | 2026-07-16 | refactor(fallback): layer per-entry reasoning_effort on upstream chokepoint; fix mcp-discovery test  |
| #669 | agent | code | 395 | 7 | 3(3) | 1/22 | 0/1 | 2026-09-10 | fix(credential_pool): refuse cross-account + stale Codex singleton adoption; one shared account-id a |
| #701 | agent | code | 391 | 4 | 2(3) | 7/67 | 0/2 | 2026-09-16 | fix(retry): don't honor a rate-limit Retry-After on a pool-exhausted seat (#701) |
| #311 | agent | code | 384 | 4 | 1(2) | 13/92 | 2/7 | 2026-07-12 | feat(pricing): pluggable external pricing source, default models.dev (#311) |
| #506 | agent | code | 383 | 6 | 3(3) | 3/55 | 0/2 | 2026-08-08 | fix(compaction): stop compacting sessions that are nowhere near full (#506) |
| nopr:1921a02047 | other:hermes_state.py | code | 383 | 4 | 3(3) | 7/47 | 1/7 | 2026-07-10 | fix(compression): persist P2 skew calibration across process restarts |
| #103 | agent | code | 381 | 2 | 1(3) | 2/33 | 0/1 | 2026-06-25 | fix(auxiliary): thread the dropped timeout + route-scope max_retries=0 for compaction (#103) |
| #303 | other:hermes_state.py | code | 368 | 7 | 3(3) | 1/128 | 2/2 | 2026-07-11 | feat(search): platform + channel/thread name matching in session search (#303) |
| #730 | agent | code | 344 | 3 | 0(0) | 5/30 | 0/1 | 2026-09-19 | credential pool: adopt the rotated token when the agent's stale key matches no entry (#730) |
| #563 | agent | code | 344 | 3 | 1(3) | 4/51 | 5/6 | 2026-08-10 | fix(compression): a shipped DEFAULT is not an operator's explicit value (#528 was inert) (#563) |
| #503 | agent | code | 328 | 4 | 2(3) | 1/47 | 0/1 | 2026-08-08 | fix(compaction): show BOTH token counters when they disagree (#503) |
| #917 | agent | code | 327 | 2 | 1(2) | 0/31 | 0/2 | 2026-09-23 | fix(anthropic): guard the two Opus 5.5 request-shape breaking changes (#917) |
| nopr:255a4c4546 | agent | code | 320 | 4 | 2(3) | 14/68 | 1/8 | 2026-07-16 | Extract tool gate fork helpers |
| #625 | other:hermes_state.py | code | 319 | 2 | 1(3) | 6/15 | 0/0 | 2026-08-20 | fix(state): unlocked reads on the shared writer connection destroy turns as session_persistence_fail |
| #257 | other:hermes_state.py | code | 315 | 12 | 5(3) | 15/78 | 1/2 | 2026-07-10 | feat(search): rank session-title matches above content hits in desktop search (#257) |
| #656 | agent | code | 314 | 4 | 0(0) | 1/21 | 0/0 | 2026-09-09 | fix(hooks): deny failed security hooks before tool execution (#656) |
| #2 | other:hermes_state.py | code | 313 | 5 | 4(3) | 4/52 | 2/3 | 2026-06-02 | feat(usage): persist last-turn token snapshot, survive agent eviction (#2) |
| #511 | agent | code | 312 | 2 | 0(0) | 1/46 | 0/2 | 2026-08-08 | fix(compaction): a pass that compacted nothing must not say it did (#511) |
| #342 | agent | code | 300 | 8 | 3(3) | 6/59 | 0/2 | 2026-07-14 | fix(blackbox): price MoA turns by physical model routes (#342) |
| #1052 | agent | code | 297 | 6 | 3(3) | 4/27 | 0/1 | 2026-09-25 | fix(redact): redact JSON per leaf so a masked ENV value cannot eat the closing quote (t_d59ca5db) (# |
| #658 | agent | code | 290 | 3 | 1(3) | 4/22 | 0/1 | 2026-09-09 | fix(kanban): lifecycle enforcer honors the originating run's terminal outcome (#658) |
| #1019 | agent | code | 286 | 6 | 1(2) | 8/55 | 4/10 | 2026-09-24 | feat(lsp): per-host cap on running language servers (lsp.max_servers_per_host, default 3) (#1019) |
| #392 | agent | code | 283 | 5 | 3(3) | 2/43 | 0/2 | 2026-07-18 | fix(compaction): cold-start trigger-skew prior prevents empty-history false-fire (#392) |
| #459 | agent | code | 281 | 4 | 3(3) | 1/16 | 0/1 | 2026-07-27 | fix(delegate): stamp subagent turns with their real custom-provider lane (#459) |
| #686 | agent | code | 273 | 5 | 1(3) | 0/27 | 0/0 | 2026-09-13 | fix(compaction): make the auto banner agree with the runtime footer (#686) |
| #310 | agent | code | 270 | 3 | 2(3) | 10/48 | 0/1 | 2026-07-12 | fix(context): resolve real window for aggregator providers via cross-provider models.dev lookup (#31 |
| #676 | agent | code | 260 | 6 | 4(3) | 1/13 | 1/1 | 2026-09-11 | fix: display Codex safety refusals without changing recovery policy (#676) |
| #605 | agent | code | 256 | 7 | 5(3) | 2/19 | 1/2 | 2026-08-18 | fix(stream): fail over malformed provider responses (#605) |
| #206 | agent | code | 256 | 2 | 1(3) | 0/57 | 0/4 | 2026-07-05 | feat(relay): stamp x-hermes-lane on pool main-turn requests (Phase 2 lanes) (#206) |
| #891 | other:hermes_state.py | code | 256 | 2 | 1(3) | 0/5 | 0/0 | 2026-09-23 | fix(state): the sqlite3 salvage remedy must survive the shell when pasted (#891) |
| #495 | agent | code | 253 | 4 | 3(3) | 0/24 | 0/1 | 2026-08-08 | feat(providers): add ProviderProfile.process_response_text hook (#495) |
| #164 | agent | code | 253 | 3 | 0(0) | 0/29 | 0/1 | 2026-07-01 | refactor: consolidate compaction signature partitioning (#164) |
| #573 | other:run_agent.py | code | 246 | 5 | 3(3) | 1/33 | 1/3 | 2026-08-10 | fix(session): stop blaming a full disk for a restart-interrupted turn (#573) |
| #554 | agent | code | 243 | 2 | 1(2) | 2/21 | 1/1 | 2026-08-10 | fix(compaction): plugin engines never received the session store, making skew persistence inert (#55 |
| #418 | other:hermes_state.py | code | 243 | 2 | 1(3) | 0/5 | 0/0 | 2026-07-24 | fix: maintain session denorm across parent rewrites (#418) |
| #893 | agent | code | 241 | 2 | 0(0) | 1/43 | 0/2 | 2026-09-23 | fix(hooks): a crashed fail_closed hook must not be worded like a policy denial (t_a53d38ad) (#893) |
| #99 | agent | code | 241 | 2 | 0(0) | 3/31 | 1/2 | 2026-06-22 | fix(compaction): harden _content_to_text for structured summary content (#99) |
| #512 | agent | code | 238 | 2 | 1(3) | 0/7 | 0/0 | 2026-08-08 | fix(tokens): compose_request_breakdown double-billed document + audio base64 (#512) |
| nopr:4972c37aa9 | agent | code | 238 | 4 | 4(3) | 1/33 | 0/2 | 2026-06-19 | feat(fallback): announce model fallback in chat, always-emitted + deduped (Phase A) |
| #680 | agent | code | 235 | 8 | 3(3) | 7/31 | 0/0 | 2026-09-12 | fix: preserve compression refusal details and resolved route diagnostics (#680) |
| #300 | agent | code | 233 | 5 | 4(3) | 6/45 | 0/0 | 2026-07-11 | feat(delegation): compact skill index for subagents + per-task skill promotion (#300) |
| #623 | agent | code | 232 | 4 | 3(3) | 1/8 | 0/0 | 2026-08-20 | fix(classifier): pool read-phase 504 fails over on first hit instead of burning 3 retries (#623) |
| #576 | agent | code | 231 | 3 | 2(3) | 0/34 | 0/3 | 2026-08-11 | fix(gateway): the restart drain must wait for an in-flight compaction (#576) |
| #7 | agent | code | 228 | 3 | 2(3) | 4/20 | 0/1 | 2026-06-04 | Notional pricing for subscription proxies/bridges + Codex (#7) |
| #253 | other:hermes_state.py | code | 228 | 4 | 4(3) | 9/39 | 0/1 | 2026-07-10 | feat(session-store): config gate to disable the trigram FTS index (#253) |
| nopr:5c0ecedb4b | agent | code | 227 | 3 | 2(3) | 1/4 | 0/0 | 2026-06-16 | fix(fallback): re-sync auxiliary-routing runtime globals on failover + restore |
| #739 | agent | code | 226 | 5 | 4(3) | 3/54 | 0/1 | 2026-09-20 | fix(classifier): a Claude Code CLI usage cap is a quota, not a connection issue (#739) |
| #679 | agent | code | 226 | 4 | 2(3) | 2/21 | 0/1 | 2026-09-11 | fix(compression): inherit main fallback policy on watchdog expiry (#679) |
| #535 | agent | code | 223 | 3 | 0(0) | 4/11 | 0/1 | 2026-08-09 | [verified] fix(relay): sanitize split surrogates before stream encoding (#535) |
| #681 | agent | code | 221 | 20 | 3(3) | 2/59 | 0/1 | 2026-09-12 | fix(compression): derive the total ceiling so a stall-fallback can finish (#681) |
| #65 | other:hermes_undo.py | code | 220 | 5 | 3(3) | 1/20 | 1/2 | 2026-06-20 | fix(undo): preserve partial /redo progress, bound _states, public redo_count bump, clamp /redo (#65) |
| #997 | agent | code | 217 | 2 | 0(0) | 1/60 | 2/7 | 2026-09-25 | agent: output_split -- shared finished/unfinished output producer (pure move from tokens-ace) (#997) |
| #260 | agent | code | 216 | 3 | 1(3) | 2/23 | 0/1 | 2026-07-10 | fix(model-metadata): don't misdetect an OpenAI-compat proxy as LM Studio (#260) |
| #529 | agent | code | 215 | 3 | 1(2) | 0/11 | 0/0 | 2026-08-08 | fix(compaction): persisted skew calibration must survive a restart (#529) |
| #533 | agent | code | 213 | 2 | 1(3) | 0/11 | 0/0 | 2026-08-08 | fix(failover): honor an identified 7d quota window in the primary cooldown (#533) |
| #27 | agent | code | 208 | 3 | 1(3) | 2/37 | 0/0 | 2026-06-09 | fix(codex): add progress-stall watchdog for keepalive-only Codex hangs (#27) |
| #1054 | other:hermes_state.py | code | 208 | 4 | 3(3) | 1/31 | 0/3 | 2026-09-25 | fix(state): one active tool result per tool_call_id across in-place compaction (#1054) |
| #505 | agent | code | 207 | 4 | 3(2) | 1/23 | 1/2 | 2026-08-08 | feat(anthropic): gate fine-grained-tool-streaming beta behind a config knob (#505) |
| #237 | other:hermes_state.py | code | 207 | 3 | 2(3) | 5/46 | 0/2 | 2026-07-08 | feat(cli): nudge `chat -c` when the last turn was interrupted mid-flight (#237) |
| #1015 | agent | code | 204 | 2 | 1(3) | 1/8 | 0/0 | 2026-09-24 | fix(agent): iteration-limit summary strips display_kind/display_metadata (#1015) |
| #222 | agent | code | 203 | 4 | 1(2) | 1/51 | 0/3 | 2026-07-07 | feat(acp): route Claude ACP adapters through the Claude Relay pool (#222) |
| #223 | agent | code | 202 | 3 | 1(3) | 3/38 | 0/1 | 2026-07-07 | fix(retry): honor Retry-After on provider overload (503/529), not just rate-limit (#223) |
| #262 | agent | code | 197 | 2 | 1(2) | 2/27 | 0/1 | 2026-07-10 | feat(pricing): price xAI Grok models (xai-oauth notional + metered xai) (#262) |
| nopr:a8e443aad1 | agent | code | 197 | 4 | 4(3) | 2/36 | 1/1 | 2026-06-19 | fix(compression): re-resolve per-model threshold on model switch/fallback (Phase B) |
| #207 | agent | code | 194 | 2 | 1(2) | 1/35 | 0/2 | 2026-07-05 | feat(pricing): price gemini-bridge (Google AI Ultra sub) turns in tokens.ace (#207) |
| #596 | agent | code | 193 | 4 | 3(3) | 1/19 | 0/1 | 2026-08-18 | fix(failover): name the model and the scope on a pool-exhaustion fallback (#596) |
| #36 | agent | code | 191 | 6 | 3(3) | 0/4 | 0/0 | 2026-06-12 | fix(bridge-routing): isolate background-review fork CLI session (#36) |
| #176 | agent | code | 189 | 4 | 3(3) | 0/29 | 0/0 | 2026-07-02 | feat(gateway): wire-first /compress feedback — real measured tokens lead, one number story (#176) |
| #195 | agent | code | 188 | 4 | 4(3) | 1/25 | 0/0 | 2026-07-05 | fix(failover): don't cascade a malformed-conversation 400 across every provider (#195) |
| #43 | agent | code | 183 | 2 | 1(3) | 1/25 | 0/3 | 2026-06-15 | fix(blackbox): two-tier char->token divisor (fixed=4.0, non-fixed=3.5) (#43) |
| #674 | agent | code | 181 | 13 | 5(3) | 0/27 | 0/0 | 2026-09-10 | fix(notices): chat-facing warning glyphs use emoji presentation (#674) |
| nopr:b62cb039ed | agent | code | 177 | 3 | 2(3) | 1/38 | 0/1 | 2026-06-19 | fix(compression): anti-thrash counter via request-level effectiveness (Phase C) |
| #500 | agent | code | 176 | 2 | 1(1) | 0/2 | 0/0 | 2026-08-07 | fix(kanban): background-review forks must not fail the parent's task (#500) |
| #174 | agent | code | 176 | 4 | 3(3) | 0/23 | 0/0 | 2026-07-02 | fix(gateway): label manual /compress granular block by its stored-transcript basis (#174) |
| nopr:14b186d6fa | other:hermes_undo.py | code | 173 | 4 | 3(3) | 3/40 | 2/4 | 2026-07-07 | feat(gateway): show new-tail preview after /undo and /redo |
| #709 | agent | code | 172 | 2 | 0(0) | 0/31 | 0/1 | 2026-09-18 | fix(kanban): stop-nudge must check WHO owns the card, not just whether the tool exists (#709) |
| #604 | agent | code | 171 | 5 | 3(3) | 5/39 | 0/1 | 2026-08-18 | fix(requests): distinguish byte-413 remediation telemetry (#604) |
| #280 | agent | code | 170 | 3 | 2(3) | 0/26 | 0/1 | 2026-07-10 | fix(fallback): name WHY a route changed on stream-drop failovers (#280) |
| #236 | agent | code | 168 | 4 | 3(3) | 0/5 | 0/0 | 2026-07-08 | feat(fallback): model.auto_recovery toggle + sticky cooldown on safety-refusal (#236) |
| #910 | agent | code | 167 | 7 | 5(3) | 1/15 | 0/0 | 2026-09-23 | feat(anthropic): add claude-opus-5-5 baseline support (metadata, pricing, picker) (#910) |
| nopr:85ee6f0f12 | agent | code | 167 | 3 | 2(2) | 1/5 | 0/0 | 2026-07-25 | test(anthropic): class-level invariant — no conversion path may emit a blank text block |
| #29 | agent | code | 166 | 7 | 4(3) | 1/34 | 0/1 | 2026-06-09 | feat(telemetry): split system prompt into identity+skills, add per-message framing (#29) |
| #127 | agent | code | 164 | 3 | 2(2) | 2/15 | 0/0 | 2026-06-30 | fix(mem0): bg-review now actually CALLS mem0_remember (was suppressed by contradictory prompt) (#127 |
| #21256 | agent | code | 162 | 4 | 4(3) | 2/22 | 0/0 | 2026-06-08 | feat(config): per-entry reasoning_effort in fallback_model chain (#21256) |
| #630 | agent | code | 160 | 8 | 2(3) | 0/8 | 0/0 | 2026-08-20 | fix(agent): full compaction trigger-attribution coverage across all surfaces (#630) |
| #25 | agent | code | 159 | 2 | 1(2) | 1/27 | 0/1 | 2026-06-09 | feat(compression): config-driven per-model threshold (#25) |
| #95 | agent | code | 158 | 2 | 0(0) | 6/31 | 1/3 | 2026-06-22 | fix(compaction): render granular in-turn announce on list-content messages (#95) |
| nopr:b56024aa7a | agent | code | 157 | 3 | 2(3) | 0/18 | 1/1 | 2026-06-20 | feat(compaction-announce): separable loud-fail markers + call-site coverage (Phase 6) |
| nopr:c7d0795102 | agent | code | 157 | 3 | 2(3) | 0/24 | 0/1 | 2026-06-20 | feat(compaction-announce): record fallback event + turn-scoped post-fallback linkage |
| #502 | agent | code | 156 | 7 | 6(3) | 1/13 | 0/0 | 2026-08-07 | fix(compaction): every compaction names the arm that fired it (#502) |
| nopr:e7d2b9f1bd | agent | code | 156 | 3 | 0(0) | 0/21 | 1/1 | 2026-07-25 | fix(curator): scope the shared-tree clean-tree gate to the paths a pass touches |
| nopr:d97b1070f7 | agent | code | 152 | 11 | 8(3) | 0/16 | 0/0 | 2026-07-10 | fix(parity): reconcile remaining full-suite CI reds (slices 1-4, 9) |
| #446 | other:hermes_state.py | code | 151 | 2 | 2(3) | 0/11 | 0/0 | 2026-07-26 | fix(state): skip the trigram sweep in _fts_rebuild_finish when the trigram index is unavailable (#44 |
| nopr:2870fd4994 | agent | code | 142 | 2 | 1(3) | 3/23 | 0/0 | 2026-06-20 | feat(compaction-announce): emit at the engine-agnostic compression done-site |
| nopr:7188575694 | agent | code | 142 | 2 | 1(3) | 0/13 | 0/0 | 2026-06-04 | fix(codex): fire no-byte TTFB watchdog before the stale timer |
| #504 | agent | code | 141 | 2 | 1(2) | 0/14 | 0/0 | 2026-08-08 | feat(background-review): raise iteration ceiling 16 -> 30 and make exhaustion LOUD (#504) |
| #518 | agent | code | 137 | 2 | 0(0) | 2/33 | 0/1 | 2026-08-08 | fix(memory): strip gateway harness metadata before the provider fan-out (#518) |
| nopr:a557064b09 | agent | code | 134 | 2 | 2(3) | 3/45 | 0/2 | 2026-07-08 | fix(compaction): harden summarizer explicit-override paths on model swap (A4 axis B) |
| nopr:75b1b49ca7 | agent | code | 133 | 3 | 1(1) | 1/21 | 0/1 | 2026-06-21 | fix(memory): re-index provider routing after initialize() so config-gated tools are callable |
| #54 | agent | code | 132 | 2 | 1(2) | 0/9 | 0/1 | 2026-06-17 | fix(pricing): pattern-match notional-Anthropic -fN failover lanes + claude-pool (#54) |
| #1047 | agent | code | 130 | 4 | 3(3) | 1/3 | 0/0 | 2026-09-25 | fix(kanban): persist the stop-guard's assistant candidate; strip only the nudge (t_4eeb0202) (#1047) |
| #242 | agent | code | 126 | 4 | 2(3) | 0/2 | 0/0 | 2026-07-08 | fix(pricing+relay): finish claude-app→claude-apr rename — LIVE pools were unpriced (#242) |
| #608 | agent | code | 122 | 4 | 2(3) | 6/24 | 1/1 | 2026-08-18 | fix: classify malformed final stream messages (#608) |
| #402 | agent | code | 122 | 3 | 2(3) | 2/27 | 0/1 | 2026-07-19 | fix(compaction): announce shows session-truthful reasoning effort, not global config default (#402) |
| #317 | agent | code | 120 | 3 | 3(3) | 0/5 | 0/0 | 2026-07-12 | fix(model): route-change announce silently suppressed after cooldown-pin (#317) |
| #56 | agent | code | 120 | 2 | 1(3) | 0/4 | 0/0 | 2026-06-17 | fix(auxiliary): resolve aux provider+model as a matched pair in _resolve_auto (#56) |
| #31 | agent | code | 120 | 2 | 1(3) | 1/21 | 0/1 | 2026-06-11 | fix(telemetry): recompute skills-index split on gateway restore path (#31) |
| #179 | other:hermes_state.py | code | 120 | 4 | 4(3) | 7/21 | 0/3 | 2026-07-02 | fix(dashboard): offload session stats hot read (#179) |
| #457 | agent | code | 118 | 2 | 2(2) | 0/2 | 0/0 | 2026-07-27 | fix(redact): eliminate catastrophic backtracking in _CFG_DOTTED_RE (#457) |
| #327 | agent | code | 118 | 2 | 2(3) | 0/11 | 0/0 | 2026-07-14 | fix(context): self-heal stale aggregator cache entries (#327) |
| nopr:bf30e92673 | agent | code | 118 | 2 | 1(3) | 0/18 | 0/1 | 2026-06-20 | feat(compaction-announce): trigger_reason + value clause in formatter (Phase 1) |
| #406 | agent | code | 116 | 2 | 0(0) | 0/7 | 0/0 | 2026-07-19 | fix(compaction): quoted summary text must not classify as a summary row (TAG_MISSING false-fire clas |
| #958 | other:hermes_state.py | code | 114 | 4 | 1(3) | 3/13 | 0/0 | 2026-09-24 | fix: retain session prompt across route metadata writes (#958) |
| #699 | agent | code | 112 | 2 | 0(0) | 1/10 | 0/1 | 2026-09-15 | fix(curator): count on-disk skills across the entire run (#699) |
| #324 | agent | code | 110 | 2 | 2(1) | 5/19 | 0/1 | 2026-07-13 | fix(verification): honor a leading `cd <dir> &&` when keying evidence to a root (#324) |
| #1025 | agent | code | 108 | 3 | 2(2) | 0/11 | 0/1 | 2026-09-25 | fix(redact): stop masking PASS=<n> test counters beside a FAIL counter (#1025) |
| #517 | agent | code | 108 | 3 | 3(3) | 1/9 | 0/0 | 2026-08-08 | fix(failover): entitlement 403 announces '(account blocked)', not '(auth refresh)' (#517) |
| #708 | agent | code | 106 | 3 | 1(3) | 3/26 | 1/2 | 2026-09-18 | fix(kanban): skip the stop-nudge when the session exposes no kanban terminal tool (#708) |
| #205 | agent | code | 102 | 2 | 1(3) | 2/25 | 0/1 | 2026-07-05 | feat(relay): stamp x-hermes-session on pool requests for reset-weighted router affinity (#205) |
| #649 | agent | code | 101 | 9 | 4(3) | 3/18 | 0/0 | 2026-09-04 | feat(models): add gpt-6-astra to catalogs, metadata and pricing (#649) |
| #469 | agent | code | 100 | 4 | 3(3) | 1/6 | 0/0 | 2026-08-06 | fix(failover): honest label for multi-sub pool exhaustion ("no eligible sub") (#469) |
| nopr:ad8de4f275 | agent | code | 100 | 4 | 2(3) | 0/7 | 0/1 | 2026-07-16 | Tighten tool gate extraction budget |
| #973 | agent | code | 98 | 2 | 1(3) | 2/6 | 0/0 | 2026-09-24 | fix(agent): iteration-limit summary carries the profile's session routing user (#973) |
| #23 | agent | code | 94 | 3 | 2(3) | 1/7 | 0/1 | 2026-06-09 | feat(telemetry): tunable chars-per-token divisor for composition estimate (default 3.5) (#23) |
| #547 | agent | code | 93 | 2 | 2(2) | 0/3 | 0/0 | 2026-08-09 | fix(repair): Pass 1.5 must match tool results on call_id, not just id (#547) |
| #806 | other:hermes_state.py | code | 93 | 2 | 1(3) | 0/16 | 0/0 | 2026-09-21 | fix(state): back off cross-process turn-lease wait notices (#806) |
| #48 | agent | code | 92 | 2 | 1(2) | 0/5 | 0/0 | 2026-06-15 | fix(pricing): bill cache-write at input rate when no cache-write rate published (#48) |
| #609 | agent | code | 89 | 2 | 0(0) | 1/6 | 0/0 | 2026-08-18 | fix(requests): inherit APR/APX body limits (#609) |
| #559 | agent | code | 84 | 2 | 2(1) | 0/4 | 0/0 | 2026-08-10 | fix(verification): recognize env options before test commands (#559) |
| #878 | agent | code | 81 | 8 | 5(3) | 1/11 | 0/0 | 2026-09-22 | feat(models): add grok-4.7 as the xAI flagship (catalog, pricing, 500K window) (#878) |
| #45 | agent | code | 81 | 2 | 1(3) | 0/0 | 0/0 | 2026-06-15 | fix(blackbox): tune fixed char->token divisor 4.0 -> 4.2 (post-deploy gate) (#45) |
| nopr:67d79e812d | other:hermes_state.py | code | 80 | 1 | 1(3) | 12/32 | 1/3 | 2026-06-14 | state: survive missing trigram tokenizer (FTS5 present, trigram absent) |
| nopr:17910311e6 | other:hermes_undo.py | code | 80 | 3 | 2(3) | 0/9 | 0/0 | 2026-07-08 | fix(gateway): harden /undo-/redo tail preview (Greptile #224 P1s) |
| #263 | agent | code | 79 | 2 | 1(3) | 0/9 | 0/0 | 2026-07-10 | fix(model-metadata): local ctx probe must not read max_tokens as the context window (#263) |
| #55 | agent | code | 74 | 2 | 1(2) | 0/10 | 0/1 | 2026-06-17 | fix(pricing): fall back to base snapshot for dated Anthropic model ids (#55) |
| #722 | agent | code | 73 | 2 | 0(0) | 1/2 | 0/0 | 2026-09-19 | fix(transports): read cache_creation_tokens from Anthropic-backed OpenAI bridges (#722) |
| #61 | agent | code | 73 | 2 | 1(3) | 0/1 | 0/0 | 2026-06-18 | fix(auxiliary): keep auto model resolved from runtime pair (#61) |
| #600 | agent | code | 71 | 2 | 0(0) | 0/8 | 0/0 | 2026-08-18 | fix(monitoring): make a total OTLP span-export outage visible (#600) |
| nopr:a0eec7dd47 | agent | code | 67 | 4 | 4(3) | 10/20 | 2/3 | 2026-06-08 | fix: honor provider retry-after during fallback cooldown |
| #208 | agent | code | 66 | 2 | 1(2) | 0/2 | 0/0 | 2026-07-05 | fix(pricing): stop gemini-bridge unsupported models leaking to vendor rates (#208) |
| #11 | agent | code | 66 | 2 | 1(3) | 0/2 | 0/0 | 2026-06-04 | feat(log): tag Turn-ended diag line with cron task id (task=) (#11) |
| #24 | agent | code | 65 | 3 | 3(3) | 0/13 | 0/1 | 2026-06-09 | feat(telemetry): unify ALL rough token estimators on the 3.5 divisor (#24) |
| nopr:78ce24d1db | agent | code | 65 | 2 | 1(3) | 0/4 | 0/0 | 2026-05-28 | fix(auxiliary_client): consult ProviderProfile.api_mode in resolver |
| #12 | agent | code | 64 | 2 | 1(2) | 0/3 | 0/0 | 2026-06-04 | fix(blackbox): background_review fork inherits parent chat context (#12) |
| #496 | agent | code | 63 | 2 | 2(3) | 0/0 | 0/0 | 2026-08-07 | fix(fallback): stop announcing one failover hop as two chat messages (#496) |
| #464 | agent | code | 62 | 2 | 2(3) | 1/1 | 0/0 | 2026-07-30 | fix(models): add claude-opus-5 to DEFAULT_CONTEXT_LENGTHS (was resolving at 200K, not 1M) (#464) |
| #959 | agent | code | 57 | 2 | 1(2) | 2/10 | 0/0 | 2026-09-24 | fix(pricing): pin GPT-5.5 official list rate and context tier (#959) |
| #161 | agent | code | 56 | 2 | 1(3) | 0/13 | 0/1 | 2026-07-01 | fix(compaction): render APPROX_ATTRIBUTION kept_tail/pre ratio truthfully (>100% basis artifact) (#1 |
| #32 | agent | code | 51 | 5 | 2(3) | 0/7 | 0/0 | 2026-06-11 | feat(telemetry): show skill count on /context Skill index line (#32) |
| #226 | agent | code | 50 | 2 | 1(2) | 0/0 | 0/0 | 2026-07-07 | feat(pricing): price Yunwu (云雾) claude-* turns at the Anthropic snapshot (#226) |
| #416 | agent | code | 49 | 2 | 0(0) | 2/4 | 0/0 | 2026-07-23 | fix(vision): consult provider profile supports_vision when models.dev misses (#416) |
| #419 | agent | code | 48 | 3 | 3(3) | 2/15 | 0/0 | 2026-07-24 | Add claude-opus-5 to pricing + provider catalogs (#419) |
| nopr:e655dd5bf3 | agent | code | 48 | 3 | 2(3) | 0/5 | 0/0 | 2026-07-16 | fix(review): None chokepoint resolution keeps current reasoning (session-override survival); anchor  |
| nopr:e57e66e59f | agent | code | 43 | 4 | 3(3) | 8/11 | 0/0 | 2026-06-02 | feat(usage): retain last provider-call token snapshot |
| #241 | agent | code | 42 | 2 | 1(3) | 0/5 | 0/0 | 2026-07-08 | fix(relay): pool affinity gate accepts claude-apr (2026-07-08 rename) + legacy claude-app alias (#24 |
| #3 | agent | code | 42 | 2 | 2(2) | 0/2 | 0/0 | 2026-06-02 | fix(redact): exempt GIT_AUTHOR_*/GIT_COMMITTER_* from ENV-assignment redaction (#3) |
| #622 | agent | code | 41 | 6 | 2(3) | 0/3 | 0/0 | 2026-08-20 | fix(failover): keep Tailscale errors terse (#622) |
| #165 | agent | code | 41 | 2 | 1(2) | 1/7 | 0/0 | 2026-07-01 | fix(pricing): add claude-fable-5 snapshot rate (premium tier) (#165) |
| #650 | agent | code | 40 | 2 | 1(2) | 0/2 | 0/0 | 2026-09-04 | fix(pricing): codex notional route consults the curated snapshot before external catalogs (#650) |
| nopr:f84b4ae6e4 | agent | code | 39 | 5 | 4(3) | 0/14 | 1/1 | 2026-06-20 | feat(compaction-announce): thread trigger_reason from all in-turn paths (Phases 4/5) |
| #629 | agent | code | 36 | 6 | 3(2) | 0/1 | 0/0 | 2026-08-20 | fix(agent): compaction trigger clause says 'manual' literally (#629) |
| #587 | agent | code | 36 | 4 | 4(3) | 1/9 | 0/0 | 2026-08-12 | feat(models): add grok-4.6 (catalog, pricing, 500K context, effort dial) (#587) |
| #647 | agent | code | 33 | 4 | 4(3) | 1/10 | 0/0 | 2026-09-02 | rollout: add claude-fable-5-1 (model lists, 1M context, $10/$50 pricing w/ $0.25 cache read) (#647) |
| nopr:a662e9f1e2 | agent | code | 33 | 1 | 1(2) | 0/15 | 0/1 | 2026-05-12 | fix(compressor): reject provider error/refusal strings as fake summaries |
| nopr:a7c5c51156 | agent | code | 33 | 2 | 1(2) | 0/0 | 0/0 | 2026-07-10 | fix(compression): persisted skew history must survive gateway shutdown's on_session_end |
| #46 | agent | code | 29 | 2 | 1(3) | 0/0 | 0/0 | 2026-06-15 | fix(blackbox): single global fixed char->token divisor 4.2 -> 4.5 (#46) |
| #312 | agent | code | 23 | 2 | 1(2) | 0/0 | 0/0 | 2026-07-12 | fix(pricing): gemini-bridge is Gemini-only — drop stale claude/gpt-oss aliases (#312) |
| #133 | agent | code | 23 | 2 | 2(3) | 2/10 | 0/0 | 2026-06-30 | feat(models): add claude-sonnet-5 to known-model lists + pricing table (#133) |
| nopr:e2e69f6fd1 | agent | code | 23 | 2 | 2(2) | 0/1 | 0/0 | 2026-07-25 | fix(anthropic): non-whitespace placeholder for the synthetic leading user turn |
| #38 | agent | code | 22 | 3 | 2(3) | 1/2 | 0/0 | 2026-06-14 | fix(blackbox-pricing): add claude-*-f2 failover aliases to notional Anthropic providers (#38) |
| #26 | agent | code | 18 | 2 | 1(3) | 1/2 | 0/0 | 2026-06-09 | feat(telemetry): route 413/stale-call estimator through shared 3.5 divisor (#26) |
| nopr:d0f884298b | agent | code | 18 | 2 | 2(3) | 1/11 | 0/0 | 2026-06-19 | feat(fallback): render provider/model on both sides of fallback announce |
| nopr:1a633f7cdd | agent | code | 13 | 1 | 1(3) | 0/1 | 0/0 | 2026-05-12 | fix(auxiliary_client): _get_cached_client honors normalize_resolved_model on caller-passed model |
| #447 | agent | code | 12 | 1 | 1(2) | 0/2 | 0/0 | 2026-07-26 | docs(pricing): cite Anthropic's official page for the Claude Fable 5 rates (#447) |
| nopr:9c52daad05 | agent | code | 10 | 1 | 1(3) | 1/1 | 0/0 | 2026-05-12 | fix(auxiliary): treat aux <task>.model: auto as sentinel, not a literal model id |
| nopr:1b028678e1 | agent | code | 9 | 1 | 1(2) | 0/1 | 0/0 | 2026-07-10 | fix(tool-executor): don't let the mismatch net self-disable silently (Greptile P2) |
| #68 | agent | code | 8 | 1 | 1(2) | 0/5 | 0/0 | 2026-06-20 | docs(pricing): document _lookup_official_docs_pricing precedence (#68) |
| #216 | other:hermes_state.py | code | 8 | 2 | 1(3) | 0/1 | 0/0 | 2026-07-06 | fix(state): force recency backfill v3 (#216) |
| nopr:af1cca05d5 | agent | code | 2 | 2 | 1(2) | 0/0 | 0/0 | 2026-06-21 | fix(pricing): price claude-app as notional Anthropic (claude-pool rename) |
