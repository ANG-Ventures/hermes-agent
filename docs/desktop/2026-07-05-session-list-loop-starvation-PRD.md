# PRD: Dashboard `session.list` / `list_sessions_rich` loop-starvation on the ws path

- **Status:** ✅ APPROVED — Ace 2026-07-05, BUILDING (pass-15 folded post-approval: archive is a RUNTIME PREDICATE not a NULL-marker [ground-truthed :2901/2903], grep-gate widened to all parent_session_id writes, message.timestamp/started_at immutability asserted, filter-variant EXPLAINs required before close). Converged after 15 review passes: BLOCK×5, AWC×9, two consecutive 'None that block the design' with all load-bearing premises spike-proven or source-line-verified; remaining OQs are Phase-1 build measurements). BUILDING. Prior: v0.13 (pass-12 BLOCK folded — 2026-07-05). Pass-12 correctly BLOCKed: I'd carried
  "FOUR SET-NULL sites" since pass-8 without re-counting. GROUND-TRUTHED against source
  (`grep -nE "parent_session_id\s*=\s*(NULL|COALESCE)"`): there are **SIX** writers — **FIVE**
  SET-NULL sites (114, 4916, 5034, 5126, 5186) + the COALESCE upsert (1610). Critically, **all five
  SET-NULL sites are FK-safety orphaning during DELETION — they NULL a SURVIVING child's parent when
  its parent is deleted**, so a child that was a hidden compression continuation (NULL) becomes a
  standalone VISIBLE root and MUST be recomputed to non-NULL or it VANISHES from `session.list`
  (INV-7 violation). My "moot — row gone" was wrong: the PARENT is gone, the CHILD survives and
  changes visibility. Folded: count corrected to 6 everywhere; all 5 delete-orphan sites reclassified
  as surviving-child recompute; the COALESCE MAX-absorb transaction boundary pinned. This is the
  genuinely-final member of the maintenance enumeration — now ground-truthed against source, not
  memory. **v0.14 (pass-13 folded): pass-13 was APPROVE-WITH-CHANGES with "None that block the actual
  deployment" — the reviewer independently grep-verified the 6-writer count matches `main`. Folded
  the 3 deployment items: WAL-vs-DELETE-fallback contract stated CONDITIONAL (WAL host = INV-1 holds,
  network-mount = degraded, live box is WAL); rollback ORDER sequenced (code→index→column);
  delete-orphan recompute made UNCONDITIONAL (no fragile per-site proof); tool-member AC strengthened
  to positive-contribution. Design + enumeration complete and source-ground-truthed. Re-review to
  confirm convergence.**
- **Owner:** Apollo
- **Origin:** During the turn-isolation live certify, concurrent `session.list` load drove the
  dashboard event loop to 97% CPU with multi-second stalls. A live `sample` of the wedged
  process proved `pysqlite_connection_execute` with the **main asyncio thread starved on
  `take_gil`**. This is the read-path residual the eventloop-starvation PRD (2026-07-02) explicitly
  scoped OUT (RR1) and turn-isolation does NOT fix (it moved *agent turns* to a child; DB *reads*
  stay serving-side).

## 0. Phase-0 Ground-Truth (measured 2026-07-05, live state.db) — EXPANDED per pass-1

- **state.db:** 11,301 sessions / 216,567 messages / 3.2GB.
- **`list_sessions_rich(limit=400)` WARM scan: median 2068ms** (min 1871 / max 2275). COLD (fresh
  connection): 2551ms — cold≈warm, so the cost is CPU-bound query work, NOT disk I/O.
- **ROOT CAUSE — measured on a real DB copy 2026-07-05 (pass-2 forced this; my index theory was
  FALSE):** decomposed the query by timing each piece against `/tmp/state-idxtest.db` (a proper
  `sqlite backup` copy):
  - Preview subquery + MAX-timestamp subquery, plain `started_at` order, NO tip CTE: **16ms.**
  - Recursive compression-`chain` CTE alone (COUNT): **161ms.**
  - `chain_max` (the MAX-message-timestamp per chain, feeding `_effective_last_active`): **444ms
    in isolation**, but it **materializes over ALL 11,306 sessions.**
  - Full `list_sessions_rich(400)`: **2263ms.**
  - **Building the `(session_id, role, timestamp, id)` seek index and re-timing: 2263→2146ms — a
    ~5% change. The index is NOT the fix.** (This empirically kills v0.2's primary lever.)
  - **The 2s is the `_effective_last_active` ORDER BY forcing a FULL-TABLE tip materialization
    before `LIMIT 400`:** because the sort key is computed per-root from the compression chain +
    each member's max message timestamp, sqlite cannot apply the LIMIT early — it computes the
    effective-last-active for all ~11.3K sessions, sorts, THEN takes 400. That per-call
    full-table work is the wedge.
- **The prime fix is therefore to avoid recomputing the full-table tip ordering on every list
  call** (D-1, proven). **SPIKE RESULTS on a real 3.2GB copy (2026-07-05, pass-4 forced this):**
  - Root-stored `effective_last_active` column, indexed, but query unchanged (`SELECT s.*` + the
    preview/last_active subqueries computed for all rows before ORDER BY): **1769ms** — still slow,
    still `USE TEMP B-TREE FOR ORDER BY`. A stored column ALONE is not enough (the per-row SELECT
    subqueries force the full scan).
  - **UNIFORM-NULL projection (v0.8, pass-7 — the ONE-query proof):** `effective_last_active` is
    non-NULL **only for VISIBLE rows** (roots that are neither compression continuations nor
    deny-listed sources); every hidden row (continuation OR `tool`/denied source) is NULL. The inner
    query is therefore just `SELECT id FROM sessions WHERE effective_last_active IS NOT NULL ORDER BY
    effective_last_active DESC, started_at DESC, id DESC LIMIT 400`. SPIKED on the real copy:
    **`SEARCH sessions USING COVERING INDEX idx_eff (effective_last_active>?)` at 0ms** with NO
    `source` in the index or predicate. The full two-stage query = 291ms warm / 313ms.
  - **Correctness proven — ONE query, INCLUDING the deny-list (pass-7 Blocker 1):** the full
    two-stage query's top-400 ordered ids are **byte-identical** to the current CTE's top-400
    *with the `source NOT IN ('tool')` deny-list applied* on the real copy. The EXPLAIN (covering)
    and the byte-diff are the SAME verbatim production query — the deny-list is no longer a separate
    spike; it's folded into the NULL marker so ONE `IS NOT NULL` predicate expresses both hide-reasons.
  - `(effective_last_active>?)` in the plan IS SQLite's lowering of `IS NOT NULL` to an index range
    excluding NULLs (verified on the verbatim query) — plan and stated query match.
  - Backfill (once, via the chain CTE, sets non-NULL only on visible roots): **3.6s.**
- **Confirmed asymmetry + primitive mismatch (pass-1 Blocker 1+2, still valid):** REST
  `get_sessions` offloads via `_session_db_heavy_read_semaphore()` = **`asyncio.Semaphore(2)`,
  per-loop, async-acquired, HARDCODED to 2** (`web_server.py:1949`). The ws `session.list`
  (`server.py:5278`) dispatches via `asyncio.to_thread` (`ws.py:388`) → default executor → **NO
  bound.** The bound (D-1) is still needed as the concurrency safety net, but it is now SECONDARY
  to fixing the 2s single-call cost.

## 1. Summary & Goal

`session.list` (ws) runs `list_sessions_rich()`, which is slow (~2s on the 3.2GB DB) because its
`_effective_last_active` ORDER BY forces a full-table compression-tip materialization over ~11.3K
sessions before `LIMIT 400` (§0, measured — NOT the preview subquery, NOT an index, both empirically
ruled out). It runs on an **unbounded shared executor**, so concurrent list load both thrashes the
GIL to starve the loop AND can exhaust the shared `to_thread` pool. **Goal:** make `session.list`
fast AND unable to starve the loop, via:

1. **Root-stored `effective_last_active` + TWO-STAGE query (PRIMARY, D-1, SPIKE-PROVEN):** store the
   chain-MAX effective-last-active on the ROOT row, indexed, maintained on write; rewrite the query
   two-stage (inner LIMITs N on the indexed column — `SCAN USING COVERING INDEX`; outer enriches only
   those N). Measured **291ms warm / 313ms on a real 3.2GB copy (8× from 2263ms), top-N ordering
   byte-identical to the current CTE.** Both pieces required — a stored column without the two-stage
   rewrite is still 1769ms (the per-row SELECT subqueries force a full scan). Fixes the single-client
   stall, which a bound provably cannot (pass-2 measured a bare index at ~5%).
2. **Bound concurrent heavy reads (SECONDARY, D-4):** acquire the SAME bound the REST path uses,
   **async-side before `to_thread` dispatch** (seam a — NOT inside the worker, which would exhaust
   the shared pool per pass-1 Blocker 1). This caps GIL-thrash under concurrency.

## 2. Non-Goals

- NOT turn isolation (shipped separately; this is the read path it left).
- NOT a read-replica / separate read-connection process (RR1) — future PRD, gated on the fix below
  proving insufficient at peak.
- NOT vacuuming / pruning `state.db` (standing user directive: leave state.db data alone — this
  spec ADDS a column + index, it does not prune/vacuum/rewrite existing rows).
- NOT changing `session.list`'s response contract (fields, ordering semantics, deny-list,
  compression-tip projection). The denormalized column must produce the SAME ordering the live CTE
  produces — byte-identical rows (INV-2).
