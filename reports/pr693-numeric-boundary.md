# PR693 — unbindable numeric timestamps quarantined during recovery

## Scope

Isolated source repair for the ONE reproducible defect the parent proved
(`~/.hermes/plans/alerts-20260914-postdeploy/pr693-p1-adjudication.md`). The
refuted bad-shape allegations (missing status, list-valued terminal, bad result
type, string timestamp) were **not** touched — they are retained in this
branch's tests as passing controls. No live source/config/runtime/gateway
change; no push/PR/merge/deploy.

## Base / head

| | |
|---|---|
| base (pinned) | `186db3f7a8a899cd6377fadc11dfb649d6d4b1d9` |
| branch | `wt/t_72ee25ec` |
| head | see `git log -1` below (commit created after this report) |
| DEV worktree | `~/.hermes/hermes-agent/.worktrees/t_72ee25ec` |

## Defect

`tools/async_delegation_store._is_optional_number` accepted **any finite JSON
number**. But those values are bound as **sqlite3 parameters** by the mirror
writes, and sqlite3 refuses an `int` outside signed 64-bit with
`OverflowError: Python int too large to convert to SQLite INTEGER`.

`terminal.completed_at = 2 ** 100` is a finite JSON integer whose `float()`
succeeds, so it passed validation, then raised **out of** the per-record
recovery loop and aborted restoration of an unrelated healthy sibling.

Measured binding boundary (sqlite3, this interpreter):

```
OK   int 9223372036854775807          # 2**63-1
OK   int -9223372036854775808         # -2**63
FAIL int 9223372036854775808   OverflowError
FAIL int -9223372036854775809  OverflowError
FAIL int 126765060022822940149670320537  OverflowError   # 2**100
OK   float 1.2676506002282294e+30     # float(2**100) binds fine as REAL
```

So the correct predicate is **int64 range for ints, isfinite for floats** —
floats are not range-limited.

### Second SQL-bound field (the sibling path)

The parent's fixture only reached the **no-envelope** mirror path
(`_recover_abandoned_delegations`, `async_delegation.py:499`). I probed the
**deliverable** path and reproduced the identical abort through a field the
loader never validated at all:

```
tools/async_delegation.py:418 in _sync_completion
OverflowError: Python int too large to convert to SQLite INTEGER
  (reached via _recover_abandoned_delegations -> _sync_completion(terminal["payload"], ...))
```

`_sync_completion` binds the **envelope's own** `payload["completed_at"]` into
the same REAL column. Fixing only the record-level `terminal.completed_at`
left the same invalid input escaping through the sibling — verified by probe
with `terminal.completed_at` left healthy and only the outbox payload poisoned.

## Changed paths

| path | change |
|---|---|
| `tools/async_delegation_store.py` | +26 lines, 0 deletions — two guards + `_SQLITE_INT_{MIN,MAX}` constants |
| `tests/tools/test_async_delegation_numeric_binding_boundary.py` | new, 43 tests |

Guards (both at the existing canonical validation seam — `_load`'s per-record
quarantine + its `_is_optional_number` predicate):

- **A.** `_is_optional_number` rejects ints outside `[-2**63, 2**63-1]` before
  the existing isfinite conversion. Floats unchanged (`math.isfinite` only).
- **B.** `_load`'s `invalid_outbox` clause now also runs `_is_optional_number`
  over `outbox[].payload["completed_at"]`, closing the deliverable sibling.

No new dependency, no refactor, no `except Exception`, no coercion. Rejection
routes through the **pre-existing** per-record quarantine, so:

- malformed records are quarantined, not modified/resealed/dropped (asserted:
  the bad value is still byte-equal in the persisted JSON after recovery);
- healthy siblings restore;
- genuine DB/IO failure is untouched and still propagates loudly (the
  `RegistryError` / `raise` in `recover_abandoned_delegations` is unchanged).

## Test results (real output)

Interpreter: `~/.hermes/hermes-agent/venv/bin/python` (runtime venv used as
interpreter only). Every run under a disposable `HOME`/`HERMES_HOME`
(`mktemp -d`), `nice -n 19`, serial, no network/model calls. The new test file
asserts `Path(ad.__file__)` and `Path(store.__file__)` resolve inside this
worktree, and that `ad._db_path()` resolves inside `tmp_path`.

