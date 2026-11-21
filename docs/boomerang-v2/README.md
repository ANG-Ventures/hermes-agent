# Boomerang v2 — operator guide

Read this when you dispatch a boomerang, read a `boomerang.fork_ineligible` log line, see an
`[boomerang-wake]` or `⚠️ boomerang-wake` line in a chat, or decide whether to put a kanban card
at the `boomerang wake` level. You do not need the spec to act on anything here.

Source of truth: `~/.hermes/plans/2026-10-18_boomerang-fork-autowake-spec.md` (v1.0, APPROVED).
Phase 0 measurements: `docs/boomerang-v2/phase0-probes.md`. Where this file and the spec disagree,
the spec wins; fix this file.

## The two modes, in one paragraph

A boomerang child inherits the parent conversation in one of two modes (D-14):

- **`fork`** (alias **`branch`**, D-16): the child's first request is byte-identical to the
  parent's (same system prompt, same message array, same tools, same model, same cache TTL) with
  one framing user turn appended, and it is pinned to the seat that served the parent's last turn.
  It reads the parent's warm prompt cache instead of writing a new one.
- **`summary`** (the v1 fold): the parent's recent turns are compressed into one labelled
  "INHERITED CONTEXT" message (≤ `inherit_max_tokens`, ~50k). Shares no cache with the parent.

`fork` is requested; eligibility decides. When any eligibility check fails, the child runs
`summary` instead and the gateway logs `boomerang.fork_ineligible reason=<reason>` (INV-2).
A forked child never ships a near-fork that silently writes cold.

### Routing is independent of context mode

The [parent-routing contract](parent-routing-inheritance-spec.md) applies to fork,
branch, explicit summary and automatic summary downgrade. Without an explicit
call-level child model/provider/credential override, a boomerang takes its parent's
active route and effective fallback chain, not `delegation.model/provider` defaults.
An explicit empty parent chain remains disabled. Ordinary delegations retain their
existing configuration precedence. A deliberately different child route uses summary
context rather than claiming a byte-identical fork.

A busy parent's route is snapshotted separately from its database transcript; it is
not reset to its configured primary. With no live parent, the gateway restores the
resolved owner's session `/model` preference, then channel overrides, then defaults,
using the normal gateway fallback policy. Unreadable persisted preferences or missing
credentials fail closed. No historical transient fallback is guessed after restart.

Existing durable delegation records carry per-task non-secret routing provenance,
identity, selected safe endpoint, protocol and fallback policy. An explicit selected
endpoint wins over a provider's current default during recovery. Userinfo/credential
query URLs are refused, never persisted. Malformed persisted chains (including null,
strings and non-route rungs) fail before credential lookup; `[]` remains disabled.
Execution resolves credentials again within the owning
profile. Credential-bearing request headers or non-symbolic fallback secrets cannot
be serialized: durable launch refuses that policy rather than silently widening or
erasing it. Legacy records without a resolved fallback policy report routing-unavailable.
`boomerang.route` logs requested/effective mode, owner/child sessions, route source,
provider/model and fallback count; it excludes endpoints, headers and credentials.

The synthetic-provider HTTP acceptance suites are
`tests/integration/test_boomerang_parent_routing_http.py`,
`tests/integration/test_boomerang_auto_routing.py` and
`tests/integration/test_boomerang_detached_profiles.py`. They drive real
notifier/owner-wake dispatch, scratch claim/board executor receipts, busy/cold
session reconstruction, child retry/fallback and two separate profile subprocesses.
The auto-wake primary/same-vendor lanes speak Anthropic Messages and the permitted
cross-vendor lane speaks CPA-shaped OpenAI chat/SSE; each uses synthetic credentials.
Plugin/MCP startup and model probes are suppressed; retry timing alone is shortened.
External platform delivery is an in-memory adapter. Socket tripwires reject non-loopback
connections. This proves routing integration, not real vendor availability or deployed activation.
See [build receipts and remaining limits](build-review-status.md) before landing.