- NOT weakening the compression-tip projection (which rows are hidden) — that logic is preserved
  exactly; only the *ordering cost* is denormalized.

## 3. Constitution / Invariants

- **INV-1 (loop liveness under read load — two arms, pass-2 Blocker 3 fixed to p99/max; WAL-scoped
  pass-14):** *(These gates hold on a WAL host — state.db's normal case; on a DELETE-fallback host
  (network mount) read-liveness is DEGRADED during maintenance writes, see D-10/INV-5. The live
  target is WAL.)* (a)
  **single-client:** a lone `session.list` never stalls the loop — post-fix `list_sessions_rich(400)`
  warm AND cold **p99 (not median) < 350ms, max < 1s** on the live 3.2GB DB (spike measured 291/313ms on the real projected query;
  350ms is the gate for the DEFAULT no-filter list — the case spike-proven at 291/313ms; **filter
  variants (pass-15): source/search filters can hit non-indexed columns and lose covering — Phase-1
  MUST EXPLAIN the common source/search variants and prove `SCAN USING COVERING INDEX` (add index
  columns as needed) BEFORE close; a deferred filter EXPLAIN is a deferred INV-1a proof).** **AND the EXPLAIN must show the inner LIMIT is
  index-served (`SCAN USING COVERING INDEX`), not `USE TEMP B-TREE` over the full table** (pass-4 Blocker 1:
  a latency number on a warm quiet copy can hide a full-table sort; gate on the PLAN). Median is a fake gate
  for a tail-event stall (pass-2 B3). (b) **concurrent:** the hardened certify runs **K=8** (see D-6
  for K derivation) concurrent list callers for 300s → zero `event loop stalled`, REST loop-liveness
  p99 < 1s.
- **INV-2 (contract byte-stability — the ordering is the risk):** `session.list` rows — fields,
  ordering (the `_effective_last_active DESC` order the CTE produces), deny-list, compression-tip
  projection — are byte-identical before/after the denormalization. *Closeout proof:* a golden test
  seeds a fixture WITH deny-list (`tool`) rows, compression roots+continuations (multi-hop chains),
  tied timestamps, AND multi-source rows; asserts the denormalized-column query returns the
  byte-identical ordered result as the current CTE query, over the SAME fixture. This is the
  load-bearing invariant — the denormalized `effective_last_active` MUST equal the CTE's computed
  value for every row, including mid-chain and just-compressed sessions. **Strengthened (pass-3
  Required Change): beyond the synthetic fixture, the closeout ALSO runs a top-N byte-diff on a real
  3.2GB state.db COPY** — the new denormalized query's top-400 ordered ids must exactly equal the
  current CTE query's top-400 on live-shaped data (the fixture alone can't catch a
  distribution-dependent ordering miss; the real-copy diff is the true gate). **The CTE is RETAINED
  (pass-8) as the reconcile-audit's ground-truth oracle** — the two-stage query replaces it on the
  READ path, but it stays live as the correctness reference (a future cleanup must NOT delete it) and
  uses the shared `_LIST_DENY_SOURCES` constant (D-8).
- **INV-3 (maintenance correctness — the denormalized value never goes stale):** `effective_last_active`
  is updated on EVERY event that changes it, with the RIGHT operator per direction (D-2):
  message insert → monotonic `MAX` bump of the chain-ROOT (UPWARD); compression split → new
  continuation born NULL (visibility marker), standalone session born non-NULL=started_at (bimodal
  birth); delete/message-delete of a chain member → **full chain-MAX RECOMPUTE of the root** (DOWNWARD — MAX
  can't lower); `reopen_session` → recompute; **archive → NO-OP for the stored value (runtime
  predicate, pass-15).** *Closeout proof:* a test drives EACH
  mutation — including a DOWNWARD arm (delete the max-contributor, assert the root DROPS to the new
  true MAX) — and asserts the stored column == a fresh CTE recompute after each; a periodic
  reconcile-audit (read-only, defined SLA) logs drift LOUD. Audit-window tolerance (pass-6): a missed
  UPWARD bump self-heals on the next message; a missed DOWNWARD recompute does NOT self-heal and
  persists to the audit window — so the downward paths must be exhaustive, not audit-backstopped.
