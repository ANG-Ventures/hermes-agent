# Fork-Delta + Merge-Trap Review — 2026-07-23 Parity Merge

**Reviewer:** subagent (review lane). **Gate:** `manifest+forkdelta`. **Frozen upstream target:** `a7a696ba5`. **Fork base:** `37fa0a353`. **Merge-base:** `e0240d7bf7`.
**Method:** enumerate → bucket with evidence → mechanical silent-drop scan (Python AST top-level def/class + TS symbol regex, fork∖staged∖upstream) → diff suspicious vs `fork/main` → run the 30 fork-features.json manifest tripwires → mass-ack evidenced buckets via `state.record_ack` → hold out unexplained/RED. **Bar:** every ack reason names ledger line, upstream commit, deletion-set membership, or a green test — never "looks fine".

**Bottom line: 247 acked / 1 held out at review time; gate = honest FAIL; 3 REAL findings reported (2 block a green gate, 1 cleanup) — see §4, §4b, §5. No finding was acked-away.**

> **⚠️ Concurrent-sibling update (post-review):** a sibling process resolving this same worktree LANDED the §4 fix mid-review — staged `tests/hermes_cli/test_gateway.py` is now upstream's superset, `_force_process_exit` dropped, and it recorded its own ack for that path (so acks went 247→248, and the held-out path is now covered by the sibling's fix, not by me). Per skill §6e I did not fight it — verified its fix on disk (test file == upstream `a7a696ba5`; the run-gateway hard-exit tests pass). **Finding #4b (locales) remains UNFIXED as of last check** (en.yaml still 0 occurrences; `test_manual_reset_sticky_route.py` still RED) and is the outstanding blocker. Findings #1 and #3 (server.py dupes) also stand for the record.

Ran `scripts/run_tests.sh` evidence is inherited from `fullsuite-final-delta-classification.md`: it claims **ZERO merge regressions across ~35,093 tests**. ⚠️ **That claim is INCOMPLETE — this review found 2 reds it missed** (§4 `test_gateway.py`, §4b `test_manual_reset_sticky_route.py`), because its "failing file byte-identical to upstream ⇒ inherited" classifier skips files that differ from upstream (fork-kept tests) and it evidently did not run/attribute the fork-features manifest tripwires. The full-suite pass is still a strong behavioral net for the ~35k upstream-shared tests behind the coverage acks below, but it is NOT sufficient proof of fork-feature parity — the manifest tripwires are, and one of them is RED.

---

## 1. Enumeration

`gates --stage manifest+forkdelta` at tree `c9e4fa80`: **6 covered, 248 uncovered, 0 acked, 0 lint errors.** All 248 are status **M (modified)** fork-delta paths — the merge changed a fork-delta file with no `fork-features.json` glob covering it. There are **no wholesale (D) deletions in the uncovered set** (the 39 merge deletions are handled separately, §3). `lint_manifest` = clean (no `fork-features.json` path rot this sync).

## 2. Buckets (248 total)

| Bucket | Count | Evidence class | Disposition |
|---|---:|---|---|
| **Clean auto-merge fork-delta** | 135 | NOT a conflict file; git 3-way kept both non-overlapping sides by construction, no side picked | ACK |
| **Ledger-itemized conflict** | 60 | Resolution recorded in `RESOLUTION-LEDGER.md` (incl. 16 locales as a block) | ACK |
| **Conflict resolved, not itemized in ledger** | 53 | 27 desktop TS + 26 py/test; reviewed this pass via silent-drop scan | ACK (5 tailored) |
| **REAL red — held out** | 1 | `tests/hermes_cli/test_gateway.py` stale test (see §4) | **NOT ACKED** |

- **"dropped-by-upstream-deletion" / DU / arch-split:** the DU/arch-split files (mem0 `_backend`/`_oss_providers`/`_setup`, their tests, `test_run_agent.py`) are `git rm`'d (status D, per `mem0-resolution-decision.md` + ledger §DU) — they are **not** in the uncovered M-set, so no ack needed. The upstream-deletion *intent* still explains several §2 losses (release.py ACP registry, config.py brew/uv helpers) — see §3 + tailored acks.
- **fork file superseded per ledger:** covered by the 60 ledger-itemized acks (e.g. web_server converge-on-upstream `to_thread`, hermes_state v23 FTS, moa_loop fold).
- **path rot in fork-features.json:** **none** (`lint_manifest` clean).
- **genuinely-uncovered NEW fork delta needing test/ack:** the 53 unledgered conflict files — acked with per-path evidence; none needed a NEW covering test because behavioral parity is already proven by the full suite + the fork-features `tests` nodes.

## 3. Suspicious bucket — mechanical silent-drop investigation

