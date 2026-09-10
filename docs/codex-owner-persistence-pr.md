# Codex owner-aware refresh — PR preparation

Base: `a034e62a702beae21f7fa8fd09f98ce5c5249da2` (`fork/main`).
Target: **ANG-Ventures/hermes-agent**, fork-only. Branch push and review PR are
authorized; no merge, deployment, restart, real credential inspection, or real
OAuth operation is part of this change.

## PR title

fix(auth): preserve Codex pool ownership across refresh and persistence

## Scope / implementation

* `load_pool("openai-codex")` resolves an explicit owner auth path. Nonempty
  local provider pools remain local. Root fallback is root-owned, not a mutable
  profile copy. A local singleton declares a local grant too; it cannot seed
  into, or overwrite, an inherited root pool. Other providers retain their
  existing loading and persistence paths.
* Stable row IDs are retained; missing legacy IDs are assigned under the owner
  lock. Duplicate identities are refused. Manual rows are never joined to a
  singleton by account ID, access-token equality, or refresh-token equality.
* Refresh holds the owner lock through uncached row reread, durable reservation,
  POST, and atomic auth-store replacement. Waiters adopt the committed row,
  including on forced refresh. Removed/source-replaced rows cannot be restored
  from old in-memory snapshots. Quarantined peer generations cannot escape as
  usable credentials.
* Persistence merges field deltas against the original snapshot, conditional on
  the stored token generation and source still matching. It preserves peer token
  rotations, additions, unknown fields, unrelated providers, priorities, and
  newer cooldowns. It never repopulates a removed row from a known snapshot.
* Singleton runtime resolution uses the same owned transaction. Explicitly
  singleton-owned rows and the singleton are committed together. Suppression
  cannot fall through to the old singleton refresh path. Re-auth writes only
  `device_code` rows, never inferred manual aliases.
* A tokenless receipt is durably created **before POST** in the owner's
  `auth.json.codex-refresh/` directory. Its filename is SHA-256 of the refresh
  generation alone; the directory scopes the owner. Renaming/removing row IDs
  cannot bypass it. Its content is only version/outcome (format version 2). It survives process
  death and failed replacement writes. The uncertain generation is blocked on
  fresh load, selection/lease, and before refresh. A new authenticated generation recovers without
  deleting receipts. A definite 429 releases the reservation; transport errors,
  malformed success, 5xx, and terminal auth errors stay fenced and raise.
  HTTP-status provenance, not a body-controlled error code, permits release.
  Genuine 429 cooldown is persisted before release and honored on repeat calls.
  Public selection isolates expected auth refusals and tries healthy siblings;
  private transactions still raise. Runtime prefers the singleton but falls back
  to healthy manual rows when that singleton is unusable.
* Existing Anthropic post-failure receipt infrastructure is source-specific and
  records after rotation; it cannot supply this pre-POST transaction reservation
  unchanged. This patch does not alter that provider's semantics.

Lock order is pool mutex → exactly one owner auth lock. The Codex transaction
never calls profile write-through while holding root. Root-lock-only callers
reenter the same path lock. Codex refresh now holds its pool mutex across the
transaction; that trades same-pool thread latency for a consistent lock order.
No thread-level concurrency/latency improvement is claimed.

## Required migration refusals

**This code does not migrate the live fleet.** A green synthetic preflight is
not evidence that existing mirrored stores have a single owner.

* Mirrored profile-local manual rows remain distinct declared owners. The
  unchanged diagnostic's mirrored case still shows two original-token POSTs;
  this is deliberately NOT auto-coalesced. Its ten predicates do not include
  mirror safety, despite the historical `migration_safe` aggregate field name.
* The migration must explicitly inventory and retire mirrored provider pools
  and stale singleton seeders, with lineage evidence and separate approval.
  Independent local grants must not be deleted or copied into root.
* Shadowing is **per provider**, not per account. Retaining any intentional
  local Codex pool means that profile does not inherit the remaining root rows.
  Mixed local/shared accounts and per-profile shared-pool priority overrides
  require a separate explicit design. Deleting just one copied account is not
  sufficient. No new mixed-owner schema is claimed here.
* Add/remove through an inherited pool is refused rather than silently mutating
  root or creating local mirrors. Perform administration at the declared owner.
* All writers must run the new implementation before migration. Old resident
  processes and external token consumers do not honor receipts. Quiescence and
  a new grant at the canonical owner remain necessary operational gates.
* The earlier row-ID-based receipt format was unreleased and never deployed.
  This revision does not migrate those prototype receipts. Any experimental
  installation must quiesce old writers and obtain new grants before switching;
  do not treat row-scoped receipts as format-2 backup replay protection.
* Receipts deliberately are not garbage-collected: restoring an old auth backup
  must not make a consumed generation refreshable. Keep receipt directories with
  auth-store backups. Directory-fsync failures fail closed; tests certify macOS
  POSIX filesystem behavior, not Windows/network-filesystem durability.

