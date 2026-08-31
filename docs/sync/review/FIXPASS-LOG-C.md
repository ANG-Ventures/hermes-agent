# FIXPASS-LOG-C — Parity fix pass C (hermes_cli/ tui_gateway/ tools/ rest)

Card: t_dd16ac0d · Worktree: `~/.hermes/worktrees/parity-2026-08-29` @ sync/upstream-2026-08-29
Baseline (read-only): `~/.hermes/hermes-agent` @ de200ebbf5
Scope: `/tmp/parity-red-files.txt` lines NOT matching `^tests/(agent|cli|ci|computer_use|cron|gateway)/` → 63 files (`/tmp/parity-C-list.txt`).

Protocol: hermetic run (`scripts/run_tests.sh`, fresh `HERMES_HOME`) → baseline-binary
classify → fix → re-prove → git add. Two runs due to iteration-budget timeout on
attempt 1; attempt 2 resumed from `/tmp/fixpassC-run1.log` + re-established ground
truth with a fresh full run.

## Run ledger

| Run | Log | Result |
|---|---|---|
| run1 (attempt 1) | /tmp/fixpassC-run1.log | 63 files: 31 red at start of attempt |
| run2 (attempt 2 ground truth) | /tmp/fixpassC-run2.log | 13 red files (18 tests) after attempt-1 fixes |
| run3 (final re-prove) | /tmp/fixpassC-run3.log | 6 red files — 5 = MCP env-only, 1 = card-B STOP item |

## Attempt-1 fixes (from run-115, verified green in run2/run3)

- run_agent import cluster (7 files under tests/run_agent/) — torn-merge import
  targets; includes continuation ceiling upstream 3→4.
- tui_gateway/server.py duplicate method binding (test_server_no_duplicate_defs).
- tests/test_hermes_state.py — shadowed def + trace fix.
- register_skills torn merge — cleared tests/hermes_cli/test_web_server.py,
  tests/plugins/memory/test_discovery_sources.py, tests/test_plugin_skills.py.
- langfuse plugin, compression budget/closed-adoption clusters, state writer gates
  (test_no_locked_readers_gate: audit_effective_last_active routed via _read_ctx).

## Attempt-2 fixes (this run)

### 1. tests/test_guest_durability_barriers.py — macOS expectation
`test_guest_barriers_leave_synchronous_alone_when_unset` asserted synchronous
stays 1 when `database.synchronous` is unset, but `apply_durability_barriers()`
always runs `_enforce_macos_synchronous_full` (Darwin forces FULL=2 — the btree
corruption guard; deliberate, predates the fork commit 4882184e95 that added the
test on Linux CI). Test now expects 2 on darwin / 1 elsewhere. Baseline also
fails this on macOS (test file doesn't exist there; the sibling behavior does).
Verified: 3/3 pass.

### 2. Windows-footgun scan — plugins/memory/mem0/tests/test_capture_router_transient_drop.py
Bare `log.read_text()` → `read_text(encoding="utf-8")`. Verified: scan green.

### 3. scripts/run_tests_parallel.py — private per-file basetemp restored
The merge kept the fork's `PYTEST_DEBUG_TEMPROOT` temproot but dropped the
`--basetemp` pinning + Popen-failure cleanup that
tests/test_run_tests_parallel.py pins. Restored: one private dir serves as both
temproot and `--basetemp` (explicit caller `--basetemp` still wins → no runner
temp dir, no cleanup), spawn-failure path rmtrees it. Verified: 11 pass, 1 skip.

### 4. hermes_state.py — insert-path denorm contract (test_session_list_denorm_reland)
The rotation/archive tail clones (fork commits 652f5c2ebb/406c5daf04) were
merge-inlined as raw `INSERT INTO messages (` sites inside
`publish_compression_child` and `archive_and_compact`, violating the
insert-site allowlist. Extracted `SessionDB._clone_message_tail_rows(conn,
row_ids, session_id, retarget=)` — pure-SQL column clone, always ends with
`_recompute_effective_last_active_for_session` (strictly stronger than the
bump). Test allowlist extended with the new qualname and accepts recompute as
the denorm-adjacency witness. Verified: 25 pass (reland) + 20 pass (acceptance).

### 5. hermes_state.py — rotated-tail duplicate steer (test_compression_watermark_commit)
`_rows_to_conversation`'s exact-clone dedupe keyed on `msg.get("timestamp")`,
but the fork gates the message-shape timestamp behind `include_timestamp`
(off for resume/display) — key was always None, dedupe never fired, rotated
tail steers appeared twice in display_history. Keyed on `row["timestamp"]`
instead. Verified: 16/16 pass.

### 6. tests/hermes_state/test_composite_carrier_rewind.py — fork return-shape
Upstream test asserts `rewind_to_message` returns exactly 3 keys; the fork adds
`rewound_ids` (consumed by tui_gateway/server.py undo). Assertion updated to the
fork contract + `rewound_count == len(rewound_ids)` invariant. Verified: 12/12
pass (both hermes_state and tui_gateway variants green in run3).

### 7. optional-skills/security/1password/SKILL.md — description hardline
217 chars → "Fleet service-token op CLI secret reads, writes, injection." (59).
Fork commit 7d4f9eb63e had lengthened it past upstream's new 60-char gate.
Verified: test_authoring_standards fully green in run3.

## Env-only (NOT bugs — leave red in shared-venv runs)

tests/test_mcp_serve.py, tests/tools/test_mcp_capability_gating.py,
tests/tools/test_mcp_dashboard_oauth.py, tests/tools/test_mcp_elicitation.py,
tests/tools/test_mcp_oauth.py — need `mcp==2.0.0` (dev extra); the probed shared
venv (`~/.hermes/hermes-agent/venv`) carries mcp 1.x (`MCPError` import fails).
Re-proven live this run: `uv run --extra dev --python 3.11 -m pytest <5 files>`
→ **181 passed in 9.74s**. CI installs the dev extra; no code change needed.

## STOP item (card-B file ownership — orchestrator action needed)

tests/test_no_shadowed_test_definitions.py fails because
`tests/cron/test_parallel_pool.py:282 TestSyncMode.test_sync_false_returns_immediately`
duplicates line 233 — a torn merge kept both the fork's deterministic-barrier
version (233, from #458 wall-clock conversion) and upstream's stopwatch version
(282). Fix = delete the 282 copy (the 233 version supersedes it by design).
File is `tests/cron/` = card A/B territory; NOT touched here.

## Final state (run3, /tmp/fixpassC-run3.log)

63 files → 57 green, 5 MCP env-only, 1 card-B STOP. Zero in-scope red.

## Changed files (this attempt)

- tests/test_guest_durability_barriers.py
- plugins/memory/mem0/tests/test_capture_router_transient_drop.py
- scripts/run_tests_parallel.py
- hermes_state.py
- tests/hermes_state/test_session_list_denorm_reland.py
- tests/hermes_state/test_composite_carrier_rewind.py
- optional-skills/security/1password/SKILL.md
