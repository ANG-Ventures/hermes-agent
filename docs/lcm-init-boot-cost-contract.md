# LCM store init: the boot-cost contract

**Why this document exists.** On 2026-09-22 the default gateway (Apollo) froze twice in one
day — every in-flight turn parked for ~21–25 minutes per boot, no log line naming the cause.
Both times the cause was a **one-time row backfill in `MessageStore._init_db` that had no
done-marker and no index**, so it re-scanned the whole `messages` table (10.9 GB, 2.5 M rows,
zero rows needing work) at every process start, while every other turn waited on the
process-wide engine-load lock. Each backfill was correct and cheap when it landed (June, August)
and nobody re-measured as the DB grew. Fixes: fork PRs #887 (`search_content`), #902
(`ingested_at`), and this document's regression layer.

## Freeze #3 (2026-09-24): parity COUNT plus a false-rebuild race

This was **not generic I/O contention** and not “Kanban is too busy.” The
restart burst created many LCM engine loads and active ingests; the *code* turned
that combination into a false FTS parity mismatch and an enormous synchronous
index rebuild. Aegis also uses LCM, but its independent gateway/database and
small, low-concurrency workload do not exercise Apollo's 2.7 M-row DB with
simultaneous Kanban children. A healthy Aegis chat is therefore no control for
this load pattern.

Measured on the live Apollo DB and an APFS clone, without mutating the live DB:

- `EXPLAIN QUERY PLAN SELECT COUNT(*) FROM messages` → `SCAN TABLE messages
  USING COVERING INDEX idx_msg_session_ts`; `messages_fts_docsize` → `SCAN
  TABLE messages_fts_docsize`. A covering index is still a traversal of all
  2.7 M rows. Cold clone: `MessageStore()` took 17.9 s; these two SELECTs
  occupied 11.1 s and 2.35 s. Warm-cache standalone counts (~0.07/0.05 s)
  understated the contended restart cost.
- The original `_fts_needs_rebuild_structural` issued **two autocommit SELECTs**.
  A concurrent valid message insert and FTS trigger commit between them made
  the two snapshots disagree. Reproduced with a real SQLite writer: **15/300
  parity calls falsely requested rebuild**; a deterministic interleaving test
  fails on fork/main and passes with both reads in one deferred read transaction.
- The real `apollo-loadlock-samples.log` sampled the lock holder at
  `db_bootstrap.py:2885` (`_drop_fts_table`) at 05:07–05:10, then at `:3023`
  (`INSERT INTO messages_fts(messages_fts) VALUES('rebuild')`) through 05:20;
  `PHASE=context_engine_load_slow held=779.8s`. A second rebuild ran about
  05:22–05:28. Up to 12 other threads waited on `_LOAD_LOCK`. These are direct
  stack samples of **rebuilding**, not an inference from a slow log.
- Two amplifiers were also observed: no-op engine loads unconditionally wrote
  the `fts_integrity_failed` DELETE and the `messages_dedup_v1`/schema markers,
  queuing behind the SQLite write lock; at session start, lifecycle GC ran
  `SELECT DISTINCT session_id FROM messages/summary_nodes` under `BEGIN
  IMMEDIATE` once its default 200-row threshold was exceeded (live: 11,108
  lifecycle rows). The three undecryptable NULL `search_content` rows were
  point-updated to NULL on **every** load, firing FTS update triggers despite
  no change. A separate cold-clone trace measured 56.7 s in one such FTS
  shadow write. These are distinct from the parity root cause and all sit on
  the same restart fan-out path.

**Code provenance (git, not chronology-as-causality):**

- `8b869633a4` (2026-06-16 initial LCM vendor) already has the two COUNTs in
  `db_bootstrap.py:308,317` and `MessageStore`/`SummaryDAG` registration calls.
  The parity design did **not** originate in September.
- `3174e25373` (2026-07-21) introduced the process-global `_LOAD_LOCK` to
  prevent half-import races. `27b617846e` (2026-08-06 re-vendor) preserved the
  COUNTs and the per-load FTS check; it did **not** newly introduce them.