## Evidence

The original diagnostic is copied byte-for-byte from diagnostic commit
`1c7349520af4c42aa9e3a7e6e48a731e941184c6`:
`scripts/probe_codex_profile_refresh.py`. Its expectations were not edited.

* Original source: nine real two-process scenarios, **2/10 predicates true**;
  exits 1. Saved tokenless evidence: `docs/codex-owner-original-red.json`.
* Implementation: same nine scenarios, **10/10 predicates true**, exit 0.
  Mirrored local owners remain explicitly unsafe for shared-grant migration.
* Committed implementation `44a68e034532feede73fb0ff94a9a71912e03524`:
  sibling credential/auth suite (including real multiprocessing cases)
  **433 passed, 2 skipped in 37.64s**; focused filesystem suite **20 passed**.
  Ruff checks and `git diff --check` pass. All tests used disposable homes,
  positively pinned imports, and a denied real-network boundary.
* New filesystem tests on clean original source (initial 14-test subset): **11 failed, 2 passed,
  1 deselected**. Deselected test targets the newly introduced receipt-writing
  helper. Failures include duplicate original-token POST, root not updated,
  local shadows, removed-row resurrection, singleton/manual misownership and
  missing fail-closed behavior—not collection/import failures.
* Independent review found suppressed-singleton legacy fallthrough and peer
  quarantine-after-load holes. Four new tests reproduced those failures before
  the corrections; both call paths now enforce the checks.

Existing tests that expected same-account/unknown-account manual adoption or
access-token-equality alias rewriting were changed to assert the explicitly
requested independence contract. No diagnostic expectations were weakened.
The old direct-constructor lock test now loads its row from the actual store;
unowned in-memory Codex refresh is refused.

## Review-blocker follow-up evidence

* Eight receipt/runtime regressions were RED before their fixes: changed/missing
  row-ID replay, absent genuine-429 cooldown, 500 with a quota-looking body,
  runtime singleton failure isolation (429/500), and transport-timeout isolation
  for select and lease. Transport errors retain the receipt and become structured
  auth refusals; programming errors still propagate.
* Added public consumer coverage for select, implicit/explicit lease, rotation,
  resident receipt/dead/removed/source-replaced rows, peer generation adoption,
  deferred-refresh race, and programming-error propagation.
* Selection regression suite on pre-fix implementation: **44 failed, 3 passed**;
  controls preserved private error propagation and programming-error visibility.
* Exact sibling suite below: **489 passed, 2 skipped in 48.49s**. This is the
  complete documented credential/auth slice, not the repository-wide test suite.
* Unchanged nine-scenario diagnostic: **10/10 predicates true**, exit 0; mirrored
  profile-local same-grant stores still POST twice and are NOT migration-safe.
* Original reviewer selection probe: two healthy selections, one POST, persisted
  exhausted status. Original multi-case probe (only source path changed) now
  stops at the changed-ID replay with `codex_refresh_uncertain`, as intended.
  Before stopping: healthy selected after 429; 500 quota-body receipt retained,
  one POST; resident reserved generation returns no credential. Missing-ID and
  new-grant recovery are independently covered by the passing regressions.
* Real network denied, synthetic tokens only, disposable HOME/HERMES_HOME, and
  imports positively pinned with editable finders removed in every test runner.

## Reproduction

Use the runtime interpreter **only as a dependency carrier**; never its editable
source finder. From this worktree, with disposable HOME and HERMES_HOME:

```sh
SB=$(mktemp -d)
env HOME="$SB" HERMES_HOME="$SB/.hermes" PYTHONPATH="$PWD" \
  /Users/alexgierczyk/.hermes/runtime/hermes-agent/venv/bin/python -c '
import glob, pathlib, socket, sys
root = pathlib.Path.cwd().resolve()
sys.meta_path[:] = [f for f in sys.meta_path if "__editable__" not in str(f)]
import agent.credential_pool as cp, hermes_cli.auth as auth
assert pathlib.Path(cp.__file__).resolve().is_relative_to(root)
assert pathlib.Path(auth.__file__).resolve().is_relative_to(root)
def deny(*args, **kwargs):
    raise AssertionError("network forbidden")
socket.socket.connect = deny
import pytest
paths = (glob.glob("tests/agent/test_credential_pool*.py")
       + glob.glob("tests/hermes_cli/test_auth*.py")
       + glob.glob("tests/agent/test_codex_owner*.py")
       + ["tests/agent/test_anthropic_credential_persist_failure.py"])
sys.exit(pytest.main(paths + ["-q", "-o", "addopts="]))'
```

Run the unchanged diagnostic separately with the same interpreter. Every child
pins source imports, has a disposable home, and refuses real socket connects.
Only synthetic HTTP responses are used. The multiprocessing pytest suite also
runs each of its nine cases and asserts owner persistence and failure behavior.