## 1. Which mode each entry point uses (spec §5.6)

| Entry point | `inherit_context` | Mode you get |
|---|---|---|
| raw `delegate_task(...)`, `inherit_context=true`, no `inherit_mode` | `true` | `summary`. Byte-identical to v1 (INV-1). |
| raw `delegate_task(...)`, `inherit_mode="fork"` or `inherit_mode="branch"` | `true` | `fork` requested, `summary` on any failed check. |
| raw `delegate_task(...)`, `inherit_mode="summary"` | `true` | `summary`. |
| skill `boomerang` or `/boomerang <task>` | `true` | `fork` requested (D-1). |
| `/boomerang --summary <task>` | `true` | `summary`. |
| `/boomerang --fork <task>` ≡ `/boomerang --branch <task>` | `true` | `fork` requested (explicit; same as the default). |
| kanban boomerang-wake dispatch (§4 below) | `true` | `fork` requested; the disposition row's `mode` may say `summary` (D-6); falls back to `summary` on any failed check. |

Rules that follow from the table:

- `inherit_context` stays a boolean. `inherit_mode` is a new sibling param (D-2); `None` means
  `summary`. A caller that never passes `inherit_mode` behaves exactly as before.
- `inherit_mode="fork"` and `inherit_mode="branch"` resolve to the same mode. Never argue the
  difference (D-16).
- A forked child cannot fork again: `inherit_mode=fork` from inside a fork downgrades with
  `reason=fork_depth` (D-17).
- A forked child carries the parent's full tool **schemas** and SOUL (D-11, needed for byte
  identity) but at dispatch it refuses gateway-restart-class `terminal` commands, `clarify`, and
  `inherit_mode=fork`; the child sees a tool error (D-18).
- Auto-handle on a parent with no live agent (evicted, or after a gateway restart) runs `summary`
  from the session DB transcript (§5.5).

## 2. `boomerang.fork_ineligible reason=<reason>` — what each reason means and what to do

Every reason is a downgrade, not a failure: the child still ran, in `summary` mode. Act only if
you expected a fork and the cost matters.

| `reason=` | The check that failed | What to do |
|---|---|---|
| `same_lane` | Parent's provider lane is not a pool-affinity lane, or the child's lane differs from it. A cross-lane child is a delegation, not a fork; `claude-bpr` cannot fork at all. | Nothing. Expected off the pool lanes. If you wanted a fork, run the parent on a pool lane (e.g. `claude-alr`). |
| `same_model` | After construction the child's wire kwargs (model, tier, provider, request overrides) still differ from the parent's. A fork FORCES the parent's model and ignores `delegation.model/provider`, so this means something mutated the request. | File a bug with the log line: it is defence-in-depth tripping, not a config choice. |
| `same_request_params` | Something in the prefix differs: tool list or schemas, thinking/effort config, system prompt bytes, or cache-control TTL. | If you changed tools/effort/skills mid-session, expect it. Otherwise file a bug: a near-fork was caught. |
| `turn_complete` | The parent's last turn had in-flight tool calls at dispatch (D-8). | Retry after the parent's turn ends, or accept the fold. |
| `seat_known` | The parent has no `last_served_by` (no seat recorded for its last turn, e.g. a fresh session or a non-pool response). | Nothing; the next fork after one served parent turn can pin. |
| `warm` | `now − parent.last_served_at ≥ ttl_s − 300 s`. The parent's cache has expired or is about to. | Nothing. A cold fork would write the whole prefix; the fold is cheaper. |
| `fork_enabled` | `delegation.boomerang.fork_enabled` is `false` (the P2 kill switch, §4 below). | Intended if someone flipped it. Flip it back to re-enable forks. |
| `fork_depth` | The caller is itself a forked child (D-17). | Nothing; recursion is closed on purpose. |
| `pin_refused` | The relay refused the pinned seat (draining or cooling). The relay serves a pinned request or refuses it; it never reroutes (INV-3). | Nothing. The child ran the fold on the parent's lane, never a cold fork on another seat. |

