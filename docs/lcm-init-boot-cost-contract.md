# LCM store init: the boot-cost contract

**Why this document exists.** On 2026-09-22 the default gateway (Apollo) froze twice in one
day — every in-flight turn parked for ~21–25 minutes per boot, no log line naming the cause.
Both times the cause was a **one-time row backfill in `MessageStore._init_db` that had no
done-marker and no index**, so it re-scanned the whole `messages` table (10.9 GB, 2.5 M rows,
zero rows needing work) at every process start, while every other turn waited on the
process-wide engine-load lock. Each backfill was correct and cheap when it landed (June, August)
and nobody re-measured as the DB grew. Fixes: fork PRs #887 (`search_content`), #902
(`ingested_at`), and this document's regression layer.

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
   row-bounded** (`WHERE store_id`/`session_id`, `LIMIT ?`) **or marker-gated.**
4. **Real work is batched and committed per batch** (`BACKFILL_BATCH_ROWS`), so a kill
   mid-backfill keeps its progress instead of redoing everything next boot.

## The three enforcement layers (all in `tests/context_engine/`)

| Layer | File / test | What it catches | Proven red on |
|---|---|---|---|
| Source contract | `test_lcm_backfill_cost.py::test_init_path_has_no_unmarked_full_table_writes` | walks every method `_init_db` calls; any unbounded, unmarked `UPDATE`/`DELETE … messages` fails, naming the method | injected `UPDATE messages SET source=… WHERE source IS NULL` into `_ensure_source_column` |
| Query plan | `test_lcm_init_cost_regression.py::test_second_open_issues_no_full_scan_of_messages` | traces every statement a second open issues, `EXPLAIN`s each on a fresh connection, fails on a bare `SCAN messages`; **a statement that cannot be explained is a finding, never a skip** | the 2026-09-22-morning `store.py` (both scans named), and tonight's gate alone |
| Scale ratio | `…::test_init_time_does_not_scale_with_row_count` | 200 vs 20 000 rows, second-open time ratio must be < 5× | belt-and-suspenders; too small to feel a scan on its own |
| Live signal | `plugins/context_engine/__init__.py` `PHASE=context_engine_load_slow` | any engine load that holds or waits on `_LOAD_LOCK` ≥ `HERMES_ENGINE_LOAD_SLOW_S` (5 s) logs one WARNING naming held/waited seconds and pointing here | — (observability, not a gate) |

Measured baseline, real fleet DB (10.9 GB), fixed tree: **init = 0.14 s over 99 statements**;
slowest surviving statement is a 67 ms FTS block write. `/tmp/lcm-init-trace.py`-style replay:
wrap `sqlite3.connect` with `set_trace_callback`, open `MessageStore(db_path=<real>)`, sort by
inter-statement wall time.

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
- Regression layer + slow-load signal: this PR.