- **INV-4 (ONE shared concurrency bound — one primitive, one object, one config source):** the ws
  and REST heavy-read paths acquire the **same `asyncio.Semaphore` object, async-side**, so total
  in-flight heavy scans ≤ the bound across BOTH surfaces (no double-book). Only implementable if the
  ws path acquires async-side BEFORE `to_thread` dispatch (seam a); a `threading.Semaphore` inside
  the worker (seam b) is a different primitive AND fills the shared executor pool (pass-1 Blockers
  1+2). *Closeout proof:* a test injects a controllable scan delay so K > bound calls provably
  OVERLAP, then asserts observed max-concurrency ≤ bound across ws+REST driven together.
  **Single-loop verified (pass-14): ws `@app.websocket("/api/ws")` and REST `@app.get` routes are on
  the SAME FastAPI `app`, served by one `uvicorn.Server` via `asyncio.run(_serve())`
  (`web_server.py:12821, 14316`) — ONE event loop, so D-5's per-loop accessor yields ONE shared
  `asyncio.Semaphore`, total in-flight ≤ bound across BOTH surfaces. INV-4 holds (not two loops /
  2×bound). Ground-truthed against source.**
- **INV-5 (state.db migration is additive, reversible, approval-gated):** the change is `ALTER
  TABLE sessions ADD COLUMN effective_last_active` + a backfill + an index — all ADDITIVE (no existing row
  data rewritten destructively, no drop). Ships behind explicit user approval. **Rollback DDL
  ORDER (pass-3 Required Change): `DROP INDEX` FIRST, then `DROP COLUMN`** (SQLite ≥ 3.35 required
  for `DROP COLUMN`; dropping a column still referenced by an index errors). The backfill of ~11.3K
  sessions runs once during the migration while the gateway is quiesced (no concurrent message
  inserts — see R3), so a backfilled `effective_last_active` can't be stale-on-landing. *Closeout proof:*
  the migration is idempotent (`IF NOT EXISTS`), the backfill re-runnable, and the rollback test
  asserts DROP-INDEX-before-DROP-COLUMN + the SQLite version guard restores the pre-migration schema.
  **Rollback ORDER (pass-13): revert CODE FIRST (the two-stage query references `effective_last_active`)
  → THEN `DROP INDEX` → THEN `DROP COLUMN`; a schema-rollback ahead of the code revert would leave live
  code selecting a dropped column. State the three-step order in the rollback runbook.**
- **INV-6 (shed path is observable — pass-1 Required Change):** when the concurrency bound
  sheds/queues a list call, it increments a counter + logs queue-wait, so production never silently
  sheds list load. *Closeout proof:* an AC asserts the shed counter increments under saturation and
  is surfaced in status/logs.
- **INV-7 (the `effective_last_active` column encodes recency AND visibility, uniformly — pass-7):**
  **`effective_last_active IS NOT NULL` ⟺ the row is VISIBLE in the list** (a root that is neither a
  compression continuation NOR a deny-listed source); NULL means hidden-for-ANY-reason (continuation
  OR denied source), NOT "no activity". A non-NULL value is the chain-MAX recency of a visible row.
  This is now a clean biconditional (pass-7 fixed the predicate-vs-NULL split that made the old INV-7
  false for `tool` rows). *Closeout proof:* the list query's visibility predicate is EXACTLY
  `effective_last_active IS NOT NULL` (no separate source/parent predicate); a grep-gated invariant
  that no consumer reads the column as a plain "last active timestamp" without the NULL check; a test
  asserts a `tool`-source standalone row is stored NULL (hidden) and excluded from the list.

## 4. Resolved Decisions

- **D-1 (FULL FIX — root-stored `effective_last_active` + TWO-STAGE query, SPIKE-PROVEN).**
  Add a stored `effective_last_active` REAL column on `sessions` (NULL for hidden continuations),
  indexed `(effective_last_active DESC, started_at DESC, id DESC)` (uniform-NULL folds the deny-list
  into the NULL marker — no `source` needed, pass-7), backfilled once from the chain CTE (3.6s). Rewrite `list_sessions_rich`'s order-by path as a TWO-STAGE
  query: inner `SELECT id FROM sessions {where} ORDER BY effective_last_active DESC, started_at
  DESC, id DESC LIMIT ?` (index-served, `SCAN USING COVERING INDEX`), outer joins those N ids and
  computes the preview/last_active subqueries for only those N. Measured **291ms warm / 313ms**
  (8× from 2263ms), top-N ordering **byte-identical** to the current CTE (§0). A stored column
  WITHOUT the two-stage rewrite is NOT enough (1769ms — the `SELECT s.*` subqueries force a full
  scan); both pieces are required.