### 1. Reproduction of the parent's exact fixture (pre-fix, this worktree at the pin)

```
FAILED tests/tools/..._adjudication.py::...[sqlite-wide-time]
OverflowError: Python int too large to convert to SQLite INTEGER
tools/async_delegation.py:499: OverflowError
1 failed, 5 passed in 1.14s
```

Matches the parent's `1 failed/5 passed` exactly.

### 2. Baseline RED — new regression against the PRISTINE pinned base

Separate throwaway worktree detached at `186db3f7a8`, new test file copied in,
no source change:

```
10 failed, 33 passed in 2.33s
```

### 3. Corrected GREEN — fixed tree

```
43 passed in 1.60s
```

### 4. Focused delegation suite (7 files)

```
285 passed in 36.59s        (exit 0)
```
`test_async_delegation{,_registry_boundaries,_terminal_receipts,_persistence,_boundaries,_parking}.py`
+ `test_restored_delegation_ownership.py`

### 5. Canonical runner (`scripts/run_tests.sh`, per-file isolation)

```
=== Summary: 3 files, 250 tests passed, 0 failed (100% complete) in 19.5s ===
  ✓ test_async_delegation_numeric_binding_boundary.py (43✓)
  ✓ test_async_delegation_registry_boundaries.py (86✓)
  ✓ test_async_delegation_terminal_receipts.py (121✓)
```

### 6. Per-guard mutation proof (one guard at a time, md5-verified landing)

| mutation | md5 before → after | result |
|---|---|---|
| pristine | `8b00e6c6…` | **43 passed, 0 failed** |
| **A** revert int64 range check only | `8b00e6c6…` → `b21dde4a…` | **10 failed**, 33 passed |
| **B** revert outbox-payload check only | `8b00e6c6…` → `465333a9…` | **2 failed**, 41 passed |
| restored | → `8b00e6c6…` | byte-identical to pristine |

Both mutations used a literal `str.replace` with `assert count == 1`
anchor-drift guards. Both guards are **independently** load-bearing — neither
masks the other. Mutation A's reds name the specific unbindable values;
mutation B's reds are exactly the two deliverable-path tests, failing with the
real `OverflowError` at `async_delegation.py:418` (a behavior failure, not a
setup error).

Non-mutation-sensitive tests in the file are deliberate **controls**: the
bindable/healthy-timestamp rows, the null row, the non-number rows, and the
`sqlite3`-binds-what-we-accept pair. They document that the fix does not
over-reject; they are not claimed as gates.

## Coverage matrix in the new test file

- unbindable: `2**100`, `-(2**100)`, `2**63`, `-(2**63)-1`, `10**400`
- signed-64 inclusive edges accepted: `2**63-1`, `-(2**63)`
- huge **finite float** accepted: `float(2**100)` (binds as REAL — proven by a
  live `sqlite3` insert in the same file)
- non-finite rejected: `inf`, `-inf`, `nan`
- bool rejected (int subclass — pre-existing contract, kept)
- healthy int/float/zero/`None` accepted, and timestamp **identity** asserted
  both in the persisted JSON and in the mirror's `completed_at` column
- evidence preservation asserted for both bad paths
- healthy-sibling witness asserted **by delegation id** on both paths

## Scope caveats

- The malformed inputs are **synthetic**. No claim that such a timestamp has
  occurred in production, and no claim the ordinary writer can emit one.
- I did not re-run the full repo suite; only the delegation-focused files
  listed above (task budget). The `1191-style` matrix named in the card body
  was not located as a runnable artifact in the parent's docs — if Apollo has
  its concrete invocation, it should be re-run before merge.
- The refuted bad-shape cases were not modified.
- No live state, queue, config, cron or profile touched; nothing pushed.
- Review required. Apollo integrates into PR693 and owns CI/merge/deploy.

---

# Addendum — run 89 (review changes-requested round)

Review run 88 blocked the first pass: the same defect class still escaped
through two **unguarded conversions in the replay consumer itself**, both
inside the same per-record loop. Apollo independently confirmed the finding and
scoped the fix to this card. This addendum records that work.

## What was still broken (reproduced, not inherited)