Scanned every conflict file for fork-only top-level `def`/`class` (Python, via AST) and exported/declared symbols (TS, via regex) that exist in `fork/main`, are **absent from the staged tree**, AND **absent from upstream** (upstream having it ⇒ legitimate convergence, not a drop). Raw hits triaged to root cause:

**Python (12 files flagged; all resolved benign or deletion-explained):**
- `gateway/platforms/api_server.py::_port_is_available` — **BENIGN.** Upstream #10297 deliberately removed the 127.0.0.1 pre-probe (direct-bind); documented in staged `tests/gateway/test_api_server_bind_guard.py` L157-160, test green.
- `hermes_cli/config.py` (`is_uv_tool_install`, `is_unsupported_install_method`, `format_unsupported_install_warning`, `unsupported_install_method_label`, `_has_uv_tool_marker`) — **BENIGN, deletion-explained.** Upstream also dropped them (sanctioned brew/PyPI packaging removal, skill upstream #68217); their tests are in the 39-file D set (`test_uv_tool_update.py`, `test_banner_pip_update.py`); **zero remaining callers** in staged tree.
- `scripts/release.py` (`build_release_artifacts`, `_update_acp_registry_versions`) — **BENIGN, ledger L46.** acp_registry/ + MANIFEST.in + upload_to_pypi.yml deleted upstream (D set); staged release.py has 0 acp refs.
- `hermes_cli/web_server.py` (`_update_declared_provider_config`, `_dashboard_console_context`, `_declared_provider_file_path`, …) — **BENIGN, relocated.** Ledger worker3 §web_server converge-on-upstream; helpers folded into `_declared_provider_payload` (staged L5636); `DeclaredMemoryProvider` + `is_truthy_value` fork imports preserved (L101/L105).
- `gateway/run.py::_dequeue_pending_with_transcription`, `gateway/slash_commands.py::_save_config_key`, `gateway/platforms/api_server.py::_port_is_available` — **nested inner functions**; their parent methods (`GatewayRunner`, `GatewaySlashCommandsMixin`, `APIServerAdapter`) survive; flagged only by reindent, not a real drop.
- packaging/web_server test files — reconciled supersets (see below), all green.

**TypeScript (5 files flagged; all upstream-converged renames):**
- `session-row.tsx` `SessionRowLeadDot`/`SidebarRowDot` → upstream `SessionStatusDot` (staged imports & renders it L231/L240).
- `store/layout.ts` `$sidebarWorkspaceCollapsedIds` (buggy XOR string[]) → upstream `migrateWorkspaceCollapsedIds()` Record<string,bool> + `toggleWorkspaceNodeCollapsed` (staged L161/L264).
- `use-session-list-actions.ts::refreshCronSessions` — **still present** in staged (glob miss); no drop.
- `use-hermes-config.ts::cwd`, `use-message-stream::normalize` — local consts, reindented not dropped.

**Suspicious findings requiring LOUD report: 1 from the def/class scan** (→ §4). The def/class scan found no *silent* fork-feature code drop — every apparent loss traces to an upstream deletion (sanctioned), an upstream convergence/rename, or a reindent artifact. **However, the manifest `tests` nodeids surfaced a THIRD real finding the symbol scan could not see — a dropped i18n key block (→ §4b).** Net **suspicious/REAL findings: 3.**

## 4. 🔴 REAL FINDING #1 — stale test kept over upstream superset (`tests/hermes_cli/test_gateway.py`)

**Class:** stale-test / wrong-side-kept (merge kept fork's TEST while merged PROD adopted upstream's implementation). **NOT a code regression — behavior is preserved.** **Held out of acks; requires orchestrator fix (do NOT ack, do NOT let it ride).**

- Staged `hermes_cli/gateway.py` is **byte-identical to upstream** `a7a696ba5`: `run_gateway` hard-exits via `_hard_exit_after_gateway_teardown` → `gateway.run._exit_after_graceful_shutdown` (does `os._exit`, flushes stdio, handles lingering non-daemon threads — #53107). The fork's named helper `_force_process_exit` was replaced by this upstream path; **behavior preserved.**
- But the resolver **kept fork's `tests/hermes_cli/test_gateway.py`** (a UU conflict file; fork side references `_force_process_exit` 6×), which `monkeypatch.setattr(gateway, "_force_process_exit", …)` a symbol the merged prod no longer defines → **13 tests FAIL** (`test_run_gateway_*`, `test_force_process_exit_calls_os_exit_and_flushes`).
- **Correct resolution:** take **upstream's `test_gateway.py`** (superset; patches `_exit_after_graceful_shutdown`, the merged prod's real exit seam). Verified: upstream's own `tests/hermes_cli/test_gateway_run_hard_exit.py` PASSES 4/4 against the merged tree; upstream's `test_gateway.py` exit-path tests pass (51 passed; its only fails here are 6 Windows-detached tests, platform-gated env failures, unrelated to this seam).
- **Why the full-suite pass missed it:** `fullsuite-final-delta-classification.md` classifies failing files as regressions only when byte-identical to upstream; this file **differs from upstream** (it's fork's), so it fell outside that classifier's "byte-identical ⇒ inherited" rule. This is a gap in that report's coverage — flagged here.

**Impact if shipped as-is:** CI red on `tests/hermes_cli/test_gateway.py` (13 fails); no runtime behavior change.

## 4b. 🔴 REAL FINDING #3 — fork-only locale keys DROPPED in all 16 locales (the §6f locale-block trap)

**Class:** silent fork-feature drop via "took-upstream-for-all-locales" (skill `upstream-parity-merge` §6f). **REAL regression — a fork feature renders a raw i18n key instead of text.** Caught by the fork-features.json manifest tripwire, NOT by the path/def scan (a locale VALUE drop is invisible to an AST/symbol scan). **NOT sanctioned; reported LOUDLY.**

- Fork `locales/*.yaml` `reset:` block carries two **fork-only** keys: `preferences_preserved` and `model_preference_unavailable`. The merge took **upstream's `reset:` block** (which lacks both) → **both keys DROPPED in ALL 16 locales** (verified af/de/en/es/fr/ga/hu/it/ja/ko/pt/ru/tr/uk/zh-hant/zh; upstream target `a7a696ba5` has 0 occurrences).
- Staged prod still consumes them: `gateway/slash_commands.py:425` `t("gateway.reset.preferences_preserved")` and `:429` `t("gateway.reset.model_preference_unavailable")` → renders the raw key string.
- **Tripwire that caught it:** `tests/gateway/test_manual_reset_sticky_route.py` (fork-features.json #9 "gateway configured and persisted route identity helpers", `gateway/fork_ext/route_identity.py`) — **4 fails**: `test_manual_reset_persisted_entry_wins_map_divergence`, `test_manual_reset_preserves_but_marks_unavailable_model_preference`, `test_manual_reset_surfaces_invalid_persisted_model_preference`, `test_manual_reset_invalid_persisted_identity_has_safe_boundary_notice`. Symptom: `assert 'model preference' in reply` fails; reply contains literal `gateway.reset.model_preference_unavailable`. (399 of the 30 manifest-nodeid suites' cases passed; these 4 are the only fork-feature reds.)
- **Ledger error:** `RESOLUTION-LEDGER.md` L25 claims "NO fork-only keys (no gateway.branch/merge in conflict regions — verified)". That verification only looked for `gateway.branch/merge` and **missed the fork-only `reset.*` keys** — the exact §6f failure mode (fork-owned = absent upstream AND at merge-base; those keep fork values unconditionally).
- **Fix (orchestrator):** re-add `reset.preferences_preserved` + `reset.model_preference_unavailable` (fork values) to the `reset:` block of all 16 `locales/*.yaml`. Runtime impact: `/reset` with a preserved/unavailable model preference emits a raw key instead of the warning text.
- **Ack disposition:** the 16 locale *paths* are acked (the .yaml change is otherwise legit — reasoning superset), but each ack reason is rewritten to DISCLOSE this drop and point here — the drop itself is NOT sanctioned.

## 5. Merge-trap triage (39 warnings, all `duplicate-function-bodies`)

Classified same-name-same-scope (real shadow) vs distinct-name-identical-body (benign):

### 🔴 REAL FINDING #2 — 10 duplicate module-level defs in `tui_gateway/server.py`
The merge kept **both** fork's and upstream's copy of a code region → the same module-level function is defined **twice**; the second silently shadows the first. All 10 bodies are **byte-identical** (verified via `ast.get_source_segment`), so **runtime-benign** (the shadow == the original), but it is real merge-duplicated dead code and should be de-duplicated before/after landing. 9 of 10 are **merge-introduced** (fork=1 / upstream=1 → staged=2); `_content_display_text` pre-existed (fork=2). server.py grew fork 16097 → upstream 17191 → **staged 18602** lines, consistent with a re-copied block. An entire region (`_ISOLATED_SESSION_READ_COMMANDS` const + the 7 `_format_live_*` fns + `_live_slash_command_output`) is duplicated verbatim (L15076-block re-appears at L16766+).

| line (2nd def) | function |
|---:|---|
| 1603 | `_apply_compute_host_metadata_mirror` (1st @1427) |
| 4796 | `_session_usage_snapshot` (1st @4757) |
| 6729 | `_content_display_text` (1st @6414, pre-existing) |
| 16766 | `_format_live_usage_output` (1st @15076) |
| 16810 | `_format_live_history_output` (1st @15120) |
| 16835 | `_format_live_prompt_output` (1st @15145) |
| 16851 | `_format_live_context_output` (1st @15161) |
| 16897 | `_format_live_tools_output` (1st @15207) |
| 16928 | `_format_live_model_output` (1st @15238) |
| 16939 | `_live_slash_command_output` (1st @15249) |

**Verdict: REAL (report, do not fix).** Recommend the orchestrator delete the duplicated L16766+ block in `tui_gateway/server.py`. Runtime risk: low (identical shadow); maintainability risk: real (two copies drift on next edit). `tui_gateway/server.py`'s forkdelta path is ACKED (fork behavior present, no drop) with this finding disclosed in the ack reason.

### BENIGN — 29 distinct-name identical-body pairs (across 26 files)
Two *differently named* functions with structurally identical bodies — legitimate by design, NOT a merge duplication. Examples verified:
- `plugins/platforms/{irc,simplex}/adapter.py`: `is_connected` vs `validate_config` — both check the same env/config presence; identical by intent.
- `tests/tools/test_discord_tool.py`: `setup_method` vs `teardown_method` — same cache-reset body.
- ~25 test files: sibling assertion tests (`test_windows_paths_match` vs `test_unix_paths_still_match`, SSRF `test_skips_guard_*`, tirith `test_cosign_*`, message-repair pairs) — intentional parallel cases, not merge artifacts.

No action needed on the 29 benign.

## 6. Acks recorded

`state.record_ack` loop (NOT 248 CLI calls): **247 acked, 1 held out.** Per-bucket:
- 135 clean auto-merge · 60 ledger-itemized (16 locales) · 52 unledgered-reviewed (of which 5 carry tailored per-path evidence: `scripts/release.py`, `hermes_cli/config.py`, `gateway/platforms/api_server.py`, `hermes_cli/web_server.py`, `tui_gateway/server.py`).
- **Held out (NOT acked):** `tests/hermes_cli/test_gateway.py` — REAL red (§4).

Every reason cites a ledger line, an upstream commit/deletion-set membership, a relocation target symbol, or the full-suite green.

## 7. Final gate status

**`manifest+forkdelta` = FAIL — but now with a SINGLE, well-characterized blocker.** After the sibling landed the §4 fix, the latest official run (1352s) reports: **`6 covered, 0 uncovered, 248 acknowledged, 0 lint errors`** — the **path-coverage arm is fully GREEN**. The gate FAILs solely on the **manifest `tests` nodeids (exit=1)**: `tests/gateway/test_manual_reset_sticky_route.py` still 4-RED from the §4b locale-key drop (the other 29 fork-feature tripwires pass).

Evolution of the two original RED signals:
1. ~~1 uncovered path (`test_gateway.py`, §4)~~ → **RESOLVED by concurrent sibling** (swapped to upstream's superset test; path now acked/covered). Confirmed on disk.
2. **manifest test exit=1** (`test_manual_reset_sticky_route.py`, §4b locale drop) → **STILL RED — the sole remaining blocker.** Not ackable (a real fork-feature regression); clears only when the fork-only `reset.*` keys are re-added to all 16 locales.

- 6 covered · 248 acknowledged (135 auto-merge + 60 ledger + 52 unledgered-reviewed + 1 sibling's test_gateway fix; 16 locale acks disclose §4b) · 0 uncovered · 0 lint errors.
- **Acking would have hidden two real regressions; instead the gate is honest-RED on exactly the one unfixed behavioral defect.**

**Findings for the orchestrator (report-only, not fixed by this review):**
1. ~~**`tests/hermes_cli/test_gateway.py`** (§4)~~ — **done by sibling.**
2. **`locales/*.yaml` (all 16)** (§4b) — **OUTSTANDING BLOCKER.** Re-add fork-only `reset.preferences_preserved` + `reset.model_preference_unavailable` (fork values); REAL runtime regression (`/reset` emits raw i18n keys); fixes the 4 `test_manual_reset_sticky_route.py` fails → clears `manifest_test_exit` → gate goes GREEN.
3. **`tui_gateway/server.py`** (§5) — remove 9 merge-introduced byte-identical duplicate module-level defs (L16766+ block); runtime-benign, dead-code bloat; does NOT block the gate.

Once finding #2 lands, re-run `gates --stage manifest+forkdelta` → expect **PASS** (0 uncovered, 0 lint, all 30 manifest tripwires green) with the 248 acks intact.
