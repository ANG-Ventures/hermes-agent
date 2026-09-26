# Tranche: plugins — 84 rows

absorb = substantive added src lines found verbatim on upstream/main (hit/total); sym = added def/class names found. Signal only — read README §0.

| key | subsystem | kind | loc | files | conflict-files (syncs) | absorb src | sym | date | subject |
|---|---|---|---|---|---|---|---|---|---|
| #480 | plugins/context_engine | code | 53799 | 72 | 3(3) | 454/18923 | 198/1279 | 2026-08-06 | fix(lcm): re-vendor v0.21.0-rc2 + announce the below-threshold compaction arm (#480) |
| nopr:8b869633a4 | plugins/context_engine | code | 15792 | 28 | 3(3) | 241/5831 | 66/503 | 2026-06-16 | feat: vendor LCM context engine |
| #6 | plugins/blackbox | code | 2741 | 18 | 4(3) | 44/464 | 34/63 | 2026-06-04 | Blackbox Turn Telemetry — per-turn cost/token/tool telemetry + /cost (#6) |
| #190 | plugins/memory | code | 2147 | 12 | 2(2) | 25/844 | 28/106 | 2026-07-03 | feat(mem0): salient auto-capture A-lite (queue + gate + drain + wiring) (#190) |
| #141 | plugins/blackbox | code | 1428 | 24 | 3(3) | 149/368 | 4/14 | 2026-06-30 | feat(gateway): /usage shows the rich /context last-turn card + per-category context breakdown (#141) |
| #294 | plugins/memory | code | 1309 | 3 | 1(1) | 20/273 | 11/28 | 2026-07-11 | feat(memory): guard builtin mem0 reranking (#294) |
| nopr:b9e00148c7 | plugins/memory | code | 1241 | 3 | 1(1) | 10/268 | 14/31 | 2026-06-10 | mem0: add gated destructive tools (mem0_forget + mem0_delete) |
| #497 | plugins/context_engine | code | 1148 | 14 | 5(3) | 14/202 | 2/14 | 2026-08-07 | fix(compaction): below-threshold compactions announce themselves and name the cause (#497) |
| #146 | plugins/memory | code | 1141 | 6 | 1(1) | 24/479 | 14/72 | 2026-07-01 | feat(mem0): QMD-in-mem0 unified recall + operator toggles (#146) |
| #968 | plugins/kanban-home-cards | code | 1038 | 5 | 0(0) | 21/166 | 15/22 | 2026-09-24 | feat(plugins): kanban-home-cards — open home cards once at session start (t_13a135f5) (#968) |
| #250 | plugins/memory | code | 1035 | 8 | 1(1) | 17/462 | 21/55 | 2026-07-08 | feat(mem0): Arm-B two-pass capture router (deterministic class routing, codex-bridge primary) (#250) |
| #168 | plugins/context_engine | code | 927 | 7 | 1(1) | 12/159 | 1/9 | 2026-07-01 | feat(lcm): honor compression.target_ratio via a token-budgeted fresh tail (#168) |
| #248 | plugins/memory | code | 913 | 4 | 1(1) | 33/393 | 16/48 | 2026-07-08 | feat(mem0): flag-gated gbrain document leg (replaces QMD leg when enabled) (#248) |
| #432 | plugins/blackbox | code | 882 | 5 | 2(2) | 11/130 | 2/12 | 2026-07-26 | feat(blackbox): event-driven new-model pricing sentinel (#432) |
| #966 | plugins/context_engine | code | 815 | 7 | 1(1) | 11/113 | 0/8 | 2026-09-24 | fix(lcm): FTS parity COUNT(*) ran under _LOAD_LOCK on every engine load (freeze #3) (#966) |
| #107 | plugins/context_engine | code | 758 | 16 | 7(3) | 2/124 | 0/5 | 2026-06-26 | fix(lcm): preserve real per-message timestamps + stop dup-on-replay (#107) |
| nopr:6c8202b164 | plugins/context_engine | code | 752 | 9 | 1(1) | 7/184 | 4/20 | 2026-06-16 | feat(lcm): storage security, redaction corpus, encryption, retention |
| #996 | plugins/context_engine | code | 680 | 4 | 0(0) | 1/156 | 0/5 | 2026-09-24 | perf(lcm): turn-start reconcile scan is linear, not O(n^2) per cursor (t_49c1f7d7) (#996) |
| #431 | plugins/context_engine | code | 634 | 9 | 0(0) | 16/232 | 2/30 | 2026-07-26 | vendor(lcm): cherry-pick 9 upstream fixes (incl. the Anthropic HTTP 400 summary-role pin) (#431) |
| #154 | plugins/blackbox | code | 612 | 10 | 2(2) | 2/96 | 1/5 | 2026-07-01 | feat(pricing/blackbox): self-heal tokens.ace unpriced (vendor fallback + reprice + zero=$0) (#154) |
| #1056 | plugins/blackbox | code | 590 | 10 | 4(3) | 14/109 | 2/6 | 2026-09-25 | fix(blackbox): early-exit turns fire on_session_end, so every call ledger row has a turns row (t_6c0 |
| #799 | plugins/blackbox | code | 578 | 5 | 0(0) | 2/62 | 0/1 | 2026-09-21 | feat(blackbox): add per-call subscription attribution store (#799) |
| #1023 | plugins/context_engine | code | 563 | 4 | 0(0) | 8/80 | 0/6 | 2026-09-25 | fix(lcm): defer structural FTS rebuild off the engine-load thread; DROP works on a fresh connection  |
| #212 | plugins/memory | code | 563 | 2 | 1(1) | 5/101 | 0/10 | 2026-07-06 | feat(mem0): prefetch relevance floor — two-layer (specificity gate + weak cosine) (#212) |
| #102 | plugins/memory | code | 536 | 3 | 2(2) | 4/173 | 1/9 | 2026-06-24 | mem0 plugin Wave-2: hybrid retrieval (param-drop fix + rerank gate + temporal + prefetch ceiling) (# |
| nopr:6622f41055 | plugins/context_engine | code | 505 | 3 | 0(0) | 8/148 | 0/10 | 2026-06-16 | Implement LCM fail-open degraded fallback |
| #251 | plugins/memory | code | 505 | 4 | 1(1) | 3/223 | 6/30 | 2026-07-10 | mem0: bypass salience gate for corrections (#251) |
| nopr:8b332be03c | plugins/memory | code | 424 | 2 | 2(2) | 10/66 | 9/11 | 2026-06-14 | feat(mem0): direct-REST self-host backend (D-8) + B3/B4 scope hardening |
| #824 | plugins/memory | code | 421 | 6 | 1(1) | 10/73 | 2/6 | 2026-09-21 | fix(memory): retire idle per-agent workers (#824) |
| #444 | plugins/blackbox | code | 382 | 10 | 5(3) | 4/38 | 0/0 | 2026-07-27 | feat(blackbox): session depth + CLI invocation correlation columns (#444) |
| #1014 | plugins/kanban | code | 368 | 6 | 1(3) | 9/91 | 0/6 | 2026-09-25 | feat(kanban-dashboard): home-session facet + home channel on cards (#1014) |
| #531 | plugins/context_engine | code | 308 | 4 | 1(1) | 1/46 | 0/1 | 2026-08-08 | fix(compaction): don't burn a warm prompt cache to compact below threshold (#531) |
| #219 | plugins/memory | code | 262 | 2 | 1(1) | 1/52 | 0/6 | 2026-07-07 | feat(mem0): rerank-score recall relevance gate (L2) + dynamic gap (L3) (#219) |
| #465 | plugins/context_engine | code | 238 | 3 | 0(0) | 18/48 | 1/2 | 2026-07-31 | fix(lcm): merge adjacent assistant rows in active-context assembly (#465) |
| #434 | plugins/memory | code | 233 | 8 | 1(2) | 3/21 | 3/4 | 2026-07-26 | fix(hermeticity): route 6 Path.home()/".hermes" bypass sites through get_hermes_home() (#434) |
| nopr:051c2076e1 | plugins/context_engine | code | 228 | 2 | 0(0) | 1/25 | 0/2 | 2026-06-19 | fix(lcm): detect and repair content-drifted messages_fts triggers |
| #1053 | plugins/context_engine | code | 227 | 3 | 0(0) | 0/14 | 0/0 | 2026-09-25 | fix(lcm): session reads hide superseded replay copies (t_9cdad21b) (#1053) |
| #903 | plugins/context_engine | code | 224 | 3 | 0(0) | 0/12 | 0/0 | 2026-09-23 | test(lcm): regression layer for the boot-scan class + a loud signal when engine load stalls every tu |
| #171 | plugins/memory | code | 224 | 2 | 2(2) | 8/51 | 3/6 | 2026-07-04 | fix(memory): reuse mem0 background executors (#171) |
| #509 | plugins/context_engine | code | 211 | 4 | 1(3) | 0/24 | 0/2 | 2026-08-08 | fix(compaction): only announce a compaction that actually compacts (#509) |
| #193 | plugins/memory | code | 204 | 4 | 2(2) | 0/77 | 0/5 | 2026-07-04 | fix(mem0): size-cap oversized turns + dead-letter deterministic provider rejects (#193) |
| #412 | plugins/context_engine | code | 203 | 4 | 0(0) | 0/13 | 0/2 | 2026-07-21 | fix(plugins): serialize plugin loader import critical-section to close partial-import race (#412) |
| #887 | plugins/context_engine | code | 200 | 2 | 0(0) | 2/33 | 0/0 | 2026-09-23 | fix(lcm): the search_content backfill probe was a full-table scan on EVERY boot — partial index + ba |
| #20 | plugins/blackbox | code | 199 | 8 | 3(3) | 2/33 | 1/2 | 2026-06-08 | feat(telemetry): decompose context window (last-call cache split) + bind session_key for plugin comm |
| #202 | plugins/blackbox | code | 195 | 2 | 1(1) | 0/30 | 0/1 | 2026-07-05 | fix(blackbox): fold leaked composite <provider>/<model> id into split pair at record time (#202) |
| #40 | plugins/blackbox | code | 194 | 5 | 2(3) | 2/14 | 1/2 | 2026-06-14 | feat: widen blackbox composition call blobs (#40) |
| #10 | plugins/blackbox | code | 194 | 6 | 1(1) | 1/35 | 0/1 | 2026-06-04 | blackbox: subagent cost rollup in /cost session + per-turn retention sweep (#10) |
| #580 | plugins/cron_providers | code | 184 | 2 | 2(1) | 1/25 | 0/1 | 2026-08-11 | fix(cron): never arm a one-shot whose fire_at is already in the past (#580) |
| #393 | plugins/memory | code | 182 | 2 | 0(0) | 0/91 | 0/4 | 2026-07-18 | fix(mem0-scrub): catch natural-language credential disclosure (re-land of #214) (#393) |
| nopr:531f1e8c64 | plugins/memory | code | 168 | 2 | 2(2) | 1/4 | 0/0 | 2026-06-14 | harden mem0 client against fd-leak (HANDOFF-fd-leak-client-pool.md) |
| #9 | plugins/blackbox | code | 159 | 6 | 4(3) | 2/19 | 1/1 | 2026-06-04 | Blackbox card fixes: chat fields, cache %, alerts toggle, 3d price cache (#9) |
| #613 | plugins/memory | code | 149 | 2 | 1(1) | 1/22 | 0/3 | 2026-08-19 | feat(mem0): specificity-tiered rerank-gate floor (strict 0.0 vague / -0.35 specific) (#613) |
| #407 | plugins/memory | code | 144 | 2 | 0(0) | 3/34 | 0/2 | 2026-07-20 | feat(capture): transient-narration gate on world-fact staging + dropped-log (gbrain residue §5.3/RC1 |
| nopr:a1a1a1065e | plugins/memory | code | 137 | 2 | 1(1) | 1/28 | 1/2 | 2026-06-19 | feat(mem0): add gated canonical user_id pinning with provenance |
| #508 | plugins/context_engine | code | 122 | 2 | 1(1) | 0/4 | 0/0 | 2026-08-08 | fix(lcm): bridge compression.maintenance_min_pressure_ratio into LCMConfig (#508) |
| nopr:60ea6b2b42 | plugins/context_engine | code | 118 | 3 | 0(0) | 1/11 | 0/3 | 2026-07-25 | fix(lcm): self-heal FTS column drift at startup — reorder init so the repair can actually run |
| #825 | plugins/context_engine | code | 113 | 2 | 0(0) | 1/6 | 0/1 | 2026-09-21 | fix(lcm): stop clamping explicit -900k Codex context variants to 372k (#825) |
| nopr:efe50db808 | plugins/context_engine | code | 108 | 3 | 0(0) | 0/21 | 0/1 | 2026-06-19 | fix(lcm): toggleable Prong-A identifier fidelity — fixes baseline-repro loop |
| #592 | plugins/memory | code | 102 | 2 | 1(1) | 1/8 | 1/1 | 2026-08-15 | fix(mem0): destructive by-filter resolution sees the full scope, not a 20-row page (#592) |
| #905 | plugins/blackbox | code | 94 | 2 | 0(0) | 0/9 | 0/0 | 2026-09-23 | perf(blackbox): index turns(ts_start) so rolling-window reads SEARCH instead of SCAN (#905) |
| nopr:59ed0e41a1 | plugins/memory | code | 87 | 2 | 2(2) | 0/10 | 1/1 | 2026-06-14 | mem0 client: optional CA bundle for private-CA HTTPS (mem0.ace cutover fix) |
| #448 | plugins/memory | code | 86 | 2 | 1(1) | 0/1 | 0/0 | 2026-07-26 | fix(mem0): floor_outcome reported an empty search when a gate emptied the candidates (#448) |
| nopr:dc4245ab4c | plugins/context_engine | code | 84 | 2 | 0(0) | 2/12 | 0/1 | 2026-06-17 | fix(lcm): lcm_expand_query degrades on malformed aux-LLM response (concurrency) |
| #902 | plugins/context_engine | code | 82 | 3 | 0(0) | 0/14 | 0/1 | 2026-09-23 | fix(lcm): the ingested_at backfill was the SECOND full-table scan on every boot — gate one-time back |
| #468 | plugins/memory | code | 76 | 2 | 1(1) | 0/15 | 0/1 | 2026-08-06 | feat(mem0): log full query text (qtext=) on prefetch telemetry lines (#468) |
| #64 | plugins/memory | code | 73 | 3 | 2(2) | 0/1 | 0/0 | 2026-06-20 | fix(mem0): api_key optional in schema; harden composition test env isolation (#64) |
| #915 | plugins/blackbox | code | 66 | 2 | 0(0) | 0/11 | 0/1 | 2026-09-23 | fix(blackbox): create turns indexes after the column migration (t_71ae3a75) (#915) |
| #218 | plugins/memory | code | 65 | 2 | 1(1) | 0/16 | 0/0 | 2026-07-07 | feat(mem0): richer prefetch recall telemetry — cosine distribution + injected count (#218) |
| nopr:3087d451ce | plugins/memory | code | 61 | 2 | 2(2) | 0/14 | 1/1 | 2026-06-14 | mem0 client: reads scope to user_id only, writes scope both (cutover recall fix) |
| #972 | plugins/blackbox | code | 55 | 2 | 0(0) | 0/8 | 0/1 | 2026-09-24 | fix(blackbox): roll up served subs from joined call ledger (#972) |
| #660 | plugins/blackbox | code | 45 | 2 | 0(0) | 0/3 | 0/0 | 2026-09-09 | fix(blackbox): reprice_unpriced honors a snapshot-sourced entry on a notional codex route (#660) |
| #320 | plugins/memory | code | 43 | 2 | 1(1) | 0/20 | 0/1 | 2026-07-13 | fix(mem0): recover orphaned inflight capture rows on gateway startup (#320) |
| #192 | plugins/memory | code | 42 | 2 | 2(2) | 0/5 | 0/0 | 2026-07-04 | fix(mem0): search_meta_filtered must send a non-empty query (live auto-capture 400) (#192) |
| #955 | plugins/blackbox | code | 31 | 3 | 1(1) | 0/1 | 0/0 | 2026-09-24 | Fix blackbox turn ID join with per-call ledger (#955) |
| #44 | plugins/blackbox | code | 30 | 1 | 1(1) | 0/12 | 0/1 | 2026-06-15 | fix(blackbox): alert card output -> finished/unfinished (#44) |
| nopr:d4c0ea5a47 | plugins/memory | code | 26 | 1 | 1(1) | 0/21 | 0/0 | 2026-06-20 | SPEC-10: strengthen mem0_conclude save-guidance for capture=off model A (explicit when-to-save/when- |
| #652 | plugins/memory | code | 24 | 2 | 0(0) | 0/13 | 0/1 | 2026-09-08 | fix(mem0): capture-router defaults — primary gpt-5.4-mini (retired) -> gpt-6-astra; fallback never i |
| #912 | plugins/blackbox | code | 21 | 2 | 0(0) | 0/1 | 0/0 | 2026-09-23 | blackbox: index turns(profile) — the skill-stats miner's DISTINCT profile read was a 107 s SCAN (#91 |
| #30 | plugins/blackbox | code | 19 | 5 | 2(3) | 0/10 | 0/0 | 2026-06-09 | feat(telemetry): show message count on conversation-history line (#30) |
| nopr:5ae50ce919 | plugins/memory | code | 19 | 1 | 1(1) | 0/2 | 0/0 | 2026-06-14 | mem0: bound httpx connection pool to stop fd leak |
| #516 | plugins/memory | code | 17 | 2 | 1(1) | 0/3 | 0/0 | 2026-08-08 | fix(mem0): gate bare status-pings at Gate A (the recurring 'status?' near-miss) (#516) |
| nopr:bb54af8d6c | plugins/memory | code | 15 | 1 | 1(1) | 0/4 | 0/0 | 2026-06-04 | mem0: add MEM0_CAPTURE recall-only mode (skip per-turn auto-capture) |
| #117 | plugins/memory | code | 8 | 1 | 1(1) | 0/7 | 0/1 | 2026-06-28 | docs(mem0): document why dedup search omits agent_id (server treats it as a result filter) (#117) |
| #149 | plugins/kanban | code | 4 | 1 | 0(0) | 0/2 | 0/0 | 2026-07-01 | fix(kanban-dashboard): Title-case the 'review' + 'scheduled' column labels (#149) |
