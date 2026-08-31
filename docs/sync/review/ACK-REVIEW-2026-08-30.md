# ACK-REVIEW — parity sync 2026-08-29 · manifest+forkdelta gate

**Lane:** ACK-REVIEW (read-only except the ack API + this file).
**Reviewer:** subagent, 2026-08-30.
**Worktree:** `~/.hermes/worktrees/parity-2026-08-29`, branch `sync/upstream-2026-08-29`.
**Fork base (`fork_main_at_start`):** `de200ebbf52b62f38a3353b1920e5852bc09b143`
**Frozen upstream target:** `26350357d76e4508c8df9304a3374bdc5a6f6220` · **merge-base:** `1e5b50744094959db5536eca9df3881d13fd28d8`
**Doctrine:** forkdelta-gate-honest-clearance · `fork-parity-doctrine` D8/D9/D10 · `upstream-parity-merge-local`.

---

## 🔴 SUSPICIOUS BUCKET (fork code VANISHED with no explanation): **ZERO ITEMS**

**No fork-authored code disappeared unexplained.** This is a measured result, not an assumption — see §5 for the mechanical method and §5b for the full triage of every hit. Nothing was acked away.

## 🟠 NEEDS-ATTENTION: **1 ITEM** (not acked — a red canary on a fork-permanent feature)

Enumerated by name in §6. It is **not** a path-coverage problem (the path arm is green); it is the manifest `tests` arm, and it is the sole remaining blocker on this gate.

---

## 1. Enumeration — and a correction to the "367" headline

The last `manifest+forkdelta` entry in `gates.jsonl` (ts 2026-08-30 04:23, tree `8319c6b5`) reported
`7 covered, 367 uncovered, 0 acknowledged, 3 lint error(s)`.

Recomputed this review against `fork_main_at_start` (**not** `merge_base` — the gate intersects the fork delta with the paths this merge actually touched):

```python
base    = git merge-base origin/main de200ebbf5      # 1e5b507440
touched = worktree_changed_files(fork_start) | changed_files(fork_start, HEAD)   # 5,004
report  = forkdelta.compute_fork_delta(repo, base=base, fork_ref=de200ebbf5, touched_paths=touched)
# changed 385 · covered 12 · uncovered 373
```

**373, not 367 — and 12 covered, not 7.** The delta is the NEW 22-entry `docs/sync/fork-features.json` (up from 13) landing between the two runs: it newly covers `cli.py`, `hermes_cli/commands.py`, `hermes_undo.py`, `plugins/platforms/discord/adapter.py`, `plugins/platforms/telegram/adapter.py`, `tools/cronjob_tools.py`, and `gateway/runtime_footer.py`-class paths, while the recompute also picks up 6 paths the earlier snapshot predated. **Bucket 3 (REGISTRY-COVERED-ELSEWHERE) was therefore checked FIRST and is empty by construction** — every path below was re-tested against the new registry via `forkdelta.covered_path()` before being treated as uncovered.

**The 3 lint errors are already fixed.** They were rotten registry nodeids (`test_gateway_stop_launchd_service_restart_keeps_nonzero_exit`, `TestRunJobScript::test_script_timeout`, `TestRegression_ToolsetScoping::test_tool_call_rejects_out_of_scope_tool`). Re-running `lint_manifest.lint_manifest()` against the new 22-entry registry this review returns **`ok=True`, 0 errors** — the canary lane's registry v2 repaired them. No ack was needed or taken for these.

## 2. Bucket counts (373 paths, 100% adjudicated)

| # | Bucket | Count | Disposition |
|---|---|---:|---|
| 1 | **UPSTREAM-ADOPTED** — worktree blob byte-identical to upstream `26350357d7` | **50** | ACK |
| 1/2 | **LEDGERED RESOLUTION** — itemized in `RESOLUTION-LEDGER-2026-08-29.md` | **121** | ACK |
| 1/2 | **CLEAN AUTO-MERGE** — not a conflict file; git kept both non-overlapping sides | **173** | ACK |
| 1/2 | **RESOLVED CONFLICT, not ledger-itemized** — cleared by mechanical audit | **23** | ACK |
| 4 | **ORCHESTRATOR / TOOL EDITS** | **6** | ACK |
| 5 | **GENUINELY-UNCOVERED NEW DELTA** | **0** | — |
| 6 | **SUSPICIOUS (fork code vanished)** | **0** | — |
| | **Total acked** | **373** | |

Buckets 1 and 2 are merged in the table where the evidence class is the same artifact (the ledger records both "took upstream" and "fork superseded by upstream" as one line with a side-choice column); each individual ack reason states which it is.

