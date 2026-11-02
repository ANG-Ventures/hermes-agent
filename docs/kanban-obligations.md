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
| `progress <id> <line...>` | operator, or the worker of that card itself | renews the lease of a CLAIMED row. rc 2 "claim first" on an unclaimed row. Not a holder change: `holder_gen` unchanged |
| `resolve <id> <resolution> [--expected-gen G]` | `handed:*` / `dropped:*`: operator only. Observed outcomes: any non-worker | any open state -> `resolved` |
| `receipt <id> --wake-outcome O` | any (drainer, reconciler) | ONLY `wake_attempts`, `first_wake_at`, `last_wake_at`, `last_wake_outcome`. Never `state`, never `holder_gen` |
| `bind <card> [--job-name J] [--code-file F\|-] [--repo O/R --pr N] [--obligation ID] [--worker-owned] [--holder-ref R]` | anyone, read-only | nothing. Prints the binding decision (below); rc 2 when refused |
| `executor <id> --holder-ref R [--rule X] [--expected-gen G]` | non-worker | `open_unclaimed`/`open_claimed` -> `open_executor`. The binding is written once: rc 2 if R is bound to another row or the row to another R |
| `release <id> [--why W] [--expected-gen G]` | non-worker | `open_executor` -> `open_unclaimed` (bound job ended opening no row: `--stop`, signal, `=ok`, queue row retired other than merged) |
| `supersede <id> --successor-key K [--created-at E] [--summary S] [--repo/--pr/--job-name] [--expected-gen G]` | non-worker | ONE transaction: open K (idempotent) AND resolve `<id>` as `superseded:K`. A lost CAS commits nothing (successor included) and never falls back to `open_unclaimed` |
| `expire` | non-worker | every `open_claimed` row past `claim_expires` -> `open_unclaimed`, claimed time banked AT `claim_expires`, `lease_expiries + 1` |
| `list [--card T] [--open]` | anyone | nothing |
| `audit [--card T]` | anyone | nothing; rc 1 when any open row's `escalation_due` is set |

Source keys: `detached:<run_id>`, `queue:<repo>#<pr>@<head>#<gen>`, `blocked:<event id>`,
`review:<episode event id>` (class `review`), `holderless:<...>`. Everything else is `decision`.

Resolutions: `requeued-green:<run_id>` (only when the row's executor `holder_ref` IS that run:
a watcher's green never resolves a lander decision), `superseded:<source_key>`, `merged:<sha>`,
`card-closed`, `handed:ace`, `dropped:<why>`. Empty or bare (`dropped:`) is rc 2.

## Authority

Operator = ALL of: profile in `OPERATOR_PROFILES` (default/apollo/aegis); a session id (bound actor,
then the in-process session, then `HERMES_SESSION_ID`); that session exists in an operator profile's
`state.db` `sessions`; NO worker-run env marker (`HERMES_KANBAN_TASK`, `HERMES_KANBAN_RUN_ID`,
`HERMES_KANBAN_LINEAGE`); and NO dispatched worker in the process ancestry
(`kanban_identity.worker_ancestor_profile`, read from `task_runs.worker_pid` + spawn fingerprint),
so stripping the env does not make a worker an operator.

A claim from a session that is not the card's home is accepted and returned with
`"non_home": true` (the `obligation_claimed` event carries it). A `dropped:*` resolve and any
non-home resolve post a machine comment (author `obligations`, body starting `[obligations]`) on the
card and one #logs line naming the actor profile and session.

## Binding a job queued with `--card` (`bind`)

`{"status": "bound"|"unbound"|"refused", "obligation": id|null, "rule": "explicit"|"same-pr"|"same-name"|"persisted"|null, "reason": "..."}`

Decided ONCE at enqueue; the caller persists it (job.json `obligation: <id>`, or `executor` for a
queue row). First match wins:

0. `--holder-ref` already bound to a row -> that row, `persisted` (a later row never retargets it).
1. Not LANDING CODE -> `unbound` (a receipt). With `--obligation` -> `refused`. Landing code is
   what the job actually runs: `fleet-merge(.sh)`, `apollo-land(.py)`, `land-handback(.py)`,
   `gh pr merge`, a REST `.../pulls/<n>/merge`, `landing_queue.py`, `land_request.py`. The job
   name (`^land`) and `--allow-lander` do NOT qualify; that wide test is detached-run's D-7 gate only.
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
resolution, unclaimed_total_s, claimed_total_s` (`*_total_s` include the current stint).
`audit` rows add `escalation_due` (reason string or null) and `lease_expired`.

## Events

Each state change appends one `task_events` row in the same transaction: `obligation_opened`,
`_claimed`, `_progress`, `_executor`, `_released`, `_lease_expired`, `_escalated`, `_resolved`.
They are NOT owner-wake `SCAN_KINDS` (a test pins that); the reconciler is the only deliverer.

## Clocks (decision class)

Numbers come from `<root>/fleet/obligations-policy.json`; absent or invalid file -> the compiled
`POLICY_DEFAULTS` (decision: rewake 10, escalate 30, lease 30, max_claim 120 min). Escalation is
due when banked + current UNCLAIMED time >= `escalate_min`, on the 2nd lease expiry, or when
claimed time >= `max_claim_min`. An executor holder suspends both. Review rows use the deadman's
clock, never this one.