**How `warm` decides the TTL (P0.4 correction).** `ttl_s` comes from the parent's **last
response usage split**, not from the harness's `_cache_ttl`:

- `cache_creation.ephemeral_1h_input_tokens > 0` → `ttl_s = 3600`
- else `ephemeral_5m_input_tokens > 0` → `ttl_s = 300`
- else fall back to `_cache_ttl`.

Reason: on `claude-alr`, apx bills a 1h TTL on every request unless the seat is in overage, so
`_cache_ttl` alone is wrong there (Phase 0, P0.4).

**5m-TTL parents never fork, by design.** For `ttl_s = 300`, `ttl_s − 300 = 0`, so `warm` always
fails. Forks are for human-paced (1h) sessions; machine-paced 5m sessions use the fold. Do not
"fix" the 300 s margin.

## 3. Wake dispositions (spec §5.4)

`wake_disposition(kind, payload, task)` returns exactly one of three values. It is consulted by
both wake paths (the kanban notifier and the owner-wake), after the self-echo suppressor, and
only for cards at the `boomerang wake` level (§4).

**Precedence: evaluate tiers in this order and stop at the first that matches.** It is not
first-match over the row list.

1. **Card tags** `irreversible`, `spend`, or `outbound` → `surface`. Beats every kind row.
2. **Judgment kinds**: `blocked` with `needs_input`, any other `blocked`, `block_loop_detected`
   → `surface`.
3. **Merge-pass-owned**: `review_requested` on an own repo with the PR green or pending → `none`.
4. **Specific kind rows** → `auto`: `review_requested` on an own repo with a red PR;
   `completed`; `changes_requested`; the first `crashed` / `timed_out` / `stalled` / `gave_up`
   for that card.
5. **Default** → `surface`. This covers `review_requested` on an upstream-controlled repo
   (`merge_policy != own`), a second crash-class event for the same card (D-13), `reclaimed`,
   `skipped_nonspawnable`, `status`, heartbeat/plumbing kinds, and any kind not listed.

Worked cases (each is a required table test): `completed` + `outbound` tag → `surface`;
`changes_requested` + `irreversible` tag → `surface`; own-repo green → `none`; own-repo red →
`auto`; second crash → `surface`.

What each return value does downstream:

| Value | Effect |
|---|---|
| `auto` | The passive line posts as today, tagged `[boomerang-waking…]`. The gateway takes a single-owner claim on `(board, task_id, event_id)` (if the other wake path already holds it, stop). It snapshots the home session, dispatches a boomerang child (fork requested) whose goal is a fixed instruction with the wake text quoted as DATA, writes a ledger entry, and advances the cursor only after dispatch is accepted. The parent's live turn is never interrupted. |
| `surface` | Existing path, unchanged: passive line + wake turn in the live session. Ace sees it exactly as today (INV-5). |
| `none` | Existing path, unchanged. No child, no claim. The merge pass owns the event. |

The dispatcher dispatches a child only on an explicit `auto`. Any other string, or an exception
in `wake_disposition`, is treated as `surface` (fail-safe).

Each `auto` row may carry `mode: fork|summary` (default `fork`, subject to §2 eligibility).
`completed` verification may default to `summary` if fork turns out to lose on cost for that kind
(D-6, AC-4b).

Retries (D-13): a crash-class event gets one automatic retry. The count is persisted per card in
`kanban_boomerang_wake.json` (`retries[task_id]`), survives restarts, and resets when the card
reaches `completed`/`archived` or when Ace surfaces-and-acts on it. The second occurrence
surfaces. The crash-class budget is per card lifetime; the restart budget below is per wake.

Three budgets live in that file, one per kind of automatic second try (t_5701751b):

