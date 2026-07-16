# Message-write maintenance-adjacency audit

## Scope

Audited direct `messages` table inserts and their runtime callers in:

- `hermes_state.py`
- `run_agent.py`
- `agent/conversation_loop.py`
- `agent/tool_executor.py`
- `agent/conversation_compression.py`

The required literal gate is:

```sh
grep -nE 'INSERT INTO messages' \
  hermes_state.py \
  run_agent.py \
  agent/conversation_loop.py \
  agent/tool_executor.py \
  agent/conversation_compression.py
```

Because the literal is also a prefix of `messages_fts` and appears once in prose, every hit is classified below instead of being assumed to write the canonical table.

## Result

- The literal gate has 13 hits, all in `hermes_state.py`.
- Three hits are actual SQL inserts into the canonical `messages` table.
- Two canonical-table inserts are persistent production paths. Both call `_bump_effective_last_active_for_message` in the same `_execute_write` transaction.
- One canonical-table insert is an FTS health probe. It has no maintenance hook, but its transaction is unconditionally rolled back; it cannot leave either a message row or stale session recency behind.
- The four audited runtime files contain no raw `INSERT INTO messages` SQL. Their write paths route through `SessionDB.append_message` or `SessionDB.archive_and_compact`.
- One real maintenance-ordering gap exists in `SessionDB.import_sessions`: `_insert_message_rows` bumps recency before imported parent links are applied, and no recompute runs after the links are applied. Importing a compression root plus a fresher continuation child leaves the root's stored `effective_last_active` stale.

## Required literal-gate inventory

| Match | Classification | Maintenance status |
|---|---|---|
| `hermes_state.py:683` | Docstring text in `_db_opens_cleanly` | Not SQL |
| `hermes_state.py:710` | Canonical `messages` insert; rollback-only FTS health probe | No hook; exempt because success rolls back at line 714 and the error path rolls back at line 718 |
| `hermes_state.py:1081` | `messages_fts` trigger insert | FTS index write, not canonical table |
| `hermes_state.py:1093` | `messages_fts` trigger update/reinsert | FTS index write, not canonical table |
| `hermes_state.py:1111` | `messages_fts_trigram` trigger insert | FTS index write, not canonical table |
| `hermes_state.py:1123` | `messages_fts_trigram` trigger update/reinsert | FTS index write, not canonical table |
| `hermes_state.py:1395` | Base FTS rebuild | FTS index write, not canonical table |
| `hermes_state.py:1406` | Trigram FTS rebuild | FTS index write, not canonical table |
| `hermes_state.py:2039` | Trigram FTS migration/backfill | FTS index write, not canonical table |
| `hermes_state.py:2081` | Base FTS migration/backfill | FTS index write, not canonical table |
| `hermes_state.py:2100` | Trigram FTS migration/backfill | FTS index write, not canonical table |
| `hermes_state.py:5175` | Canonical insert in `SessionDB.append_message` | Hooked at lines 5215-5217 in the same `_execute_write` transaction |
| `hermes_state.py:5271` | Canonical batch insert in `SessionDB._insert_message_rows` | Hooked per row at lines 5298-5300 using the caller's live transaction |

A narrower canonical-table gate is:

```sh
grep -nE 'INSERT INTO messages([[:space:]]|[(])' \
  hermes_state.py \
  run_agent.py \
  agent/conversation_loop.py \
  agent/tool_executor.py \
  agent/conversation_compression.py
```

Expected canonical SQL sites: `hermes_state.py:710`, `hermes_state.py:5175`, and `hermes_state.py:5271` only.

## Canonical insert paths

### 1. `_db_opens_cleanly` health probe — unhooked, non-persistent exemption

- Insert: `hermes_state.py:710-713`.
- Transaction starts at `hermes_state.py:704`.
- Success path rolls back at `hermes_state.py:714`.
- `sqlite3.OperationalError` path rolls back at `hermes_state.py:717-719`.
- No `_bump_effective_last_active_for_message` or recompute is called.

This is mechanically the only direct insert without adjacency maintenance. It is not a persisted-message consistency gap because both the probe session and probe message are always rolled back. If a future edit commits or reuses this probe transaction, it must add maintenance or retain an explicit rollback-only invariant.

### 2. `SessionDB.append_message` — hooked

- Method: `hermes_state.py:5108`.
- Insert: `hermes_state.py:5175-5200`.
- Session counters: `hermes_state.py:5203-5214`.
- Maintenance: `_bump_effective_last_active_for_message(conn, session_id, message_timestamp)` at `hermes_state.py:5215-5217`.
- Transaction boundary: `append_message` returns `self._execute_write(_do)` at `hermes_state.py:5220`; `_execute_write` begins at `hermes_state.py:1458`, starts `BEGIN IMMEDIATE` at line 1477, and commits only after the callback returns at line 1480.