- `d8b2d2448e` (#887, 2026-09-23) gated/indexed the `search_content` backfill;
  `4964ea0518` (#902) marked the `ingested_at` backfill. Neither changed
  `_fts_needs_rebuild_structural`. `4806d08c8b` (#903) added this init-cost
  test and slow-load logging, but the test **exempted `USING COVERING INDEX`
  and skipped SQL mentioning `messages_fts`**, so it passed the bad COUNTs.
  This PR strengthens the existing gate, rather than calling those prior fixes
  the cause of freeze #3.

**Fix:** The engine-load path does only O(1) FTS shape/metadata checks. The
parity count is independently throttled (default 6 h, persisted per FTS table
in metadata), dispatched with the existing background integrity worker, and
retained on explicit `/lcm doctor` repair. Its two counts share one SQLite read
snapshot. No-op loads take no SQLite write lock, NULL-to-NULL backfill writes
are skipped, and empty-lifecycle GC uses indexed point probes and an
in-process 6 h throttle rather than an immediate full `messages` traversal.
A structural corruption/missing table still takes the existing repair path;
background findings still surface through `/lcm doctor`.

**Scope of verification:** isolated worktree with `PYTHONPATH=<worktree>`;
`tests/context_engine/` passes. No fork merge, deploy or gateway restart is
part of this PR. The final operational gate is a restart with concurrent
Kanban workers and a real reply from Apollo; tests and a clone cannot claim
that user-visible result in advance.

## The contract

1. **`_init_db` is on every turn's critical path.** `load_context_engine()` runs it under
   `plugins/context_engine/__init__.py::_LOAD_LOCK`; every concurrent `init_agent()` blocks
   until it returns. Nothing in `_init_db` (or anything it calls) may cost O(rows).
2. **A one-time backfill is a MIGRATION STEP, not a startup check.** Gate it with
   `is_migration_step_complete(conn, STEP)` / `mark_migration_step_complete(conn, STEP)`
   (`db_bootstrap.py`, table `lcm_migration_state`). The marker is the only thing that makes
   "run once" true across boots. `search_content` additionally uses a partial index
   (`idx_msg_search_content_null`) so the presence probe is O(1) even before the marker exists.
3. **Any `UPDATE`/`DELETE`/`SELECT` against `messages` on the init path must be either
   row-bounded** (`WHERE store_id`/`session_id`, `LIMIT ?`) **or moved off the load path.**
   A covering-index scan is still O(rows). A marker does not make a full scan safe
   under `_LOAD_LOCK` on the first load after a restart or marker expiry.
4. **Real work is batched and committed per batch** (`BACKFILL_BATCH_ROWS`), so a kill
   mid-backfill keeps its progress instead of redoing everything next boot.
5. **Concurrent reads must share one snapshot.** Separate autocommit parity counts
   straddled a valid FTS-triggered ingest and falsely authorized a full inline rebuild.
   A no-op load must not acquire the SQLite write lock or erase a background finding.

## The enforcement layers (all in `tests/context_engine/`)

| Layer | File / test | What it catches | Proven red on |
|---|---|---|---|
| Source contract | `test_lcm_backfill_cost.py::test_init_path_has_no_unmarked_full_table_writes` | walks every method `_init_db` calls; any unbounded, unmarked `UPDATE`/`DELETE … messages` fails, naming the method | injected `UPDATE messages SET source=… WHERE source IS NULL` into `_ensure_source_column` |
| Query plan | `test_lcm_init_cost_regression.py::test_second_open_issues_no_full_scan_of_guarded_tables` | traces every statement a steady-state engine construction **+ `on_session_start`** issues on the loading thread, `EXPLAIN`s each on a fresh connection, and fails on **any** `SCAN` of `messages`, `messages_fts*`, `summary_nodes`, `nodes_fts*` — **`USING COVERING INDEX` is NOT exempt** (a covering-index `COUNT(*)` still reads every row); only `LIMIT`-bounded statements are. **A statement that cannot be explained is a finding, never a skip** | the 2026-09-22-morning `store.py`; fork/main `df435998f9` (freeze #3: `COUNT(*) FROM messages` → `SCAN messages USING COVERING INDEX`, `COUNT(*) FROM messages_fts_docsize`, lifecycle-GC `SELECT DISTINCT session_id` ×2) |
| Write lock | `…::test_engine_construction_needs_no_write_lock` | a steady-state engine construction must succeed while another connection holds `BEGIN IMMEDIATE`, busy timeout 0 — i.e. a no-op load issues **no write** and can never queue behind an in-flight writer | fork/main `df435998f9` (unconditional `messages_dedup_v1` / `schema_version` upserts, `_clear_integrity_failed` DELETE) |
| Flag survival | `…::test_ordinary_open_keeps_background_corruption_flag` | an ordinary open must not erase the background scan's corruption flag | fork/main `df435998f9` |
| Parity race | `…::test_concurrent_ingest_between_parity_counts_cannot_trigger_rebuild` | commits a real message + FTS trigger between the two count reads; a valid ingest must not authorize rebuild | fork/main: `assert not needs_rebuild` fails; fixed: one read transaction succeeds |
| Scale ratio | `…::test_init_time_does_not_scale_with_row_count` | 200 vs 20 000 rows, second-open time ratio must be < 5× | belt-and-suspenders; too small to feel a scan on its own |
| Live signal | `plugins/context_engine/__init__.py` `PHASE=context_engine_load_slow` | any engine load that holds or waits on `_LOAD_LOCK` ≥ `HERMES_ENGINE_LOAD_SLOW_S` (5 s) logs one WARNING naming held/waited seconds and pointing here | — (observability, not a gate) |

Measured baseline, real fleet DB (10.9 GB), fixed tree: **init = 0.14 s over 99 statements**;
slowest surviving statement is a 67 ms FTS block write. `/tmp/lcm-init-trace.py`-style replay:
wrap `sqlite3.connect` with `set_trace_callback`, open `MessageStore(db_path=<real>)`, sort by
inter-statement wall time.

## Health checks that are O(rows) — the throttled lane (freeze #3)

5. **A periodic health check is not a startup check either.** The FTS row-count parity check
   (`_fts_count_parity_mismatch`: `COUNT(*) FROM <content table>` + `COUNT(*) FROM <fts>_docsize`)
   is O(rows) — measured 13.4 s of a 17.9 s cold `MessageStore()` init on an APFS clone of the
   fleet DB. Until 2026-09-24 it lived inside `_fts_needs_rebuild_structural` and ran on **every**
   `MessageStore`/`SummaryDAG` construction under `_LOAD_LOCK`. Now:
   - the load path (`throttle=True`) runs only the O(schema) structural check;
   - parity is due at most once per `LCM_FTS_PARITY_CHECK_INTERVAL_HOURS` (default **6 h**; `0` =
     every startup but still in the background; `<0` = never on startup), tracked by the
     `metadata` key `fts_parity_checked_at:<fts table>`;
   - when due it is dispatched to the same daemon thread as the deep integrity-check (own
     connection, never under `_LOAD_LOCK`); a mismatch is recorded as the integrity-failed flag
     for `/lcm doctor`, **not** rebuilt inline;
   - the two COUNTs are read in one deferred transaction (one WAL snapshot). As two autocommit
     reads, a concurrent ingest between them gave a false mismatch 15/300 times (5 %), and each
     one triggered a full inline FTS rebuild (13 min + 6.5 min under `_LOAD_LOCK` on 2026-09-24);
   - explicit `/lcm doctor repair apply` (`throttle=False`) still runs parity + deep check
     synchronously.
6. **Per-session-start work is on the same critical path.** The empty-lifecycle GC
   (`LifecycleStateStore.prune_empty_sessions`) no longer holds `BEGIN IMMEDIATE` across two
   `SELECT DISTINCT session_id` full scans. It uses indexed per-session probes, takes the write
   lock only when there is something to delete, and runs at most once per
   `empty_lifecycle_gc_interval_hours` (default 6 h) per process.

## An FTS repair is one transaction (freeze #3, third defect)

`repair_external_content_fts` issues DROP TABLE / CREATE VIRTUAL TABLE / `'rebuild'` /
DROP+CREATE TRIGGER. Python's legacy sqlite3 isolation opens **no implicit transaction for
DDL**. Before t_d3963974's follow-up, each of those statements autocommitted on its own. For
the whole O(rows) rebuild (13–28 min on the fleet store), other connections saw the index
missing (`LCM ingest failed: no such table: main.messages_fts`, live 05:10) or empty
(docsize=0 vs 2.79 M, captured on disk), and a dropped trigger let concurrent inserts skip
the index. Any repair now runs under `BEGIN IMMEDIATE` with one COMMIT and rolls back on any
error. A no-op load still takes no write lock. The explicit-repair path was atomic only by
accident: #966's parity-marker write opened a transaction first. The engine-load
structural path was not. Gate: `test_lcm_fts_atomic_rebuild.py` (observer connection sees
no torn state; a failed rebuild leaves the old index), RED on dc228d5810.

## A structural rebuild never runs on the load thread (t_1e04c4bd)

With genuine structural damage (missing shadow table, non-FTS5 table, missing indexed
column), the load path (`throttle=True`) does only O(1) work. It drops the FTS triggers,
because a broken index makes every trigger-firing ingest raise. It sets
`fts_integrity_failed:<table>` and starts a daemon thread (`lcm-fts-rebuild-<table>`, own
connection). That thread runs the same one-transaction rebuild and recreates the triggers
before the single COMMIT, so rows ingested during the window get indexed. MATCH raises
until then, and search falls back to LIKE.

- Inline still applies when the content table has ≤ `INLINE_STRUCTURAL_REBUILD_MAX_ROWS`
  (1000) rows (bounded `LIMIT` probe). This covers a fresh DB with no FTS table yet. It
  also applies with `LCM_FTS_INTEGRITY_BACKGROUND=false`, and for an in-memory DB.
- Explicit `/lcm doctor repair apply` (`throttle=False`) stays synchronous.
- Not removed: the rebuild still holds the SQLite **write** lock for its whole duration
  (atomicity requires it), so ingests contend on the write lock for that time. Turns and
  engine loads no longer wait behind `_LOAD_LOCK`.
- `_drop_fts_table` restores stub shadow tables when `DROP` fails. A missing
  `<fts>_config` makes the FTS5 constructor, and so any DROP on a *fresh* connection, fail
  with `vtable constructor failed`, so before this every restart hit an unrepairable index.

Gate: `test_lcm_fts_deferred_structural_rebuild.py`. On #971's head all 3 tests are RED
(`vtable constructor failed`). With the deferral disabled, the load-thread test is RED.

## Adding a new column with a legacy backfill — the recipe

```python
_MY_BACKFILL_STEP = "messages_<col>_backfill_v1"

def _ensure_<col>(self):
    add_column_if_missing(self._conn, columns, "<col>", "ALTER TABLE messages ADD COLUMN <col> …")
    if not is_migration_step_complete(self._conn, _MY_BACKFILL_STEP):
        # batched, committed per batch, terminates on a no-progress batch
        …
        mark_migration_step_complete(self._conn, _MY_BACKFILL_STEP)
```

Then run `tests/context_engine/test_lcm_backfill_cost.py` and
`test_lcm_init_cost_regression.py`. If either is red, the backfill is on the boot path.

## How to recognise the class live

- `sudo py-spy dump --pid <gateway>` shows N threads in `load_context_engine → _load_engine_from_dir`
  and ONE in `lcm/store.py::_init_db` (or a callee). That one is the holder; the rest are victims.
- `gateway.log` shows `turn_slot_acquire` climbing with no `turn_slot_release` for minutes.
- From 2026-09-22 on: a `PHASE=context_engine_load_slow` WARNING with the held seconds.
- The unblock without a code change: build the missing index / write the marker on the live DB
  from a side process (`PRAGMA busy_timeout`), then the *next* boot is fast.

## Provenance

- Incident 1: 2026-09-22 11:30–11:51 PDT, `search_content` scan, PR #887 (`d8b2d2448`).
- Incident 2: 2026-09-22 20:56–21:20 and 21:28–21:38 PDT, `ingested_at` scan, PR #902 (`4964ea051`).
- Regression layer + slow-load signal: PR #903 (`4806d08c8`). Its plan check exempted
  `USING COVERING INDEX` and skipped every `messages_fts*` statement, so it passed on the freeze #3 code.
- Incident 3: 2026-09-24, FTS parity `COUNT(*)` on every load (card t_d3963974). 163×
  `PHASE=context_engine_load_slow`, held 49–64 s, waited up to 120 s, and two false-mismatch
  inline rebuilds (held 779.8 s). The count was introduced by the original vendor import
  `8b869633a4` (2026-06-16, `_fts_needs_rebuild_structural`). It was carried unchanged through
  re-vendor `27b617846e` and was not touched by #887/#902/#903. The DB grew until the count mattered.