### Bucket 4 — orchestrator/tool edits (6, named)

| path | evidence cited in the ack |
|---|---|
| `scripts/hermes_parity/gitops.py` | gate-fidelity fix to `conflict_marker_lines()` (exact-7-char shapes + diff3 `\|\|\|\|\|\|\|`); regression-tested by `tests/scripts/test_hermes_parity_gate_fidelity.py`, **6/6 green** |
| `scripts/hermes_parity/forkdelta.py` | gate-fidelity fix — order-preserving dedupe in `manifest_nodeids()` (duplicate nodeids → spurious "Empty parameter set"); same test file, 6/6 green |
| `docs/sync/fork-features.json` | the registry itself (13→22 entries, canary lane). Verified: `lint_manifest` now `ok=True`, 0 errors. A registry cannot cover itself |
| `docs/sync/review/conflict-buckets.md` | sync review artifact (the 184-row conflict census this review used); documentation, not fork behavior |
| `gates.jsonl` | the gate ladder's own append-only audit log |
| `.parity-state.json` | the parity state file `hermes_parity` writes (these acks are recorded *into* it) |

`tests/fork_canaries/*` and `tests/scripts/test_hermes_parity_gate_fidelity.py` did **not** appear in the uncovered set — they are new files the merge added, so they are not part of the fork delta vs `fork_main_at_start`. Their acks were therefore unnecessary, not omitted. **All 6 canaries execute green** (see §7).

## 3. Method

1. **Recompute** the uncovered set against `fork_main_at_start` with the *new* registry (§1).
2. **Classify by artifact, not by vibe:** intersect with (a) `git merge-tree` conflict set + the 184-row `conflict-buckets.md` census, (b) exact-path and glob (`apps/desktop/**`, `locales/*.yaml`, `contributors/emails/*`) matches into `RESOLUTION-LEDGER-2026-08-29.md`, (c) byte-comparison of every worktree blob against the frozen upstream target.
3. **Mechanical silent-drop audit (D10)** across all 324 code paths — tree-wide, not per-file (§5).
4. **Marker + compile sweep** across all 373 paths: **0 real conflict markers** (exact-7-char shapes incl. `|||||||`), **0 `py_compile` failures**.
5. **Ack in one Python loop** via `state.record_ack(Path('.'), path, reason)` — 373 acks in 6.4 s, not 373 CLI calls.

**Ack bar:** every reason names a ledger line number, a block spec, an upstream commit SHA, a byte-identity verification, or a green test. No reason says "looks fine".

## 4. Evidence distribution

- **169** of the 373 were staged conflicts (`UU`/`AA`/`UD`); **204** auto-merged clean and were never a conflict at all.
- **135** of those 169 conflicts are itemized in the 2026-08-29 ledger; the remaining **34** are covered by the ledger's block specs (`apps/desktop/**`, `contributors/emails/*`) or by the §5 mechanical audit.
- **50** paths are byte-identical to upstream — including **all 36** `apps/desktop/**` paths (D9 retired-subtree, verified 0/36 differ) and the `tools/browser_use_cli.py` + `tests/tools/test_browser_use_cli.py` AA pair.

## 5. 🔬 SUSPICIOUS-bucket investigation — the mechanical silent-drop audit

**Method (fork-parity-doctrine D10).** For every code path in the uncovered set:
`fork_authored = defs(fork/main) − defs(merge-base)`; a **loss candidate** is a fork-authored symbol absent from the **entire merged tree**, not merely from its own file. Tree-wide is load-bearing: upstream refactors legitimately *move* code, and a per-file diff manufactures ~200 false alarms.

- Merged-tree index: **94,594 symbols across 8,229 files** (Python AST `def`/`async def`/`class`; TS/JS export+function regex).
- Audited: **324** code paths in the uncovered set, plus a widened second pass over all 279 non-upstream-verbatim paths.

**Result: 10 fork-authored loss candidates across 5 files. All 10 traced to a sanctioned cause. ZERO unexplained.**

### 5a. The 10 fork-authored candidates — each resolved