The counter updates sit between the insert and bump, but the insert, counters, and maintenance are atomic in one write transaction.

Runtime callers in the requested files are routed through this path:

- `run_agent.py:2220-2235` calls `SessionDB.append_message` from the flush path.
- `agent/conversation_loop.py:5245` calls the agent flush path before tool execution.
- `agent/tool_executor.py:131-147` defines the post-tool-progress flush helper, with calls at lines 220, 427, 1071, 1125, 1145, 1756, and 1788.

Neither `conversation_loop.py` nor `tool_executor.py` writes SQL directly.

### 3. `SessionDB._insert_message_rows` — hooked, with three caller paths

- Helper: `hermes_state.py:5222`.
- Per-row insert: `hermes_state.py:5271-5296`.
- Per-row maintenance: `_bump_effective_last_active_for_message` at `hermes_state.py:5298-5300`, before the next loop iteration.
- The helper receives the caller's live connection and therefore shares the caller's `_execute_write` transaction.

Callers:

1. `SessionDB.replace_messages` (`hermes_state.py:5308`) calls the helper at lines 5346-5348, then performs a full `_recompute_effective_last_active_for_session` at line 5353 before commit. Status: hooked and recomputed.
2. `SessionDB.archive_and_compact` (`hermes_state.py:5371`) calls the helper at lines 5410-5412, then performs a full recompute at line 5419 before commit. Status: hooked and recomputed. `agent/conversation_compression.py:1081` routes in-place compression through this method.
3. `SessionDB.import_sessions` (`hermes_state.py:7202`) calls the helper at lines 7464-7468. Status: per-row hook present, but post-link maintenance is missing; see Finding G1.

## Finding G1 — imported compression lineage leaves root recency stale

Severity: correctness gap.

`import_sessions` initially inserts every session with `parent_session_id = NULL` (`hermes_state.py:7401-7403`). `_insert_message_rows` then resolves and bumps each row while it is still a root (`hermes_state.py:7464-7468`). Only after all message inserts does `import_sessions` apply parent links (`hermes_state.py:7500-7508`). It returns at line 7517 without recomputing the linked child or its newly resolved compression root.

Observed runtime reproduction:

- Imported `root`: `end_reason='compression'`, own message timestamp `100.0`.
- Imported `child`: `parent_session_id='root'`, message timestamp `500.0`.
- `import_sessions` returned `ok=True`, `imported=2`, `detached=0`.
- Stored `root.effective_last_active`: `100.0`.
- Fresh `expected_effective_last_active('root')`: `500.0`.
- Stored `child.effective_last_active`: `500.0`.

The direct insert adjacency guard is green because `_insert_message_rows` contains the bump call, but the bump runs before the operation that changes the recency root. This is a caller-ordering blind spot, not a missing helper call at the SQL statement itself.

Recommended fix: after parent-link validation and updates, recompute each successfully linked child's session/root with `_recompute_effective_last_active_for_session` inside the same import transaction. Cover both an imported parent and a pre-existing compression parent.

## Existing guard and blind spot

`tests/hermes_state/test_session_list_denorm_reland.py:327-364` already provides a source-contract gate:

- It asserts the canonical insert functions are exactly `_db_opens_cleanly`, `SessionDB.append_message`, and `SessionDB._insert_message_rows`.
- It requires every production `SessionDB` method containing direct insert SQL to contain `_bump_effective_last_active_for_message`.
- It separately requires recompute calls in `replace_messages`, `archive_and_compact`, and `update_session_meta`.

The guard correctly detects a newly added direct insert that bypasses the helper. It does not detect a caller that invokes the hooked helper before later changing parent/root semantics. `import_sessions` is not in its recompute-required set.

## Grep-gated checklist

- [x] Broad literal gate accounted for all 13 matches.
- [x] Canonical-table gate reduced the inventory to exactly three SQL sites.
- [x] `hermes_state.py:710` classified as rollback-only and non-persistent.
- [x] `hermes_state.py:5175` verified adjacent to maintenance in the same transaction.
- [x] `hermes_state.py:5271` verified adjacent to per-row maintenance in the caller transaction.
- [x] `run_agent.py` has no raw canonical-table insert; flush routes through `append_message`.
- [x] `agent/conversation_loop.py` has no raw canonical-table insert; persistence routes through the agent flush/compression paths.
- [x] `agent/tool_executor.py` has no raw canonical-table insert; persistence routes through the agent flush path.
- [x] `agent/conversation_compression.py` has no raw canonical-table insert; in-place persistence routes through `archive_and_compact`.
- [x] All `_insert_message_rows` callers enumerated: `replace_messages`, `archive_and_compact`, `import_sessions`.
- [ ] `import_sessions` post-link recompute is missing; Finding G1 remains open.
