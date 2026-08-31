# RESOLUTION BRIEF — parity sync 2026-08-29
## fork/main (de200ebbf5) ← upstream origin/main (26350357d7), 5,374 commits, merge-base 1e5b50744

You are a resolution relay worker on a STAGED MERGE in THIS worktree
(`~/.hermes/worktrees/parity-2026-08-29`, branch `sync/upstream-2026-08-29`).
MERGE_HEAD exists. Your job: resolve remaining conflicts per the rules below.
**You are BARRED from: committing the merge, running `hermes_parity finish`, pushing,
or touching any tree outside this worktree.** The orchestrator (Apollo) reviews and lands.

## PROTOCOL (read first, every worker)
1. Read `docs/sync/review/RESOLUTION-LEDGER-2026-08-29.md` FIRST. Do NOT redo files
   already ledgered. Append one line per file you resolve:
   `| path | choice U/B/F/UP/TEST | why | residual risk |`
   (U=union, B=both-interleaved, F=fork side, UP=upstream side, TEST=test-contract driven)
2. Heartbeat: append `date +%T` + current file to `/tmp/parity-w-heartbeat.txt` at each file START.
3. Work EASY→HARD in the phase order below. `git add` each file as you finish it (durable).
4. Per-file after resolving: `grep -cE '^(<<<<<<<|=======|>>>>>>>|\|\|\|\|\|\|\|)' <f>` must be 0,
   then `python3 -m py_compile <f>` for .py files.
5. If YOU near your context ceiling: stop clean, append to the ledger, report honestly.
   An honest ceiling-stop with a clean ledger is the CORRECT outcome. Never rush god-files.
6. Another worker may be live on this tree. Before trusting a file as "already resolved,"
   re-grep markers yourself. Never redo a sibling's in-flight file; take the next one.

## METHOD RULES (per conflict class)
- **Both sides changed same region:** understand INTENT of each. Upstream refactors win
  structurally; fork BEHAVIOR must survive re-threaded into the new structure. Never
  blind-pick a side on a semantic hunk.
- **Upstream ABSORBED a fork feature (AA, or our code visible upstream evolved):** upstream's
  copy is canonical BASE; re-apply only genuine fork deltas on top.
