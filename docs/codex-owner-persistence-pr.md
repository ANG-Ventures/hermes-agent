# Codex owner-aware refresh — PR preparation

Base: `a034e62a702beae21f7fa8fd09f98ce5c5249da2` (`fork/main`).
Target: **ANG-Ventures/hermes-agent**, fork-only. No push, PR, merge, deployment,
restart, real credential inspection, or OAuth operation is part of this change.

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
  `auth.json.codex-refresh/` directory. Its filename fingerprints row ID plus
  refresh generation; its content is only version/outcome. It survives process
  death and failed replacement writes. The uncertain generation is blocked on
  fresh load and before refresh. A new authenticated generation recovers without
  deleting receipts. A definite 429 releases the reservation; transport errors,
  malformed success, 5xx, and terminal auth errors stay fenced and raise.
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
* Sibling credential/auth suite (including real multiprocessing cases):
  **431 passed, 2 skipped** before the final two unowned-entry guards' tests;
  the final focused filesystem suite is **20 passed**. Full committed-tree
  rerun is recorded in the handoff.
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