- **D-2 (maintenance — insert=monotonic-MAX; downward events=RECOMPUTE; NULL-continuation).**
  `effective_last_active` on a ROOT row = MAX over its chain of each member's last message ts; hidden
  compression continuations carry NULL (the visibility marker, §0). Maintenance events:
  (1) **message insert** → resolve the insert session's chain-ROOT (UPWARD parent-walk child→parent
  via `parent_session_id`; ≤ chain depth, once per turn, NOT per list call). If that ROOT is VISIBLE
  (non-deny-listed), `effective_last_active = MAX(effective_last_active, new_message_ts)` (MONOTONIC —
  pass-5 Blocker 2; a blind SET could LOWER it on a stale/older-ts insert). If the root is
  deny-listed it stays NULL (hidden). The MAX on a currently-NULL visible root treats NULL as −∞
  (first message makes it non-NULL = visible).
  (2) **row birth (uniform-NULL, pass-7)** → `effective_last_active` is born non-NULL (= started_at)
  ONLY for a VISIBLE new row: a standalone session (no parent) whose source is NOT deny-listed. It is
  born **NULL** for any hidden row — a compression continuation (child of a compression parent) OR a
  deny-listed source (`tool`). So a compression split births the continuation NULL; a `tool`-source
  session is born NULL. This is the single visibility marker INV-7 depends on.
  (3) **DOWNWARD event — delete of a chain member, or message-delete (pass-6 Blocker 2; archive
  REMOVED pass-15 — see below)** →
  a monotonic MAX CANNOT lower a value, so deleting/archiving the member holding the chain-MAX
  requires a **full chain-MAX RECOMPUTE of the root** (the backfill CTE, scoped to one chain), not a
  MAX bump. **Empty-root fallback (pass-8 Blocker 2):** if the recompute runs over ZERO remaining
  messages (e.g. a visible standalone session whose only message was deleted), it falls back to
  `started_at` — MATCHING the CTE's `COALESCE(MAX(msg.ts), started_at)` birth semantics — NOT NULL
  (NULL would wrongly HIDE a still-visible session). The row stays listed at `started_at`. **Epoch
  note (pass-9, ground-truthed): `started_at` and `message.timestamp` share the same `time.time()`
  float-seconds epoch** (message_timestamp = `time.time()` at `hermes_state.py:3266`; started_at
  passed as `time.time()` in create_session), so the fallback orders correctly against
  message-derived values in the same ORDER BY.
  **Recency-key immutability (pass-15, ground-truthed): `message.timestamp` and `started_at` are
  never UPDATEd in place (grep `UPDATE messages SET timestamp` / `SET started_at =` = 0 matches) — so
  the recency determinant only changes via insert (up) or delete (down), both enumerated; no hidden
  in-place-timestamp-mutation drift class.**
  **CTE-shape verified (pass-14): the CTE is `FROM sessions s LEFT JOIN chain_max`
  (`hermes_state.py:2988-2989`) with last_active = `COALESCE((SELECT MAX(m2.timestamp) ...),
  s.started_at)` — a CORRELATED subquery, NOT an inner join on messages. So a zero-message visible
  session IS returned at `started_at`; the denorm's fallback-to-started_at MATCHES (does not add a
  phantom row). Fold direction confirmed against source, not asserted.**
  (3b) **only-message-deleted** — see the empty-root fallback above.
  This is the only non-monotonic path; it's rare and off the hot read path.
  (4) **`reopen_session`** → clears `ended_at`/`end_reason`; the reopened chain's root MUST recompute
  (same as (3)). Explicitly handled.
  (5) **`parent_session_id` change → recompute BOTH affected roots (pass-8 Blocker 1 + pass-9
  Blocker: membership changes on TWO sides).** `source` is immutable after insert (NOT in the
  upsert's `ON CONFLICT DO UPDATE SET`, `hermes_state.py:1610`) — a deny-listed row can't later
  become visible. BUT `parent_session_id` IS mutable (COALESCE-fill on upsert + FOUR
  `SET parent_session_id=NULL` sites). A membership change touches TWO roots and BOTH must be
  maintained (pass-9 — recomputing only the mutated row is the same non-self-healing downward-drift
  class as D-2(3)):
  - **Branch-detach subtree X from a SURVIVING root R:** (i) X becomes a new root → recompute X
    (chain-MAX-or-`started_at`, may become visible non-NULL); (ii) **R LOST X's subtree → R gets a
    full chain-MAX RECOMPUTE (DOWNWARD, D-2(3) machinery)** — if X held R's chain-MAX, R was
    stale-high and would mis-order upward. (The deletion-FK-safety detach is moot — the old parent is
    being deleted — but branch-detach from a surviving root is the real gap.)
  - **COALESCE-merge on upsert (bare row learns its parent):** (i) the row becomes a continuation →
    NULL; (ii) **the new-parent root MUST `MAX`-absorb the merged subtree's max** — else the merged
    session is understated, can fall below the top-400 fold and vanish (a monotonic bump only heals
    on the next message, which may never come).
  *This is a new maintenance site the design depends on — enumerated, gated by an AC that asserts
  BOTH roots (not just the mutated one).*
  **Concurrency (pass-9): a branch-detach (mutating `parent_session_id`) and a concurrent message
  insert (which walks child→root via `parent_session_id`, D-2(1)) can resolve to the pre- or
  post-detach root.** Resolution: both maintenance ops run inside the SAME write transaction as the
  mutation they react to (the detach recomputes both roots atomically; the insert's walk+MAX is one
  txn), so the walk sees a consistent parent chain; the reconcile-audit is the backstop for any
  residual race. Stated, not left implicit.
  (6) **archive is a RUNTIME PREDICATE, NOT a NULL-marker hide-reason (pass-15, ground-truthed).**
  `list_sessions_rich` filters `s.archived` as a PARAMETERIZED where-clause (`hermes_state.py:2901/2903`
  — it can list archived=0 OR archived=1 depending on the caller), and `set_session_archived`
  (`:2554`) doesn't touch messages. So archive does NOT change `effective_last_active` (recency is
  unchanged; visibility toggles per-query, not per-row) — the two-stage inner query keeps the
  existing `s.archived = ?` predicate ALONGSIDE `effective_last_active IS NOT NULL`. archive is
  therefore NEITHER a NULL-marker reason NOR a maintenance event — INV-7's biconditional is scoped to
  the NON-archive dimension; the archive filter composes on top. (This corrects the pass-6 error of
  listing archive as a downward recompute event — it's a no-op for the stored value.) **The index
  must still cover the archived predicate: add `archived` to the covering index OR confirm the
  archived filter over ~400 candidate rows is negligible (build-time EXPLAIN, like the deny-list arm).**
- **D-3 (correctness guard — the sealed-parent invariant is ENFORCED, not hoped; pass-4 Blocker 3).**
  The design's correctness rests on "the stored root value == the true chain-MAX." Two defenses:
  (a) **write-path guard** — `add_message` into ANY chain member updates the ROOT's stored value via
  the monotonic `MAX(effective_last_active, new_ts)` (D-2), regardless of which member it targets, so
  even a stale/orphan/reordered/older-ts insert into a not-yet-sealed or reopened parent can only
  RAISE (never lower) the root — it stays == the true chain-MAX; (b) **reconcile-audit** — a
  periodic read-only pass recomputes `effective_last_active` from the CTE for a sample and logs any
  drift LOUD with a **defined detection latency** (e.g. hourly), promoted from "defense-in-depth" to
  a real gate with an SLA. An AC injects a stale-write-to-sealed-parent race and asserts the
  ordering does not corrupt.
- **D-4 (async-side shared bound — seam a, pass-1 Blockers 1+2; SECONDARY safety net).** The ws
  heavy-read handlers acquire the shared `asyncio.Semaphore` in the async layer BEFORE
  `asyncio.to_thread` dispatch — mirroring REST's `async with _sem: await _blocking_io(...)`. Never
  a blocking acquire inside the shared `to_thread` pool. Post-D-1 the scan is fast so the bound
  rarely bites, but it remains the safety net against a concurrency spike or a pathological session.
- **D-5 (single config source — pass-2 Required Change: verify NOT frozen at import).** REST's bound
  is a module-global `asyncio.Semaphore(2)` (`web_server.py:1949`). Refactor to a lazy,
  loop-bound accessor reading `dashboard.heavy_read_max_concurrency` (default 2) — constructed on
  first use per loop, NOT at import (pass-2 flagged the config-singleton-at-import risk). The ws
  path acquires the SAME object. One key, one object, no import-time freeze.
- **D-6 (K and cold sizing).** K for the concurrent certify = **8** — derived from the observed live
  storm (the resume-picker refresh + multiple dashboard clients + the certify's own readers that
  produced the original wedge); this is ≥ the concurrency that caused the incident, not K=1 theater
  (pass-2 Required Change). Cold scan is 2551ms pre-fix (§0); post-fix spike 291ms warm / 313ms (real projected query, §0); target p99 < 350ms, proven on a cold copy.
- **D-7 (fail-loud shed, observable — INV-6).** On bound saturation, list calls queue (bounded
  wait); past a ceiling, return a structured retryable "backend busy" error; every shed increments
  an observable counter + logs wait time.
- **D-8 (single-source the deny-list set — pass-7 Required Change).** The `{'tool'}` deny-list is
  used in FIVE places now (pass-8 expanded): the existing CTE (retained as the audit oracle, D-3b),
  the backfill, the birth rule, the insert-path deny-recheck (D-2(1)), and the reconcile-audit's CTE
  oracle. ALL FIVE derive from ONE module constant `_LIST_DENY_SOURCES`, so a future deny-list
  addition can't drift one path from another (duplicated-normalizer class — esp. dangerous if the
  AUDIT ORACLE hardcodes `'tool'` while the constant grows: the audit would then fail to detect the
  very divergence it exists to catch). *Closeout proof:* grep shows one definition; the CTE/backfill/
  birth/insert-recheck/audit-oracle all reference it; NO other hardcoded `'tool'` in list paths.