- **theirs-empty hunks in tests/** (upstream deleted region, fork modified): QUADRUPLE rule —
  drop a `def test_*` ONLY if (1) inside the theirs-empty region AND (2) existed at merge-base
  AND (3) absent from upstream's version of the file AND (4) fork body == base body
  (AST-normalized). Fork-authored OR fork-MODIFIED tests MUST survive. Only ~5 such hunks
  exist this sync — do them by hand, carefully.
- **Conflicted test file = the staged test contract IS the spec.** If neither side's impl
  satisfies it, hand-author the hybrid (upstream mechanism + fork contracted deltas).
- **Locales (17 files):** sole conflict is usually one block; take upstream for upstream-owned
  keys BUT diff the RESOLVED file against fork HEAD's FULL key SET
  (`git show fork/main:locales/en.yaml`), and re-add EVERY fork-only key block
  (absent upstream AND absent at merge-base ⇒ fork-owned, keep fork values unconditionally).
  Do NOT trust "no fork-only keys" without the full key-set diff — this trap has bitten twice.
- **Fork-inline vs upstream-extracted duplicate:** if upstream extracted a helper to a module
  and imports it, DROP the fork inline copy, keep the import. Grep the import block before
  keeping any fork-side inline function.
- **Dual-param contracts:** when both sides added params to the same function, thread BOTH
  through EVERY helper in the delegation chain (grep each param name in helpers' signatures).
- **After taking upstream for a var/decl:** grep the var's USE count vs the fork side and
  restore any dropped consume site (orphaned-call-site class).
- **Never script-excise duplicates.** Byte-identical dup defs = defer to post-merge PR.
  Duplicate `def test_` names in a merged file = silent assertion loss — check with
  `Counter(re.findall(r'def (test_\w+)', src))`, pick survivor by distinguishing content.

## ALREADY RESOLVED (do not touch)
- `apps/desktop/**` — WHOLESALE upstream (D9: desktop is upstream-owned, not forked). Staged.
- `contributors/emails/*` — upstream side. Staged.
- `apps/desktop/.../gateway-event.ts` — git rm (upstream deleted). Staged.

## PRE-CLASSIFIED SPECIALS
- **DU `plugins/memory/mem0/_backend.py`, `_setup.py`, + their tests:** fork RESTRUCTURED
  mem0 (content consolidated; fork has no `_backend.py`/`_setup.py` — see
  `git ls-tree fork/main plugins/memory/mem0/`). Upstream's in-range fix `e38cca50d6`
  ("keep Mem0 OSS OpenAI requests direct", adds `_openai_llm.py`) targets the OLD structure.
  Resolution: honor the fork deletion (git rm the DU paths), then PORT the SEMANTIC content
  of e38cca50d6 into the fork's structure (grep fork's `__init__.py`/modules for the
  OSS-OpenAI request path; if the fork already routes direct, ledger NO-PORT-NEEDED with
  evidence). Do NOT resurrect upstream's file layout.
- **DU `tests/run_agent/test_run_agent.py`:** fork deliberately SPLIT this 8,699-line monolith
  (commit 8f7c231c77) to fix CI shard timeouts. NEVER restore the monolith — git rm it.
  Then: 30 upstream commits touched it in range; for each
  (`git log 1e5b5074..origin/main --oneline -- tests/run_agent/test_run_agent.py`), check if
  its test additions/changes concern behavior the fork's split files
  (`tests/run_agent/test_*.py`) cover; port genuinely-new upstream tests into the matching
  split file. Ledger each commit ported / already-covered / upstream-only-API-skip.
- **AA `tools/browser_use_cli.py` + test:** fork cherry-picked the Browser Use CLI 3.0 family
  early (PR #585, `-x` provenance). Upstream side has evolved further. Take upstream as base,
  re-apply any fork-side fixes made after #585 (`git log fork/main --oneline -- tools/browser_use_cli.py`).
- **`.github/workflows/*`:** fork runs its own sliced CI. Preserve fork's slicing/branch
  config; adopt upstream's new steps only where they don't conflict with fork CI topology.

## FORK-CRITICAL FEATURES (from docs/sync/fork-features.json — MUST survive; canary tests gate)
- relay-pool session affinity + lane headers (`agent/fork_ext/relay_headers.py` + call sites)
- cron-subagent approval gate reads ContextVar not raw env
- systemd restart exits 0, darwin/launchd exits 75
- messaging + moa toolsets present
- cron scheduler per-job reasoning + script timeout helpers (`cron/fork_ext/scheduler_ext.py`)
- gateway restart policy/config bridge/initiator breadcrumb + failure-count entry codec
- gateway configured+persisted route identity helpers (footer provider field!)
- tool-search unwrap scope gate + delegate code_execution inheritance
- state pure fork helpers: denorm gate + platform/channel session-search matching
- hygiene compaction announces in-chat on success
- `agent/fork_ext/*` and `cron/fork_ext/*` modules and their 1-line call sites in god-files —
  when resolving a god-file, the fork_ext CALL SITES must survive (a dropped call site keeps
  unit tests green; grep `fork_ext` in every resolved god-file and verify ≥1 non-import use).

## PHASE ORDER (easy → hard; god-files LAST, ONE per worker)
- P1: locales/ (17) + website/ + scripts/ + .github/ + cli-config.yaml.example
- P2: tests/ (55 files; apply test rules above; conftest.py files FIRST)
- P3: agent/ (22) + tools/ (10) + plugins/ (5, incl. mem0 special) + providers/ + model_tools.py
- P4: hermes_cli/ except kanban_db.py (15) + cron/jobs.py + cron/lifecycle_guard.py + cli.py + run_agent.py
- P5: gateway/ except run.py (5) + tui_gateway/ except server.py (3) + hermes_state_common.py + hermes_state_schema.py
- P6 (ONE god-file per worker turn, fresh context each):
  gateway/run.py (41 hunks) · hermes_state.py (20) · hermes_cli/kanban_db.py (18) ·
  tui_gateway/server.py (16) · cron/scheduler.py (13)

## VERIFY AS YOU GO
- `git ls-files -u | awk '{print $4}' | sort -u | wc -l` = remaining count (report in ledger).
- For god-files: after resolving, run that file's own test file(s) if they exist
  (`venv/bin/python -m pytest tests/<match> -q -o addopts= -p no:randomly -x`) with
  `HERMES_HOME=$(mktemp -d)`.

## ADDENDUM (22:15) — ABSORPTION CENSUS RULINGS (see ABSORPTION-CENSUS-2026-08-29.md, 130 PRs)
These verdicts are EVIDENCE-BACKED (symbol probes vs origin/main). They override generic
"re-thread fork behavior" instincts for the named features — upstream's copy is CANONICAL:

- **tui_gateway/server.py + compute_host.py + host_supervisor.py:** our compute-host isolation
  (#63096, 95.2% src match) and ws-disconnect interrupt (#90373, 98.5%) were cherry-picked
  upstream WITH our authorship. Take UPSTREAM side for these subsystems; re-apply only
  fork deltas made AFTER our PR content (check `git log fork/main --oneline -- <file>` for
  commits newer than the PR'd work). Do NOT preserve fork-side divergence in
  compute_host/host_supervisor — upstream is a superset now.
- **gateway/run.py startup-restore gate:** #71903 absorbed 100% (commit 769dba175 authored
  by us, incl. `agent.gateway_startup_restore_drain_timeout` config key). Upstream side wins
  those hunks.
- **gateway/runtime_footer.py latency field:** #71990 absorbed 100%. Upstream wins.
- **hermes_state_search.py trigram guard:** #71932 absorbed via #77629 — and upstream MOVED
  the fix (rebuild code relocated; `_fts_rebuild_finish` mirror-gated). Upstream wins.
- **agent/auxiliary_client.py `model: auto` sentinel:** absorbed + upstream extended
  (2962ba2b7 superset). Upstream wins.
- **model-switch plugin-provider resolution:** upstream #52549 redesigned it (plugin discovery
  deliberately OUT of get_provider() hot path). If fork carries `_resolve_plugin_provider`
  in the switch hot path, DROP it — take upstream's placement.
- **tools/delegate_tool.py ContextVar propagation:** upstream generalized (parent-context
  snapshot + `_child_context.run` in the timeout executor + per-batch-child snapshots).
  Take upstream; drop fork's `tools/thread_context.py` shim IF it duplicates (verify usage
  count first — ledger the check).
- **kanban_db.py reclaim retry_status:** upstream now computes `retry_status = _retry...`
  (the bug #71980 targeted is fixed differently). Take upstream for that hunk.
- **Typing-indicator stale-path (gateway/run.py):** upstream's version is a superset
  (handles Slack thread-scoped stop). Take upstream; do not re-add fork's extra stop call.
- **patch_parser header-less V4A guard:** upstream `_validate_operations` covers it.
  Take upstream; drop fork's guard (double-guarding = double-counting).
- **slash-command handler bodies:** absorbed (87.5%) into upstream's GatewaySlashCommandsMixin.
  Upstream wins handler-body hunks; fork-only COMMANDS (e.g. /undo /redo /merge /branch /fast
  route-awareness) still must survive — those are fork features, not absorbed.
