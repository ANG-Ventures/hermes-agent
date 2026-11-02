# Kanban work obligations: CLI contract

Spec: "Durable Work Ownership" v1.7 (§4.1 record, §4.3 state machine, §4.11 clocks).
Code: `hermes_cli/kanban_work_obligations.py`. Tests: `tests/hermes_cli/test_kanban_work_obligations.py`.

An obligation is one pending DECISION on a card. It lives in the `obligations` table of the
card's own board DB (additive `SCHEMA_SQL`; every board gets it on its next open).
Key: `(task_id, source_key)`, unique for ever, resolved rows included.

Every verb takes the board the usual way:
`hermes kanban --board <slug> obligation <verb> ...`. Output is ONE JSON object on stdout.
Refusals print `obligation: <why>` on stderr.

## Exit codes

| rc | Meaning |
|---|---|
| 0 | done (also: an idempotent no-op `open`, `opened: false`) |
| 1 | error: unknown obligation id / card not on this board; `audit` with an overdue row |
| 2 | refused: authority, bad input (empty resolution, bad source key), `progress` before `claim`, a binding refusal |
| 3 | conflict: lost the `holder_gen` compare-and-set, or the row is not in a state the verb starts from. Re-read and retry next tick |

## Verbs

| Verb | Who | Writes |
|---|---|---|
| `open <card> --source-key K [--created-at EPOCH] [--summary S] [--repo O/R --pr N] [--job-name J]` | anyone (drainer, reconciler) | inserts `open_unclaimed`, or nothing if K exists. `--created-at` is the EVENT time and starts the clock. With `OBLIGATIONS=0` or registry `enabled: false`: `{"disabled": true, "opened": false}`, rc 0 |
| `claim <id> [--expected-gen G]` | operator only (below) | `open_unclaimed` -> `open_claimed`, lease = `classes.<c>.lease_min` |
| `progress <id> <line...>` | operator, or the worker of that card itself | renews the lease of a CLAIMED row. rc 2 "claim first" on an unclaimed row. rc 3 "lease expired" when past `claim_expires`: the expiry is banked (row back to `open_unclaimed`, `lease_expiries + 1`) and the lease is NOT renewed; claim again. Not a holder change: `holder_gen` unchanged |
| `resolve <id> <resolution> [--expected-gen G]` | `handed:*` / `dropped:*`: operator only. Observed outcomes: any non-worker | any open state -> `resolved` |
| `receipt <id> --wake-outcome O` | any (drainer, reconciler) | ONLY `wake_attempts`, `first_wake_at`, `last_wake_at`, `last_wake_outcome`. Never `state`, never `holder_gen` |
| `bind <card> --lands yes\|no [--job-name J] [--repo O/R --pr N] [--obligation ID] [--worker-owned] [--holder-ref R]` | anyone, read-only | nothing. Prints the binding decision (below); rc 2 when refused |
| `executor <id> --holder-ref R [--rule X] [--expected-gen G]` | non-worker | `open_unclaimed`/`open_claimed` -> `open_executor`. The binding is written ONCE to `bound_ref` (unique across the board), which release, claim, expiry and board transfer never clear: rc 2 if R is bound to another row or the row to another R |
| `release <id> [--why W] [--expected-gen G]` | non-worker | `open_executor` -> `open_unclaimed` (bound job ended opening no row: `--stop`, signal, `=ok`, queue row retired other than merged) |
| `supersede <id> --successor-key K [--created-at E] [--summary S] [--repo/--pr/--job-name] [--expected-gen G]` | non-worker | ONE transaction: open K (idempotent) AND resolve `<id>` as `superseded:K`. A lost CAS commits nothing (successor included) and never falls back to `open_unclaimed` |
| `expire` | non-worker | every `open_claimed` row past `claim_expires` -> `open_unclaimed`, claimed time banked AT `claim_expires`, `lease_expiries + 1` |
| `list [--card T] [--open]` | anyone | nothing |
| `audit [--card T]` | anyone | nothing; rc 1 when any open row's `bound_violation` is set (computed from the clocks vs policy, independent of `escalated_at`: an acknowledged escalation is still overdue) |

Source keys: `detached:<run_id>`, `queue:<repo>#<pr>@<head>#<gen>`, `blocked:<event id>`,
`review:<episode event id>` (class `review`), `holderless:<...>`. Everything else is `decision`.

Resolutions: `requeued-green:<run_id>` (only when the row's executor `holder_ref` IS that run:
a watcher's green never resolves a lander decision), `superseded:<source_key>` (only when that
successor row already exists on the same card and is still open; otherwise rc 2, use `supersede`.
`supersede` likewise refuses a successor key whose row is already resolved, so no cycle can form), `merged:<sha>`,
`card-closed`, `handed:ace`, `dropped:<why>`. Empty or bare (`dropped:`) is rc 2.