| Budget | Spent by | Cleared by |
|---|---|---|
| `retries[task_id]` | a crash-class retry (D-13) | the card reaching `completed`/`archived`, or Ace surfacing-and-acting. Per card lifetime. |
| `blocked_spent[task_id]` | a blocked assessment (`blocked_assess` claim) | any claim on the card going terminal with a handled outcome (list below), except the blocked assessment's own `retry` verdict. Also the `retries` resets. |
| `restart_spent[task_id]` | a gateway-restart redispatch | any claim on the card going terminal with a handled outcome. Also the `retries` resets. |

A card gets one automatic blocked assessment without a handled wake in between. Any handled wake
clears the budget, the assessment's own included: an assessment that surfaces to the owner (or ends
stale/superseded) is itself a handled wake, so the next block is assessed again. The one case that
stays spent has no owner in the loop: the assessment says `retry` and the worker blocks again, which
ends at the owner (`blocked assessment budget exhausted; owner assessment required`) rather than
looping on worker runs. An assessment that fails or is lost also stays spent. A `#restartN`
redispatch of a blocked assessment spends none of the three. A state file from before this change
(no `blocked_spent`) is migrated on first read: at most one count per card with a spending
`blocked_assess` claim on file leaves `retries` (the old code spent it only at 0). The card starts
cleared when a claim at or after its newest assessment, that assessment included, ended with a
clearing outcome (same rule as above), else at `blocked_spent=1`. Crash spends stay, and a blocked
spend whose claim was already pruned (7 days) cannot be told apart from one and stays in `retries`.
A card that also has a `crash_first_occurrence` claim on file is not moved at all: its count cannot
be attributed, so it stays a crash spend and the card gets no `blocked_spent` entry.
The next write persists it.

## 4. The boomerang wake level

A card has one subscription level, and it is the only setting. There is no config knob, no
session `/autohandle` toggle and no board default (Ace 2026-10-22, t_7be18632).

| Level a person sees | Delivery mode on the waker row | What a wake does |
|---|---|---|
| `notify` | `notify` | Passive line only. |
| `wake` | `notify+wake` / `wake` | Passive line + a turn in the subscribed session. |
| `boomerang wake` | `notify+wake` / `wake` (+ `tasks.boomerang_wake = 1`) | Mechanical events go to a background boomerang child (§3 `auto`); everything else wakes as `wake`. |

`kanban.auto_subscribe_boomerang` defaults to `true`. The LEVEL and the SUBSCRIPTION are
decided separately:

- Level (`tasks.boomerang_wake`): every new card gets the knob's value at birth, whoever mints
  it: CLI, tool, cron/script, worker child, swarm, dashboard API, triage decomposition (children
  take the root's stored value). `kanban_db.create_task` applies it
  (`hermes_cli/kanban_boomerang_default.py`, t_c24b1cc7). An explicit
  `--no-boomerang-wake` / tool `boomerang_wake: false` is stored as given, and a retried create
  (same idempotency key) returns that card unchanged. The `created` event records
  `boomerang_wake_source` (`explicit` | `default` | `inherited`).
- Subscription (who is woken): unchanged. A gateway create subscribes its chat; a CLI create
  subscribes only when `kanban.cli_auto_subscribe` (default `false`) would, or when it names an
  explicit home (`--session <sid>`; tool `session_id`) with a routable origin chat (Ace
  2026-11-06). Worker creates, operator pseudo-homes and CLI/unknown sessions get no row.

The level never invents a recipient. A card at boomerang level with no waker row wakes nobody;
`kanban create --json` and the `kanban_create` tool return `wake_route` (`level`, `waker`, and a
`note` when nothing holds the wake), and a plain CLI create prints that note on stderr. Set the
knob to `false` to restore plain notify+wake for future cards; existing cards are unchanged.
`--boomerang-wake` still sets it explicitly. The knob is read on create;
once this code is loaded, flipping it needs no restart. Loading this code into a running
gateway does require a restart.

`tasks.boomerang_wake` holds the level (`kanban show` prints `level: boomerang wake`). Set it with
`hermes kanban create --boomerang-wake`, `kanban edit <id> --boomerang-wake` /
`--no-boomerang-wake`, `kanban notify-subscribe <id> --boomerang-wake` (= `--delivery-mode
notify+boomerang`), or the `kanban_create` tool's `boomerang_wake: true`. The level lives only
on the card: subscription rows stay `notify+wake` / `wake` (the request values `notify+boomerang` /
`boomerang` are stored as those), so a gateway that predates the level still wakes on them. A
boomerang request sets the card level only once the requesting chat holds the wake.

Until the gateway restarts onto this code, cards at level `boomerang wake` are handled by the OLD
path: every wake surfaces to the home session as a plain `wake`.

**Emergency stop** (read every notifier tick, no restart; INV-7):

| Switch | Default | Effect |
|---|---|---|
| file `<HERMES_HOME>/state/boomerang-wake.disabled` | absent (on) | Present: no card is boomerang-woken; every wake surfaces as `wake`. One #logs line on each on/off transition. `touch` to stop, `rm` to resume. |
| `delegation.boomerang.fork_enabled` | on (unset = forks allowed) | Disables fork mode (P2). Every boomerang, including boomerang-wake children, runs `summary`; logs `reason=fork_enabled`. |
| card body line `wake-model: <slot>` | absent (the parent's model) | Overrides the wake child's model for that card (t_54bbaca0). A model-routing slot name only (a worker can edit a card body). Wins over the two keys below. |
| `delegation.boomerang.wake_child.sessions.<home session id>` | unset | Same override for every wake homed to that (resolved) session; a slot, a bare id or `provider/model`. |
| `delegation.boomerang.wake_child.model` / `.provider` | unset (the parent's model and lane) | Global override. A slot or bare id keeps the parent's lane unless `provider` is set. An overridden child runs `summary` (no fork: a different model cannot read the parent's cache) and its surfaced line names the model. Any non-success outcome (failed, timeout, error, interrupted, refused, empty final, an exception) reruns the wake on the parent's model and chain, except a gateway restart or a claim cancelled for its budget (no second child; a restart leaves the claim open for the next boot), and the surfaced line says `wake-model <x> failed (<why>), ran on parent <provider/model>`. An override that cannot be used (unknown, not on the lane, unreadable, or a same-lane pin, decided on the resolved provider, while the gateway's `provider_routing` sets a filter or cannot be read; also a `wake_child` section that is not a mapping) runs on the parent and the line says `wake-model <x> not usable (<why>)`. |
| card body line `wake-effort: <level>`, `wake_child.effort_sessions.<home session id>`, `wake_child.effort` | unset (the home's effort) | Reasoning effort of the wake child, same precedence. `none` or a `VALID_REASONING_EFFORTS` level; anything else is `not usable` and runs at the parent's effort. Alone it keeps the parent's model and lane; a level different from the parent's runs as a summary fold (a changed effort changes the request fingerprint, so it cannot fork) and the surfaced line says `summary`; the parent's own level keeps the fork. With `wake-model` it rides the summary pin. The surfaced line names the effort. |

Scope: boomerang wake is armed for the `default` (Apollo) profile only (spec §2).

## 5. Lines you will see in the origin chat

**`boomerang handled <card> (<row>): <summary>`**
The handled receipt when a child resolves or surfaces a wake, plus a card comment
`boomerang: <row> <summary>`. This is not a deadman page or a system wake. Since hermes-agent#439
(t_8ab5557d) it is an agent receipt, **not a chat line**: a handled-only verdict writes it as a
row in the fleet agent log (`state/agent-logs/<UTC day>.jsonl`, `producer: boomerang-wake`,
`task_id: <card>`), and posts it to the origin chat only when no agent log is available. A
`surface` verdict delivers the child's finding to the home session as a turn (`[boomerang-wake]
child surfaced ...`) and posts no handled line (the turn is the message; the receipt row is
best-effort); a refused turn still posts the line. The file `<HERMES_HOME>/state/boomerang-handled-line-chat.enabled`
restores the old behaviour (the line posts to the origin chat). If the operator already commented
since review began, only the card breadcrumb is written.

Delivery is durable: the gateway writes a pending handback (`handbacks[<delegation>]` in
`gateway/kanban_boomerang_wake.json`) before any send. A line counts as delivered only on the
adapter's `SendResult.success`, and a turn only when it is enqueued. Each delivered part writes a
`boomerang_handback_delivered` event (`part: line|turn`). Every notifier tick retries the parts
that are still pending, and a replayed verdict retries rather than doing nothing. A failed
surface turn leaves the claim at `resurface_delivered: false` until a retry lands it.

**`[boomerang-waking…]`**
The passive event line for a wake that went to a child. A matching `[boomerang-wake]` or `⚠️` line
follows. What to do: nothing yet.

**`⚠️ boomerang-wake failed <card>: <reason>`**
Child dispatch raised, or the child crashed or timed out. The original wake is re-delivered to
the home session as a normal surfaced wake (INV-6). What to do: handle the surfaced wake as you
would any wake. If the same card keeps producing this, `kanban edit <card> --no-boomerang-wake`.

**`⚠️ boomerang-wake lost <card> (reason: gateway restart, retry spent|card moved|budget expired|timeout)`**
The reconciler (§5.7) found a claimed entry whose child died (gateway restart) or ran past its
`max_minutes` (default 45); in the timeout case it cancelled the child first (bounded 60 s wait).
The original wake is re-surfaced through the normal path, once. What to do: handle the surfaced
wake. Re-surfacing happens at most once per entry; after that it is an ordinary wake.

**Restart behaviour (t_870c1fba).** A gateway restart kills every in-flight child. On the new
boot the reconciler spends the wake's restart retry on it instead of surfacing: when the card's
status still equals the status recorded at claim time, `restart_spent[task_id] < MAX_AUTO_RETRIES` and
the claim is within `max_minutes` of `claimed_at`, it bumps the retry, marks the old claim
`terminal` with `outcome: redispatched_after_restart` (`redispatched_as` names the new claim key,
`<old key>#restart<n>`), and dispatches a fresh child for the same card and wake (new delegation
id and nonce, the same row and fork/summary decision). It posts nothing to chat; the gateway log
gets one INFO line `kanban boomerang-wake: <card> redispatched after gateway restart (retry 1/1)
delegation=<new>`. Otherwise the entry is lost as above, with the reason saying which condition
failed. The restart spent the wake's restart budget, so the redispatched child's own failure or a
second restart surfaces (no second automatic redispatch).

**The restart budget is per wake and separate (t_a256e37c).** A restart redispatch spends
`restart_spent[task_id]`, not `retries[task_id]`, and the reconciler decides on `restart_spent` alone
(`>= MAX_AUTO_RETRIES` means `retry spent`). When any claim on the card goes terminal with a handled
outcome (`complete`, `request_changes`, `redispatch`, `retry`, `handled`,
`acknowledge`, `comment_why_waits`, `surfaced`, `stale`, `superseded`, `superseded-by-terminal`),
`restart_spent` is cleared, so the next restart cut on the card is redispatched quietly again.
`failed`, `lost`, `stopped` and `dispatch_failed` clear nothing. `retries` stays the
per-card-lifetime budget of the crash-class retry (D-13); the blocked assessment has its own
`blocked_spent` (table in §3). A restart neither reads nor spends either, so a block-spent card still
gets its quiet restart redispatch and a restart redispatch never exhausts the next blocked assessment. A state file from before this change
(no `restart_spent`) is migrated on first read: the count a card's `<key>#restartN` claims account
for (capped by its `retries`) moves from `retries` to `restart_spent`, and the next write persists it.

**`[boomerang-wake cancelled; ignored]`**
A handoff that arrived from a child after the reconciler cancelled it. It is tagged and not
rendered in the origin chat. What to do: nothing; the re-surfaced wake is the live one.

## 6. Who writes the board: the child proposes, the gateway signs (D-19, spec §5.8)

A boomerang child, fork or summary, runs inside the delegated-child kanban fence
(`hermes_cli/kanban_db.py::_assert_not_delegated_child_mutation`). It opens board DBs read-only,
and its only write is a comment tagged ` (subagent)`, which the review machinery treats as
machine-written. So a boomerang-wake child cannot complete, send back, re-dispatch or approve
anything itself.

Instead, the child ends its final message with ONE verdict block:

```
<<<BOOMERANG-VERDICT nonce=<hex16>>>>
{"action": "complete" | "request_changes" | "redispatch" | "retry" | "surface",
 "evidence": ["<command + result>", ...], "findings": ["..."], "summary": "<line>"}
<<<END-VERDICT>>>
```

The gateway's executor (`gateway/kanban_boomerang_wake.py::execute_verdict`) does at most ONE
board write for it:

- **Accept only a real verdict.** The block must be the only one in the child's final message,
  must come last, and must carry the nonce minted for this dispatch. The nonce is never on the
  board, so a worker cannot pre-forge a verdict.
- **Only allowed actions.** The action must be allowed for the wake row recorded at claim time:
  - `completed`: complete, request_changes or surface.
  - `own_repo_review_red` (a dirty, conflicted PR only since t_88864f95; an unconflicted red takes
    no fork, like green): request_changes, redispatch or surface.
  - `changes_requested`: redispatch or surface.
  - `crash_first_occurrence`: retry or surface.
  The executor never merges, approves a PR, stamps coverage, edits config or touches another card.
- **`land_inherited` is retired (t_88864f95, Ace 2026-11-14).** A red check has not held a merge since
  2026-11-09, so no child proves a red is main's. A verdict carrying it is refused as an unknown action.
- **Card, PR and assignee come from the board, never from the child.** If the card moved since
  the claim, nothing is written and the wake is re-surfaced (`reason: state moved`). Moved means a
  newer event other than the child's own comment, a status change, or a new PR head. Redispatch
  goes to the card's current assignee only.
- **Signed as a machine.** Every write carries `operator_kind: machine`, `via: boomerang-wake` and the
  `delegation_id`, and every comment starts with `[boomerang-wake]`. The review machinery
  (hermes-home `scripts/lib/operator_identity.py` / `our_authors.py`) therefore never reads an
  executor write as a human hold.
- **Crash-safe.** The state goes `executing` → write → `terminal`. After a restart, the reconciler
  counts only the executor's own `boomerang_wake_executed` receipt (or a pre-rename `auto_handle_executed` one) as proof the write happened.
  Quoted text never counts.

Anything rejected writes nothing. You see the `⚠️ boomerang-wake failed <card>: <reason>` line, and
the original wake is re-surfaced.

**Residual (accepted):** the fence is cooperative scoping, not confinement. A full-toolset fork
(D-11) with `terminal` could still write the board's SQLite file directly. That gap existed
before v2; D-19 does not claim a hard boundary.

Log lines (`logs/agent.log`, from `tools.delegate_tool`): `boomerang.fork_ineligible reason=…`,
`boomerang.fork served_by=… read_share=…`. In `logs/gateway.log`:
`kanban notifier: boomerang-wake <card> disposition=…`.

## Decisions cited

D-1, D-2, D-6, D-7, D-8, D-9 (superseded 2026-10-21: no prefix ceiling), D-11, D-13, D-14, D-15, D-16, D-17, D-18, D-19 — all in spec §4.
