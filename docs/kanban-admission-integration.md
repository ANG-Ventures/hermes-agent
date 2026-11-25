# Kanban admission integration (staged; not deployed)

PR #81 preserves local ledger accounting in spawn-only and upgrades that SAME
lease through the shared door in fcfs/classes. It does not book a second lease.
Local ready/forced starts and local review starts book before launch. Door-owned
runs are excluded from legacy renewal/release to avoid double ownership.

`door_authorized` retains host, unit_id, reservation_id, until and `at`.
`at_source=authorization_receipt_received` labels `at` honestly: the current
allocd reply has no server grant timestamp. This is a conservative local
receipt clock, not an invented server clock. Exact server grant time still
requires an allocd producer extension or joining its authoritative lease row.

`spawned` joins authorization by exact task/run and retains canonical host,
unit/lease, consumer, process_id, acknowledgement `at`, boundary and a SHA256
source identity. The existing PID fingerprint/start token remains unchanged.
No PID means no spawned event; late/lost-claim spawns remain explicitly marked
and join the authorization of the supplied launching run, never a successor.
The receipt deadline is checked after its durable write, immediately before
entering launch; expiry cancels/releases that attempt and requires a fresh ID.
A recorded authorization prevents re-entry before local capacity booking, including
review bookings. Review callback arguments are selected by signature inspection
before execution: an execution-time TypeError/ValueError is a failure, never
permission to invoke an externally effective spawn twice.
`source_ref` identifies dispatcher source bytes, not an assertion that a Git
commit is deployed. Capture the runtime release revision independently.

Boundaries: spawn_now -> forced; pin route -> pinned; local route -> local;
other dispatcher route -> dispatch. Review starts carry explicit local/pinned
placement data. Failed starts do not become successful evidence.

## Receipt migration / release gate

No candidate bytes are installed or checked into the vendored package here.
The isolated candidate-package experiment copies the full five-module closure
from exact canonical `e65e27833ef171a37582507ed72bd9ac8bcd3fd1`, using the real
package adapter. It proves unprovisioned refusal, offline provision, protected
marker write, reservation/release, pending receipt and wrong-env rejection, and
replaced-root refusal in a fresh process.
Run with `ADMISSION_SOURCE_REPO=/isolated/hermes-home` and optionally
`ADMISSION_CANDIDATE_REF=<final-ref>` under test-gate. Missing source skips this
candidate-only experiment in ordinary agent CI; that is NOT release acceptance.
Final merged byte-copy refresh, manifest pins and non-skipping installed-package
integration remain mandatory before deployment/arming.

Native gateway/allocd share the installation root, not a profile root:

- ALLOCATION_HOME=/Users/alexgierczyk/.hermes (the pre-rename PLACEMENT_HOME is no longer read)
- PLACEMENT_ROOT_RECEIPT=/Users/alexgierczyk/Library/Application Support/FleetAdmission/hermes-root.receipt

These are proposed service inputs, NOT installed configuration. Offline proof
must precede writers/token minting, using the durable-anchor runbook. Receipt
parent is outside .hermes and must be durably prepared with runtime read/fsync
permissions. Preserve explicit ALLOCATION_HOME overrides and matching receipts.
Prepare all actual gateway instance env through the normal release lane; never
patch the loaded runtime. Gateway restart requires separate per-instance human
approval. Verify final source hashes, effective env and new process/module
identity plus real authorization/start evidence. No live proof is claimed here.

Local lease upgrades authenticate BOTH kanban capacity owner and kanban@host
pair capabilities. Provision these using existing authority procedures after
root proof; never copy credentials into evidence. KANBAN_LOCAL_LEDGER=0 retains
the accounting rollback; shared-door mode/host holds remain separately owned.