- **D-9 (compression does NOT prune messages — pass-7 Required Change, GROUND-TRUTHED).** Verified in
  `agent/conversation_compression.py`: a compression split creates a NEW child session +
  `parent_session_id` link and leaves the parent's message rows intact (the "Removed from … N
  messages" text is the live-CONTEXT trim, not a DB delete). So compression is NOT an unlisted
  downward event — the chain retains all members' messages, and the root's chain-MAX only ever grows
  from compression. The only true DOWNWARD events remain archive/delete/message-delete/reopen (D-2(3)).
- **D-10 (ONE maintenance chokepoint — the STRUCTURAL fix for the pass-5→9 miss-cadence, pass-10).**
  Passes 5-9 each found one newly-missed maintenance site because `effective_last_active` was
  maintained per-enumerated-event with no single guarantor. Fix (mirroring D-8's one-constant
  discipline for the deny-list):
  - **`parent_session_id` maintenance-adjacency (pass-11 amended — FIVE writers, incl. the atomic
    upsert):** the FIVE SET-NULL sites (114/4916/5034/5126/5186) route through ONE `_set_parent_session_id(child, parent)`
    wrapper that fires both-root recompute. The FIFTH writer — the get_or_create upsert's
    `ON CONFLICT DO UPDATE SET parent_session_id = COALESCE(...)` (`hermes_state.py:1610`) — CANNOT
    be split into the Python wrapper without breaking upsert atomicity (it would fire maintenance on
    a half-built row), so it stays an atomic single statement AND fires its both-root MAX-absorb
    inside the SAME upsert transaction. **Grep gate (pass-11): every `parent_session_id` write site
    is MAINTENANCE-ADJACENT — accompanied by a both-root recompute in the same txn — NOT "no write
    outside the wrapper" (which would false-positive on the upsert or force an unguarded carve-out).
    The gate enumerates exactly SIX sites (5 SET-NULL + 1 COALESCE upsert); a SEVENTH unlisted writer fails the gate. **Gate WIDENED (pass-15): derive the expected list from `grep -nE "parent_session_id\s*=" hermes_state.py` (ALL writes, not just NULL|COALESCE) PLUS `INSERT INTO sessions` column lists — so a concrete-value re-parent `SET parent_session_id = :pid` cannot escape as a silent 7th writer. Build-time: prove the only write-matches are the 6 known + the birth INSERT, or route the escapee through the wrapper.** (Ground-truthed 2026-07-05: no concrete-value re-parent exists in current source — the widened grep returns only the 6 + read-side JOINs; the widening is future-proofing.)
  - **ONE `_recompute_effective_last_active(root_id)` function** (the scoped chain-MAX-or-started_at,
    the D-2(3) machinery) is the single implementation every maintenance path calls — insert bump,
    downward recompute, both-root detach, reopen, empty-root fallback. No path hand-rolls the value.
  - **The FIVE `SET parent_session_id=NULL` sites, correctly counted + classified (pass-12 —
    ground-truthed `hermes_state.py:114, 4916, 5034, 5126, 5186`):** ALL FIVE are FK-safety orphaning
    during DELETION — `UPDATE sessions SET parent_session_id=NULL WHERE parent_session_id IN (doomed)`
    — which NULLs the parent pointer of a SURVIVING child whose parent is being deleted. **The parent
    is gone (moot for it); the CHILD survives with parent_session_id NULL→ so a child that was a
    hidden compression continuation (`effective_last_active` NULL, INV-7) BECOMES a standalone VISIBLE
    root and MUST be recomputed to its chain-MAX-or-started_at — else it stays NULL and VANISHES from
    session.list (pass-12 Blocker 1; my prior "moot — row gone" was wrong, the child is the row that
    matters).** So each of the five sites fires a surviving-orphan recompute (the D-2(3)/both-root
    machinery) for every child it orphans, in the same delete transaction, ordered after any D-2(3)
    delete-recompute of the same root. All five route through the `_set_parent_session_id` wrapper
    (or an explicit maintenance-adjacent call in the delete txn). **The recompute fires
    UNCONDITIONALLY on every orphaned survivor (pass-13): the four delete sites (4916/5034/5126/5186)
    are generic `_do` delete closures whose orphaned set is not statically known to exclude
    continuations — so rather than a fragile per-site "excludes continuations" proof (the pass-10
    "moot was too broad" trap), the recompute runs for EVERY orphaned child. Firing it on a row that
    was already a visible root is a harmless no-op (recomputes to the same value); firing it on a
    former continuation correctly promotes it to non-NULL. No per-site proof needed — unconditional
    is cheap (scoped chain-MAX) and correct by construction.**
  - **Transaction discipline (pass-10 B2 + pass-11: ALL three membership mutations):** the insert
    child→root walk + root UPDATE (D-2(1)), the detach both-root recompute, AND the upsert
    COALESCE-merge new-parent-root MAX-absorb (pass-11) each run under **`BEGIN IMMEDIATE`** (write
    lock acquired at the READ half, not upgraded mid-way), so a concurrent membership mutation cannot
    strand a stale root between the walk-SELECT and the UPDATE; `SQLITE_BUSY` → retry the whole walk.
    **WAL journal mode is REQUIRED for INV-1 liveness (pass-11): under a rollback journal a held
    write-lock blocks readers → violates INV-1 (session.list liveness) while maintenance runs; only
    WAL lets the read path proceed concurrently. RESOLUTION (pass-13, ground-truthed): SessionDB
    deliberately falls back to `journal_mode=DELETE` on network mounts (NFS/SMB/FUSE/WSL1) where WAL's
    fcntl locks don't work (`hermes_state.py:128-143`, `_WAL_INCOMPAT_MARKERS`) — and under DELETE,
    a maintenance write BLOCKS readers. The contract is therefore CONDITIONAL: (a) on a WAL host
    (state.db's normal case — verified wal on the live Mac-local-SSD box 2026-07-05) INV-1 holds
    fully; (b) on a DELETE-fallback host (network mount) INV-1 is DEGRADED — maintenance writes
    briefly block session.list; this is stated as a known limitation, NOT a silent violation. The
    migration RECORDS the journal mode at apply time; it does NOT brick a DELETE-fallback host (that
    would break every state.db feature) — it accepts the degraded read-liveness there. The live
    deployment is WAL, so INV-1 holds for the actual target.**
    Connection topology + the `busy_timeout`/retry-ceiling interaction are build-time implementation
    (the `busy_timeout`, if set, silently blocks instead of surfacing BUSY — the build must handle).
    **COALESCE MAX-absorb boundary (pass-12 Blocker 3): the upsert's SET is atomic (safe), but the
    pass-9 new-parent-root MAX-absorb is a SECOND statement (read merged child's chain-MAX → UPDATE
    the new-parent root). It MUST be wrapped WITH the upsert in one `BEGIN IMMEDIATE` txn (upsert +
    absorb together), NOT a follow-on autocommit statement — else it has the identical read-then-write
    race. Same for the delete-orphan recompute: the surviving-child recompute runs in the delete's
    own `BEGIN IMMEDIATE` txn.**
  - **Reconcile-audit is SAMPLED (D-3b) → defense-in-depth ONLY**, cannot gate the rare
    parent-mutation race; correctness rests on the exhaustive wrapper + `BEGIN IMMEDIATE`. *Closeout
    proof:* grep gate (no maintenance outside the two functions) + a concurrent detach+insert AC.

## 5. Architecture / Design

**Mechanism (measured 2026-07-05, §0):** `session.list` is slow because `_effective_last_active`
(computed per-root from the compression chain + each member's `MAX(message.timestamp)`) is the
ORDER BY key, forcing a full-table materialization over ~11.3K sessions before `LIMIT 400`.

**Fix (D-1, primary — SPIKE-PROVEN):**
1. Add `sessions.effective_last_active` (stored REAL = chain-MAX last-active on the ROOT row),
   indexed `(effective_last_active DESC, started_at DESC, id DESC)` — NO `source` needed (uniform-NULL
   folds the deny-list into the NULL marker, pass-7). Backfill once from the chain CTE (3.6s).
2. Maintain it on write via ONE chokepoint (D-10): a single `_recompute_effective_last_active(root)`
   + a single `parent_session_id` mutation wrapper (both-root recompute), grep-gated, all under
   `BEGIN IMMEDIATE`. Insert=monotonic MAX; delete/archive/reopen/detach=recompute (D-2).
3. Rewrite `list_sessions_rich`'s order path TWO-STAGE: inner `SELECT id ... ORDER BY
   effective_last_active DESC, started_at DESC, id DESC LIMIT ?` (index-served, `SCAN USING COVERING
   INDEX` — proven), outer joins those N ids and computes the preview/last_active subqueries for only
   those N. **Both pieces required** — the column alone leaves the `SELECT s.*` subqueries scanning
   all rows (1769ms); the two-stage limits enrichment to N (279ms). Byte-identical top-N ordering.

**Fix (D-4, secondary safety net):** async-side shared `asyncio.Semaphore` before `to_thread`
dispatch (seam a), bounding concurrent heavy scans across ws+REST.

**Why not seam (b):** `asyncio.to_thread` shares the loop's default `ThreadPoolExecutor` (~32) with
ALL ws dispatch (`ws.py:388`); a gate inside the worker fills the pool with blocked threads and
starves every cheap ws op. The bound must live async-side.

## 6. Implementation Phases

- **Phase 1 — Denormalized column + maintenance + backfill (D-1/D-2, the primary fix, approval-gated).**
  - *Unit:* migration adds `effective_last_active` + index idempotently (`IF NOT EXISTS`); backfill
    is re-runnable; rollback (`DROP INDEX` THEN `DROP COLUMN`, SQLite≥3.35) restores prior schema
    (INV-5). Maintenance: inserting a message bumps the tip's chain-ROOT `effective_last_active`;
    reopen recomputes (INV-3) — asserted vs a fresh CTE recompute after each mutation.
  - *E2E:* on a real 3.2GB copy, backfill then time `list_sessions_rich(400)` warm AND cold →
    **p99 < 350ms, max < 1s + EXPLAIN index-served** (INV-1a; spike 291/313ms). Golden byte-identical ordered rows vs the current CTE query
    over a fixture with deny-list + multi-hop chains + tied timestamps + multi-source (INV-2).
  - *Negative:* a drift-audit test corrupts a stored value and asserts the reconcile-audit logs it
    loud (INV-3 defense-in-depth); the golden test catches any ordering divergence.
  - *Gate:* explicit Ace approval before the `ALTER TABLE`/backfill runs on live state.db (INV-5) —
    **granted 2026-07-05** for the additive, reversible column.
  - *Verify with:* `pytest tests/state/test_effective_last_active_denorm.py` + live before/after p99.
- **Phase 2 — Async-side shared bound (D-4/D-5, seam a; secondary).**
  - *Unit:* refactor REST semaphore to a lazy loop-bound accessor reading
    `dashboard.heavy_read_max_concurrency` (NOT import-frozen — pass-2); ws heavy handlers acquire
    the SAME object async-side; injected-delay test proves K>bound overlap, observed max ≤ bound
    across ws+REST (INV-4).
  - *E2E:* hardened certify runs **K=8** concurrent ws `session.list` for 300s against a fixture
    whose scan cost is within a stated band of the live number (unfailable-fast-fixture guard);
    zero `event loop stalled`, REST loop-liveness p99 < 1s (INV-1b).
  - *Negative:* (1) saturate the bound → non-heavy ws ops stay responsive (seam-a proof, no executor
    exhaustion); (2) shed counter increments + logs (INV-6).
  - *Verify with:* `pytest tests/tui_gateway/test_session_list_bound.py` + the live certify.

## 7. Roadmap (version ladder)

| Version | What ships | Trigger | Maps to |
|---|---|---|---|
| v0.1 | root-stored `effective_last_active` + two-stage query + maintenance (D-1/D-2) | now — Ace-approved schema change; the single-client fix | Phase 1 |
| v0.2 | async-side shared bound (D-4/D-5) | now — the concurrency safety net | Phase 2 |
| v1.0 | read-replica / separate read-conn (RR1) | denorm+bound still can't hold INV-1 at peak | future PRD |

## 8. Risks & Mitigations

- **R1 — the denormalized value diverges from the true CTE value (correctness).** The dominant risk:
  a missed maintenance path leaves `effective_last_active` stale → wrong ordering. Mitigated by
  INV-3 (maintenance on every mutation, asserted vs CTE recompute) + a read-only reconcile-audit
  that logs drift loud + the golden ordering test. If any path is missed, the audit surfaces it
  rather than silently mis-ordering the resume picker.
- **R2 — the two-stage query mis-orders vs the CTE.** The two-stage is EXACT, not an over-fetch
  window: the inner query LIMITs the exact N by `(effective_last_active DESC, started_at DESC, id
  DESC)` — the same sort key the outer preserves — so the top-N is provably the CTE's top-N (proven
  byte-identical on the real copy, §0). The residual risk is only a maintenance bug that makes a
  stored value wrong (R1 / the monotonic-MAX guard), not the query shape. Golden test (INV-2) +
  real-copy top-N diff are the gates.
- **R3 — the backfill races a concurrent writer.** DEMONSTRATE the quiesce, don't assume it
  (pass-8). **PRIMARY path (pass-10): run the backfill PRE-ws-accept** — inside the gateway-start
  path BEFORE the ws server accepts connections (no session RPC can insert yet), so there is no
  concurrent writer at all. The post-reopen catch-up recompute is a FALLBACK only and is
  known-INCOMPLETE (its `last_active > backfill_start_ts` filter catches upward advances but MISSES a
  DELETE during the window) — so pre-accept is strongly preferred. Backfill measured 3.6s (one
  transaction). *Closeout proof:* an AC starts a writer during the backfill and asserts the
  post-migration value is correct, with pre-accept as the tested primary path.
- **R4 — INV-1's concurrent proof depends on the companion hardened certify harness** (built +
  circuit-breakered, companion SPEC); its fixture must reproduce the real scan cost or the gate is
  vapor (folded as the Phase-2 fixture-cost band + K=8).
- **R5 — write-path latency on the hot message-insert path: BOUNDED, measured (pass-4 corrected).** The
  reviewer flagged "single indexed UPDATE on the same row" as wrong — CORRECT for multi-session chains:
  the value is on the ROOT, so a tip insert needs a bounded tip→root walk to find+update the root
  (for a single-session chain the root IS the tip, so it's a same-row bump). The maintenance is a
  bounded upward child→parent walk (via `parent_session_id`; NEW helper, NOT `get_compression_tip`
  which is the inverse root→tip forward walk) + a single-row root UPDATE, run once per turn on the insert path, OFF the read hot path. **MEASURED on a real copy
  (pass-5 Required Change): 0.021ms for a root session (common case), 0.147ms for a 50-deep chain
  worst case** — negligible against a multi-second turn. R5 corrected: it's NOT a same-row bump like
  `message_count` for multi-session chains — it's a bounded upward walk; measured negligible.
  **Bulk-orphan delete (pass-14 residual): a delete that orphans a WIDE subtree fires N scoped
  recomputes in one `BEGIN IMMEDIATE` (compression chains are linear so N≈1, but branch-detach
  implies trees) — measure the wide-orphan-delete write-lock window in Phase 1 before close; R5's
  0.147ms is single-insert, not wide-orphan.** Reconcile-audit SLA (pass-14): the sampled audit's
  per-row detection latency = cadence ÷ sample-fraction — state the sample size at build so the
  effective latency is pinned, not left implicit (it's defense-in-depth only; downward paths are
  exhaustive by the chokepoint).

## 9. Open Questions

1. Where-filter variants (user source/search filters on the list) — the 291ms is proven for the
   default no-filter list; a filter on a non-indexed column reintroduces per-row work. Phase 1 must
   EXPLAIN + measure the common filter variants (source, search) and index as needed (pass-5 residual).
2. Backfill strategy (one-shot vs batched, R3) — measure backfill time on the copy; pick before the
   live migration.

## 10. Acceptance Criteria

- [ ] Single-client: post-fix `list_sessions_rich(400)` warm AND **cold (on a real 3.2GB copy)**
      **p99 < 350ms, max < 1s** (NOT median — pass-2 B3; spike proved 291/313ms on the real projected query) **AND EXPLAIN shows the
      inner LIMIT index-served (`SCAN USING COVERING INDEX`), not a full-table temp-btree sort** (pass-4
      Blocker 1). Evidence: before/after p99+max + EXPLAIN QUERY PLAN on a real copy, both cache states.
- [ ] `session.list` rows byte-identical (fields AND `_effective_last_active` ordering) before/after,
      over BOTH (a) a fixture with deny-list + multi-hop compression chains + tied timestamps +
      multi-source, AND (b) a **top-400 byte-diff on a real 3.2GB state.db copy** (pass-3: the
      fixture alone can't catch a distribution-dependent miss). Evidence: golden diff test + real-copy
      top-N diff both empty.
- [ ] `effective_last_active` stays correct under every mutation (new message, compression split,
      archive/delete, **`reopen_session`**) — stored value == fresh CTE recompute after each. Evidence:
      `test_effective_last_active_denorm.py` mutation arms + reconcile-audit drift test.
- [ ] **Stale-write race (pass-4 Blocker 3):** injecting a stale/orphan message insert into a
      not-yet-sealed or reopened parent keeps the root's stored value == chain-MAX (ordering does not
      corrupt). Evidence: a race-injection test asserts post-insert ordering matches the CTE.
- [ ] **DOWNWARD event (pass-6 Blocker 2):** deleting/archiving the chain member holding the max
      timestamp triggers a chain-MAX RECOMPUTE and the root's `effective_last_active` DROPS to the new
      true MAX (a monotonic bump would strand a stale-high value). Evidence: a delete-max-contributor
      test asserts the root value drops + ordering matches the CTE.
- [ ] **Empty visible-root (pass-8 Blocker 2):** deleting the ONLY message of a visible standalone
      session recomputes to `started_at` (NOT NULL) — the session stays listed, byte-identical to the
      CTE. Evidence: a delete-only-message test asserts the row remains listed at started_at.
- [ ] **Birth/visibility gating (pass-8 Blocker 1):** (a) a deny-listed (`tool`) row is stored NULL
      and STAYS NULL across a later message insert (insert-path deny-recheck fires); (b) a
      `parent_session_id` mutation recomputes BOTH roots (pass-9): (b1) branch-detach subtree X from
      surviving root R where X held R's max → assert R DROPS to its new true MAX (not just that X
      flips); (b2) COALESCE-merge → assert the new-parent root MAX-absorbs the merged subtree's max.
      Evidence: both-root tests assert ordering matches the CTE after each mutation — testing only the
      mutated row is insufficient.
- [ ] **Maintenance chokepoint grep gate (pass-10 D-10):** no code writes `parent_session_id` outside
      the single wrapper; no maintenance path computes `effective_last_active` outside the single
      `_recompute_effective_last_active`. Evidence: grep gate (D-8 model) — one wrapper, one recompute,
      zero raw writes elsewhere.
- [ ] **Concurrent detach + insert (pass-10 Blocker 2 TOCTOU):** a branch-detach and a message insert
      on the SAME chain, interleaved, leave BOTH roots == fresh CTE (no stale-high root stranded
      between walk and update). Evidence: a concurrent-writer test under `BEGIN IMMEDIATE` asserts both
      roots correct; a deliberately deferred-txn variant is shown to FAIL (proving the lock matters).
- [ ] **Mid-chain delete with surviving root (pass-10):** deleting a mid-chain node whose FK-safety
      NULLs its children while the chain root survives triggers a surviving-root recompute (NOT moot)
      and the root's value matches the CTE. Evidence: a mid-chain-delete test.
- [ ] **Delete-orphan continuation→visible-root recompute (pass-12 Blocker 1):** deleting a
      compression PARENT whose surviving child was a hidden continuation (NULL) recomputes that
      orphaned child to its chain-MAX so it appears in `session.list` byte-identical to the CTE — it
      does NOT stay NULL and vanish. Evidence: a delete-compression-parent test over ALL FIVE
      SET-NULL sites asserts every orphaned child is recomputed + listed.
- [ ] **Upsert COALESCE-merge under concurrency (pass-11 Blocker 1+2):** a bare row learning its
      parent via the get_or_create upsert leaves BOTH the child (NULL) and the new-parent root
      (== fresh CTE, MAX-absorbed) correct — under a CONCURRENT message insert, both roots stay
      correct (upsert-merge maintenance is inside the same `BEGIN IMMEDIATE` txn). Evidence: a
      concurrent upsert-merge + insert test; the maintenance-adjacency grep gate lists exactly SIX sites (5 SET-NULL + upsert), derived from source at build time.
- [ ] **Deny-listed MEMBER in a visible chain (pass-11):** a `tool`-source session that is a MEMBER
      of a VISIBLE compression chain contributes its messages to the visible root's chain-MAX
      identically in the denorm and the CTE oracle (D-2(1) bumps on ROOT visibility regardless of the
      inserting member's source). Evidence: a fixture arm where the `tool` member's timestamp is the
      chain-MAX asserts the visible root's stored value EQUALS that tool member's timestamp (a
      POSITIVE-contribution assertion — the tool member's ts IS the value that sets the root), not
      merely that ordering matches; byte-identical to the CTE.
- [ ] **Archived-row handling (pass-15):** the INV-2 fixture includes an ARCHIVED row; the two-stage
      query with the `s.archived = ?` predicate returns byte-identical results to the CTE for BOTH
      archived=0 and archived=1 list variants (archive is a runtime predicate, NOT a NULL-marker; the
      stored value is unchanged by archive). Evidence: golden diff over both archive variants.
- [ ] **Deny-list single-source (pass-8):** grep shows ONE `_LIST_DENY_SOURCES` definition;
      CTE + backfill + birth + insert-recheck + audit-oracle all reference it; no other hardcoded
      `'tool'` in list paths. Evidence: grep gate.
- [ ] **Backfill quiesce (pass-8):** a writer inserting DURING the backfill leaves a correct
      post-migration value (backfill runs pre-ws-accept OR a post-reopen catch-up recompute fires).
      Evidence: a concurrent-writer test asserts no stale-on-landing value.
- [ ] **Tied-timestamp tiebreak (pass-8 residual):** two VISIBLE roots with identical
      `effective_last_active` order deterministically by `started_at DESC, id DESC` matching the CTE
      (not just tied hidden rows). Evidence: fixture arm with two tied visible roots.
- [ ] **ONE production query proves plan AND ordering (pass-7 Blockers 1+2):** the SINGLE verbatim
      inner query `WHERE effective_last_active IS NOT NULL ORDER BY ... LIMIT N` (uniform-NULL folds
      BOTH compression-hiding and deny-list into the NULL marker) is (a) `SEARCH USING COVERING INDEX`
      by EXPLAIN AND (b) top-N byte-identical to the current CTE *with its deny-list + compression
      projection applied*. Evidence: one artifact — verbatim query → its EXPLAIN → its byte-diff (all
      three of the same text). A `tool`-source row is stored NULL and absent from both. Spike: 0ms inner, byte-identical.
- [ ] **Write-path cost (R5) — MEASURED:** the per-turn upward-walk + monotonic root UPDATE is
      0.021ms (root) / 0.147ms (50-deep chain) on a real copy — negligible vs a multi-second turn.
      Evidence: spike numbers in §0/R5; build re-confirms delta < 1ms.
- [ ] **Reconcile-audit SLA (D-3):** the drift audit runs at its defined cadence and logs any
      stored-vs-CTE divergence LOUD. Evidence: an injected drift is detected within the SLA window.
- [ ] Concurrent: **K=8** `session.list` callers for 300s → ZERO `event loop stalled`, REST
      loop-liveness p99 < 1s. Evidence: hardened certify (fixture scan-cost within band of live) +
      `grep -c "event loop stalled"` == 0.
- [ ] While the bound is saturated, non-heavy ws ops stay responsive (no shared-executor
      exhaustion). Evidence: `test_session_list_bound.py` saturation arm.
- [ ] Total in-flight heavy scans ≤ bound across ws AND REST, forced-overlap. Evidence:
      injected-delay concurrency test asserts observed max ≤ bound.
- [ ] The REST semaphore reads config lazily (loop-bound), NOT frozen at import. Evidence:
      a test changing the config key value takes effect without reimport.
- [ ] Shed path observable. Evidence: shed counter increments under saturation + surfaced in status/logs.
- [ ] Migration is additive + reversible. Evidence: idempotent `IF NOT EXISTS` migration, re-runnable
      backfill, rollback test restores prior schema; diff shows no destructive DDL.