## Authority

Operator = ALL of: profile in `OPERATOR_PROFILES` (default/apollo/aegis); a session id (bound actor,
then the in-process session, then `HERMES_SESSION_ID`); that session exists in an operator profile's
`state.db` `sessions`; NO worker-run env marker (`HERMES_KANBAN_TASK`, `HERMES_KANBAN_RUN_ID`,
`HERMES_KANBAN_LINEAGE`); and NO dispatched worker in the process ancestry
(`kanban_work_obligations.worker_ancestry`, read from `task_runs.worker_pid` + spawn fingerprint),
so stripping the env does not make a worker an operator. The ancestry check fails CLOSED: a live
run on the caller's ancestry with an unverifiable fingerprint (`unverified`), an unreadable
ancestry or board, a live run whose CURRENT process fingerprint is unavailable, or an exception
refuses claim/resolve with "worker ancestry indeterminate".
The profile compare is byte-exact (`Default` is not `default`).

A claim from a session that is not the card's home is accepted and returned with
`"non_home": true` (the `obligation_claimed` event carries it). A `dropped:*` resolve and any
non-home resolve post a machine comment (author `obligations`, body starting `[obligations]`) on the
card and one #logs line naming the actor profile and session.

## Binding a job queued with `--card` (`bind`)

`{"status": "bound"|"unbound"|"refused", "obligation": id|null, "rule": "explicit"|"same-pr"|"same-name"|"persisted"|null, "reason": "..."}`

Decided ONCE at enqueue; the caller persists it (job.json `obligation: <id>`, or `executor` for a
queue row). First match wins:

0. `--holder-ref` already bound to a row -> that row, `persisted` (a later row never retargets it).
1. `--lands no` -> `unbound` (a receipt). With `--obligation` -> `refused`. **The producer declares
   `lands`; the fork never parses job text.** detached-run decides it from its own lander gate;
   apollo-land and the landing queue already know. The job name (`^land`) and `--allow-lander`
   are not a declaration; that wide test is detached-run's D-7 gate only.
2. `--worker-owned` -> `unbound`.
3. `--obligation ID` -> `bound`/`explicit` if ID is open on the card and not a different known PR.
4. Exactly one open `detached:*` row with the same KNOWN `repo#pr` -> `same-pr`.
5. Exactly one open row with the same `job_name` (and no conflicting known PR) -> `same-name`.
6. Otherwise `refused`: "name the decision: --obligation <id>" listing the open rows.

A NULL repo or pr never equals anything, NULL included.

## JSON row shape (`obligation`, `obligations[]`)

`id, task_id, source_key, repo, pr, job_name, class, holder_kind, holder_ref, state, claim_session,
claim_expires, lease_expiries, unclaimed_s, claimed_s, state_since, wake_attempts, first_wake_at,
last_wake_at, last_wake_outcome, escalated_at, summary, created_at, updated_at, holder_gen,
resolution, bound_ref, unclaimed_total_s, claimed_total_s` (`*_total_s` include the current stint).
`audit` rows add `escalation_due` (reason string or null; null once escalated), `bound_violation`
(reason string or null, ignores `escalated_at`) and `lease_expired`.

Board export/import (`kanban_transfer`) returns every `open_claimed` / `open_executor` row to
`open_unclaimed` (holder cleared, clock restarted at import, the stint banked by `split_stint`);
`bound_ref` and resolved rows travel.

## Events

Each state change appends one `task_events` row in the same transaction: `obligation_opened`,
`_claimed`, `_progress`, `_executor`, `_released`, `_lease_expired`, `_escalated`, `_resolved`.
They are NOT owner-wake `SCAN_KINDS` (a test pins that); the reconciler is the only deliverer.

## Clocks (decision class)

Numbers come from `<root>/fleet/obligations-policy.json`; absent or invalid file -> the compiled
`POLICY_DEFAULTS` (decision: rewake 10, escalate 30, lease 30, max_claim 120 min). Escalation is
due when banked + current UNCLAIMED time >= `escalate_min`, on the 2nd lease expiry, or when
claimed time >= `max_claim_min`. An executor holder suspends both.

One helper attributes time: `split_stint(ob, now) -> (claimed_add, unclaimed_add, expired)`. A
claimed stint is claimed only up to `claim_expires`; after it the time is UNCLAIMED and the expiry
counts, whether or not `expire` has swept. Every banking transition (claim, executor, resolve,
supersede, expire, board transfer) and every reader (`*_total_s`, `audit`, `escalation_due`) uses it,
so sweep timing never changes the clocks. A banking transition that crosses an unswept expiry also
appends `obligation_lease_expired`. Review rows use the deadman's
clock, never this one.
