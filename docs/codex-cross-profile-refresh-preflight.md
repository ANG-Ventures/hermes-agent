# Codex cross-profile refresh: migration preflight blocker

Status: **BLOCKED; diagnostic only, no runtime fix or migration implemented.**
Inspected base: `a034e62a702beae21f7fa8fd09f98ce5c5249da2` (`fork/main`).

## Reproduce without credentials or network

From this checkout, using a Python environment with Hermes dependencies:

```sh
python scripts/probe_codex_profile_refresh.py
```

The diagnostic deliberately exits **1** when a safety invariant fails. It is
not a green test suite, and must not be represented as a migration approval.
It runs nine two-process scenarios against the actual `load_pool`,
`CredentialPool._refresh_entry`, `refresh_codex_oauth_pure`, and auth-store
persistence implementations. Only the HTTP client is replaced. Both processes
finish loading before refresh begins. Pipe handshakes establish ordering; the
same-profile lock test holds the first POST until the second lock times out.
Every child has a disposable HOME/HERMES_HOME, asserts its imported source is
under this checkout, and denies unexpected socket connections. Output contains
only events, booleans, exception classes and the source directory; no tokens.

The synthetic HTTP endpoint always supplies a successful replacement pair.
`POST.original=true` measures submission of the original synthetic refresh
token, not a claim about an actual provider response. In particular, two
successful mocked returns do NOT mean two real rotations would succeed.
Crash injection occurs after the mock receives POST, before the replacement
pair reaches the caller; write failure occurs after a successful response.
They model the consumed/possibly-consumed token uncertainty, not live revocation.

## Observed on the base

| Scenario | Observation |
| --- | --- |
| Same profile, first POST held | One POST; other process gets TimeoutError. Lock control works. |
| Different profiles, mirrored manual rows | Both POST the original token concurrently. |
| Different profiles, root-only manual pool | Both POST the original token concurrently; root pool is unchanged; two local shadow pools appear. |
| Different profiles, root singleton + device_code pool | Both POST the original token concurrently; two local pools appear. |
| Same profile, manual pool, serialized completion | Waiter POSTs the original token after winner commits. Serialization alone is insufficient even within one profile. |
| Root-only manual pool, explicit root lock around full operation | Serialized waiter still POSTs the original token; root pool is unchanged. |
| Same account, two distinct profile-local grants, no singleton | Both POST their own original grant. This independence must be preserved. |
| Root-only manual pool, crash after POST | Other process POSTs the original token again. |
| Root-only manual pool, persistence failure after POST | First raises OSError; other process POSTs the original token again. |

Ten safety/control predicates currently yield two true and eight false. The
root-unchanged measurement refers specifically to `credential_pool`, not the
singleton `providers` block: existing singleton write-through does not provide
pool-row ownership.

## Why an owner-lock substitution is not a sufficient fix

* `hermes_cli/auth.py:1695` (`read_credential_pool`) implements **read-only,
  per-provider fallback**, not a durable owner reference. Any local rows shadow
  the entire root provider pool. The API returns rows without provenance.
* `hermes_cli/auth.py:1809` (`write_credential_pool`) always writes the active
  profile. `CredentialPool._persist` uses it for refresh, status and other
  mutations. `load_pool` also writes locally when seeding/normalizing changes.
  Merely removing copies now will not keep them removed.
* `agent/credential_pool.py:1651` locks the active profile for Codex refresh.
  Its re-sync (`:1164`) reads **singleton state**, not the current authoritative
  pool row by stable ID. For a pool-only manual grant, no singleton exists;
  a waiter keeps stale in-memory tokens even after the winner has persisted.
* `_sync_device_code_entry_to_auth_store` (`:1507`) and its root write-through
  do have source-path information, but only for **singleton** entries. The
  method explicitly excludes manual entries. Multi-account pools cannot be
  made safe by routing all rows through one singleton.
* The post-response path (`:2263`) replaces the in-memory row and persists it
  before syncing the singleton. The Codex path has no durable pre-POST
  uncertainty marker to prevent retry of a potentially consumed token after
  process death or a failed commit. A lock is released by a dead process;
  it cannot resolve whether the remote operation committed.
* Account identity is not grant identity. The independent-grant control covers
  separate local pools without a singleton. It does not certify the existing
  singleton adoption heuristic for independent grants of the same account.

Therefore the requested complete safety contract is not a narrowly scoped
lock patch. This artifact intentionally leaves production source unchanged
rather than shipping a correct-looking partial fix. It does NOT rule out a
focused ownership-aware design; it identifies its required surfaces.

## Required implementation/migration boundary

Before shared-grant migration can proceed, an implementation must establish:

1. A resolved pool owner path + stable row identity carried from loading into
   refresh and persistence. Local independent pools stay local; root inheritance
   is explicit provenance, never inferred solely from account ID or token bytes.
2. One canonical owner lock enclosing an uncached authoritative row re-read,
   POST, and durable replacement. A waiter adopts a committed generation and
   does not POST its stale snapshot. Removed/replaced rows fail closed rather
   than resurrecting a saved in-memory credential.
3. Inherited pool mutations do not materialize profile-local credential copies.
   Root writes merge by row/generation, not whole stale in-memory pool snapshots.
   Status writes must not overwrite a newer token pair. Lock ordering must cover
   the singleton resolver, pool writer and root write-through together.
4. Explicit consumed/uncertain outcome handling durable before releasing the
   transaction boundary. If persistence cannot be guaranteed, do not retry or
   report a safe rotation. A narrowly proven single-writer external owner could
   be another design, but is not provided by current fallback semantics.
5. Multi-process regression coverage for normal and forced refresh, row removal,
   independent same-account grants, singleton/pool interaction, crash/write
   failure, and repeated load/persist cycles. This probe supplies RED evidence;
   no RED-to-GREEN implementation is claimed.

**Intended migration postcondition (not achieved):** each shared Codex lineage
has exactly one durable root-owned row and one canonical refresh transaction
owner. Consuming profiles hold no mutable mirror of that lineage and no stale
singleton that can re-seed it; reloading and status changes preserve that state.
Intentional independent profile-local grants remain independent. Because current
inheritance shadows per PROVIDER (not account/row), a profile retaining even one
local Codex row cannot also inherit the rest of the root Codex pool. Mixed
local/shared accounts require an explicit per-row ownership design, not deletion
of only one mirrored account and an assumption that the rest will inherit.

Operationally, any future copy removal must be lineage-evidenced, quiesced and
separately approved; do not delete independent same-account grants. Re-auth once
at the canonical owner only AFTER the runtime contract is implemented, tested
and deployed. Do not copy the fresh grant into profiles. An account-exists
shortcut in an add-subscription command is not proof a new OAuth flow occurred.
No auth-state/config changes, OAuth calls, deployment, restarts, push or merge
were performed for this investigation.