| symbol | file | verdict |
|---|---|---|
| `test_schema_has_role_top_level_and_per_task` | `tests/tools/test_delegate.py` | **SUPERSEDED**, ledger L87 + in-file comment L3369-3375 (upstream `9dfbde19db` depth-derived roles; survivors `test_schema_no_longer_advertises_role`, `test_role_is_depth_derived_not_caller_declared`) |
| `test_unknown_role_coerces_to_leaf` | `tests/tools/test_delegate.py` | **SUPERSEDED**, same ledger line + same in-file comment |
| `test_batch_mode_per_task_role_override` | `tests/tools/test_delegate.py` | **SUPERSEDED**, ledger L87 + in-file comment L3465-3470 |
| `test_build_child_agent_ignores_acp_command_when_binary_missing` | `tests/tools/test_delegate.py` | **SUPERSEDED**, ledger L87 — upstream `#80450` replaced silent-clear with a LOUD refusal; verified in merged `tools/delegate_tool.py:1938-1946` (raises `ValueError`), survivor `test_pinned_acp_command_missing_raises` present and passing |
| `test_default_is_three` | `tests/tools/test_delegate.py` | **SUPERSEDED**, ledger L87 — upstream raised the default 3→10 (verified `tools/delegate_tool.py:850` docstring); renamed `test_default_is_ten`, present |
| `test_directory_and_dev_null_verdicts_not_crash` | `tests/hermes_cli/test_gateway_restart_loop.py` | **SUPERSEDED**, ledger L106 — renamed `..._fail_closed_not_crash` (line 2185). **Bodies diffed this review: identical assertions** (dir→False, `/dev/null`→True) plus an `os.name != "nt"` guard |
| `test_includes_nous_subscription_prompt` | `tests/run_agent/test_run_agent_api_kwargs.py` | **SUPERSEDED**, in-tree provenance comment at L104-109 citing upstream `4032a15ad0` (#95005). Verified: `git log -S build_nous_subscription_prompt` returns exactly that commit; symbol **absent from upstream** — the test would fail on a symbol that no longer exists |
| `test_already_installed_on_path` | `tests/tools/test_browser_use_cli.py` | **UPSTREAM-ADOPTED (AA)** — brief's pre-classified AA ruling; file byte-identical to upstream. Contract survives as `test_already_installed_in_managed_bin` |
| `test_named_session_skips_backend_resolution` | `tests/tools/test_browser_use_cli.py` | **UPSTREAM-ADOPTED (AA)** — same ruling. Named-session `BU_NAME` behavior verified still live in merged `tools/browser_use_cli.py:704-710/737/747` |
| `stubOffsetDimension` | `apps/desktop/.../edit-context.test.tsx` | **D9 RETIREMENT** — `apps/desktop` is an upstream-owned retired subtree; the fork's desktop delta is deliberately discarded. Blob byte-identical to upstream |

### 5b. Widened pass — 31 files flagged, 12 near-misses triaged to benign

The second pass also surfaced 21 files whose *base-owned* symbols (upstream code the fork merely carried) are gone. Per D8 these are questions, not defects — each was traced:

- **Upstream deleted it too** (adopting the deletion is correct): `build_nous_subscription_prompt` + `_status_line` ← `4032a15ad0` (#95005); `computer_use_guidance` ← `3da5897c39`; `test_writable_close_retains_truncate_checkpoint` ← `ba80f3b86d` (#45383, PASSIVE-not-TRUNCATE — the test pinned the very behavior upstream removed); plus `_model_name_suggests_grok_4_3`, `_split_segment_tokens`, `_confirm_expensive_model_selection` and the rest, each already ledgered as superseded.
- **12 symbols still present upstream** looked alarming and were individually checked — **all benign detector artifacts**, because the AST index counts only `def`:
  - **import aliases:** `_completion_to_stream_chunks`, `_extract_tool_calls_from_text` (`agent/copilot_acp_client.py:25-26`, `import … as _…`)
  - **module-level aliases:** `_parse_systemd_duration_to_us` (`gateway/shutdown_forensics.py:520`), `_format_size` (`hermes_cli/backup.py:38`, now `sizefmt.format_bytes`)
  - **local closures:** `_strip_auth_on_cross_origin_redirect` (`tools/mcp_tool.py:3767`)
  - **ledgered supersessions:** `_resolve_prompt` (ledger L65, upstream `a0d406dcd8` personality module)
  - **substring false-positives:** `comparable`, `_status_line`, `test_nous_omits_disabled_reasoning`, `_enrich_with_attached_images`, `_schedule_replace_on_reboot`, `_start_anthropic_pkce` (matches are prose/docstrings/renamed survivors)

**Conclusion: no fork behavior vanished silently. The SUSPICIOUS bucket is empty and nothing in it was acked.**

## 6. 🟠 NEEDS-ATTENTION (1 item, NOT ACKED)

### `tests/gateway/test_undo_error_honesty.py::test_post_commit_head_read_failure_still_returns_committed_rewind`

- **Registry entry:** *"slash commands /undo and /redo (half-turn rewind with redo branch)"* — `lifecycle: fork-permanent`. This is a fork-owned canary, so it is **not ackable** and its failure correctly holds the gate red.
- **Symptom:** the only failure in the manifest suite — `1 failed, 605 passed, 2 skipped` (89.9 s).
- **Root cause — a STALE TEST against a deliberate, ledgered architectural change, not merge damage.** `RESOLUTION-LEDGER-2026-08-29.md` L9 (`hermes_state.py` god-file, hunk 20) records: *"took upstream's in-transaction counter-recompute + head read; this RETIRES the fork's post-commit fail-soft head read BY CONSTRUCTION (that guard existed only because the read ran after commit — in-txn a failure rolls the rewind back, so no 'durable rewind reported as nothing changed' split exists)."* The merged tree carries that reasoning as an in-source comment at `hermes_state.py:14628-14634`, with the head read now at **L14643, inside** the `_do` transaction body.
- **The test pins the retired contract.** It arms a `_FlakyConn` that raises `OperationalError` on the `MAX(id)` read and asserts `rewind_to_message` returns a committed result with `new_head_id=None` instead of raising. In-transaction, that read raising is a transaction failure.
- **The hazard the test guards is genuinely closed — verified, not assumed.** `_execute_write` (`hermes_state.py:5477-5489`) wraps `fn` in `BEGIN IMMEDIATE` and rolls back on **any** `BaseException` before re-raising. So a head-read failure now rolls the whole rewind back and the caller's error is truthful — there is no "durable rewind reported as nothing changed" split for the user to retry into, i.e. no double-undo. The fork's fail-soft guard is redundant under the new structure.
- **What coverage it wants (orchestrator decision, one test file, no production change):** re-point this test at the *new* invariant — arm the same `MAX(id)` failure and assert (a) `rewind_to_message` **raises**, and (b) the transcript is **unchanged** (`active` message set identical before/after), which is the property that actually prevents the double undo. That is a strictly stronger contract than the old fail-soft assertion. Do **not** revert hunk 20 to restore the post-commit read: it would re-open the exact split the in-transaction form eliminates.
- **Sibling check:** the other three `/undo` canaries (`test_undo_redo_stack.py`, `test_undo_redo_half_turn.py`, `test_undo_drain_guard.py`) are **green**, so the feature itself is intact — only this one error-path probe is stale.

**Bucket 5 (genuinely-uncovered new fork delta with no test and no ledger explanation): 0 paths.** Every path in the uncovered set carried either a ledger entry, an upstream-side byte identity, a clean auto-merge with zero symbol loss, or an orchestrator-tooling provenance.

## 7. Canary + manifest suite state

All 6 fork canaries execute green inside the 50-nodeid manifest run:
`test_fork_canary_relay_lane_headers` · `test_fork_canary_discord_free_response` · `test_fork_canary_telegram_intake_sentinel` · `test_fork_canary_cron_cross_vendor` · `test_fork_canary_runtime_footer_fields` · `test_fork_canary_slash_registry`.

> ⚠️ **Registry hygiene note for the orchestrator (not gate-blocking):** 5 registry nodeids point at **`/tmp/fork-e2e-lane/tests/…`** rather than the in-repo `tests/fork_canaries/…` copies (identical files, same sizes). A `/tmp` path is not reproducible in CI or a fresh clone — those entries should be re-pointed at `tests/fork_canaries/` before the sync lands, or the gate silently degrades to a no-op on any machine without that scratch dir.

## 8. Post-ack gate state

Re-run: `python3.11 -m hermes_parity gates --stage manifest+forkdelta`

```
gate                status  seconds  detail
------------------  ------  -------  ------------------------------------------------------------------
manifest+forkdelta  FAIL    93.52    12 covered, 0 uncovered, 373 acknowledged path(s), 0 lint error(s)
```

**The path-coverage arm and the lint arm are both fully GREEN** — `0 uncovered`, `0 lint errors` (was `367 uncovered, 3 lint errors`). The gate FAILs solely on `manifest_test_exit=1`, i.e. the single stale fork-permanent canary in §6.

Evolution of the three original RED signals:
1. ~~367 uncovered paths~~ → **0** (373 adjudicated and acked with per-path evidence).
2. ~~3 lint errors~~ → **0** (fixed by the canary lane's 22-entry registry v2; no ack taken).
3. **manifest test exit=1** → **STILL RED — the sole remaining blocker**, and correctly so: it is a real stale-test defect on a fork-permanent feature, not paperwork.

**Acking would have hidden a red fork canary; instead the gate is honest-RED on exactly one well-characterized, one-file fix.** Once §6 lands, expect **PASS** with the 373 acks intact.
