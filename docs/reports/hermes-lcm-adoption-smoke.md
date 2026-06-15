# hermes-lcm Adoption Smoke — Isolated Profile (PRD #2 v2, Phase 3)

**Generated:** 2026-06-14 12:03 UTC  
**Probe:** `scripts/probe_hermes_lcm_isolated.py --profile-dir staging/lcm-profile --out /Users/alexgierczyk/.hermes/worktrees/prd2v2-native-slimmer/docs/reports/hermes-lcm-adoption-smoke.md`  
**Verdict:** **GO (isolated smoke clean)** — 7/7 checks passed  

## Plugin under test

- **vendored_path:** `/Users/alexgierczyk/.hermes/worktrees/prd2v2-native-slimmer/staging/lcm-profile/plugins/hermes-lcm`
- **plugin_name:** `hermes-lcm`
- **plugin_version:** `0.16.2`
- **provenance:** `github.com/stephenschoettler/hermes-lcm @ 03b74f84440be99164ce3e2cd929917bc9550bfe (main, 2026-06-13) plugin v0.16.2; NO LICENSE upstream (internal fleet run/fork only); ingest-audit PASS (22 HIGH all test/benchmark placeholder secrets, scary-non-test==0)`

## Isolation guarantees

- Engine loaded ONLY from the vendored copy under `staging/lcm-profile/plugins/hermes-lcm` inside the worktree.
- No writes to `~/.hermes/plugins` or `~/.hermes/profiles/*`; the plugin's `scripts/install.sh` (which symlinks into a live plugin dir) was NOT run.
- Each check uses a throwaway SQLite DB under a fresh temp dir.
- Summarization is stubbed deterministically (offline) — same `summarize_with_escalation` / `_invoke_summary_llm_chain` seam the plugin's own tests patch.

## Smoke results

| # | Check | Result | Evidence |
|---|-------|--------|----------|
| 1 | load+identity | PASS | ContextEngine subclass=True, engine.name='lcm', version=True |
| 2 | normal-chat/tool | PASS | lcm_status.session_id='identity-s', lcm_describe.store_message_count=2 |
| 3 | threshold-compaction | PASS | should_compress(threshold)=True/should_compress(1000)=False=True; status=compacted, count=1, active 6<orig 8; DAG-summary-in-active=True |
| 4 | grep/describe/expand-recall | PASS | grep total_results=4 (fact compacted out of active ctx); selected snippet-matching store_id=2; expand.content recovers raw 'DEPLOY-CODE-7F3A' byte-exact=True |
| 5 | expand-unknown-id-loud-error | PASS | lcm_expand(bad id) -> {"error": "Message store_id 999999 not found"} |
| 6 | reset-semantics | PASS | compression_count 1->0 after on_session_reset; grep-before=2; lossless store still answers grep after reset (all-scope)=2 |
| 7 | failure-fail-open | PASS | summarizer LLM unavailable -> no crash=True, status=compacted, active_len=6, raw still grep-recoverable=1 |

## Raw check log

```
[PASS] load+identity — ContextEngine subclass=True, engine.name='lcm', version=True
[PASS] normal-chat/tool — lcm_status.session_id='identity-s', lcm_describe.store_message_count=2
[PASS] threshold-compaction — should_compress(threshold)=True/should_compress(1000)=False=True; status=compacted, count=1, active 6<orig 8; DAG-summary-in-active=True
[PASS] grep/describe/expand-recall — grep total_results=4 (fact compacted out of active ctx); selected snippet-matching store_id=2; expand.content recovers raw 'DEPLOY-CODE-7F3A' byte-exact=True
[PASS] expand-unknown-id-loud-error — lcm_expand(bad id) -> {"error": "Message store_id 999999 not found"}
[PASS] reset-semantics — compression_count 1->0 after on_session_reset; grep-before=2; lossless store still answers grep after reset (all-scope)=2
[PASS] failure-fail-open — summarizer LLM unavailable -> no crash=True, status=compacted, active_len=6, raw still grep-recoverable=1
```

## Notes for the reviewer

- This is a **Phase 3 isolated smoke**, not the PRD #3 real-session recovery gate. It proves the engine loads, compacts, recalls byte-exact, resets, and fails open in-process — it does NOT prove a live model spontaneously calls `lcm_expand` without being told (that is PRD #3's job).
- Live activation still requires `plugins.enabled: [hermes-lcm]` + `context.engine: lcm` in a profile config and a Hermes restart — deferred to a first low-blast-radius profile (Daedalus/Athena) per PRD §9.5, gated on PRD #3.
- License: the upstream repo ships **no LICENSE file**. Internal fleet run/fork is acceptable; public redistribution/vendoring is blocked until a license grant (PRD §0.1, §1).