`enqueue_pending_outbox` (`tools/async_delegation_store.py`) performs two
conversions on outbox event fields that `_load` never validated:

| Line | Conversion | Raises on |
|---|---|---|
| `:1491` | `now - created_at`, `(now - created_at) / 3600.0` | `-(10**400)` → `OverflowError: int too large to convert to float` |
| `:1479` | `int(payload.get("attempt_generation") or -1)` | `inf` → `OverflowError`; `nan` → `ValueError` |

Measured on the pre-fix head `ef35977041` in this worktree, with an imported SUT
asserted to be this tree and a disposable `HOME`/`HERMES_HOME`:

```
SUT: .../.worktrees/t_72ee25ec/tools/async_delegation_store.py
RESULT control:    queued=['bad', 'good']        <- control arm: loop works
RESULT created_at: ABORTED ALL -> OverflowError: int too large to convert to float
RESULT gen_inf:    ABORTED ALL -> OverflowError: cannot convert float infinity to integer
RESULT gen_nan:    ABORTED ALL -> ValueError: cannot convert float NaN to integer
```

The control arm passes, so these are behavior failures, not fixture artifacts.
Post-fix, the same probe:

```
RESULT control:    queued=['bad', 'good']
RESULT created_at: queued=['good']              <- healthy sibling still replays
RESULT gen_inf:    queued=['good']
RESULT gen_nan:    queued=['good']
```

## The change

`tools/async_delegation_store.py`, same canonical `_load` `invalid_outbox`
computation the first pass already touched, `+13 / -0`:

- reject an outbox event whose `created_at` fails `_is_optional_number`
- reject an event whose `payload["attempt_generation"]` fails `_is_optional_number`

No new error handling, no coercion, no blanket `except`, no new dependency.
Rejection routes through the **pre-existing** per-record quarantine, so the
malformed record is retained unmodified and healthy siblings still replay.
`_is_optional_number` already carries the signed-64 int bound established in the
first pass, so `2**100` in either field is rejected too.

`attempt_generation` is **not** narrowed for valid values: ordinary ints
(`0, 1, 7, 2**63-1`) still replay, asserted with the record's own
`attempt.generation` kept consistent (the payload↔attempt identity is a
separate pre-existing guard in `canonical_terminal` and was not touched).

## Evidence

| Check | Result |
|---|---|
| Pre-fix probe, 4 arms (control + 3) | control queues both; all 3 bad arms abort the whole profile |
| Post-fix probe, same 4 arms | control unchanged; each bad arm quarantines only the bad record |
| Regression file `test_async_delegation_numeric_binding_boundary.py` | **60 passed, 0 failed** |
| Per-guard mutation: revert ONLY the new `created_at` + `attempt_generation` block | **8 failed / 52 passed** — exactly the 8 new discriminating rows; all controls stay green |
| Restore after mutation | md5 `2f58560786271e53113e7696c207f592`, byte-identical to pristine |
| 11-file delegation suite via `scripts/run_tests.sh` | **373 passed, 0 failed**, exit 0, 22.3s |

New test cases (all with a healthy-sibling witness asserting the good
delegation is still **queued by id**, plus a passing control arm):

- `test_control_healthy_outbox_replays_every_record` — control
- `test_unconvertible_outbox_created_at_spares_sibling` — `-(10**400)`, `10**400`, `2**100`, `2**63`
- `test_unconvertible_attempt_generation_spares_sibling` — `inf`, `-inf`, `nan`, `2**100`
- `test_ordinary_attempt_generation_is_not_narrowed` — `0, 1, 7, 2**63-1`
- `test_ordinary_outbox_created_at_is_not_narrowed` — `0, 1234.5, 1789390471, 2**63-1`

Evidence preservation is asserted on the `created_at` and `attempt_generation`
paths by comparing the full persisted record before and after the replay pass
(`after == before`).

## Scope caveats (run 89)

- Inputs remain **synthetic**; still no production-incidence claim.
- Only the 11 delegation files were run; full repo suite not run (budget).
- The `1191-style` matrix still has no runnable artifact I could locate.
- Changes confined to `tools/async_delegation_store.py` and its regression file
  plus this report. No push/PR/merge/deploy; no live state/config/cron/profile
  change; `~/.hermes/runtime/hermes-agent` untouched.
