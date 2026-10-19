# Boomerang v2 — operator guide

Read this when you dispatch a boomerang, read a `boomerang.fork_ineligible` log line, see an
`[auto-handled]` or `⚠️ auto-handle` line in a chat, or decide whether to turn on `auto_handle`
for a kanban card. You do not need the spec to act on anything here.

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

## 1. Which mode each entry point uses (spec §5.6)

| Entry point | `inherit_context` | Mode you get |
|---|---|---|
| raw `delegate_task(...)`, `inherit_context=true`, no `inherit_mode` | `true` | `summary`. Byte-identical to v1 (INV-1). |
| raw `delegate_task(...)`, `inherit_mode="fork"` or `inherit_mode="branch"` | `true` | `fork` requested, `summary` on any failed check. |
| raw `delegate_task(...)`, `inherit_mode="summary"` | `true` | `summary`. |
| skill `boomerang` or `/boomerang <task>` | `true` | `fork` requested (D-1). |
| `/boomerang --summary <task>` | `true` | `summary`. |
| `/boomerang --fork <task>` ≡ `/boomerang --branch <task>` | `true` | `fork` requested (explicit; same as the default). |
| kanban auto-handle dispatch (§4 below) | `true` | `fork` requested; the disposition row's `mode` may say `summary` (D-6); falls back to `summary` on any failed check. |

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
| `prefix_le_cap` | Parent prefix > `min(fork_max_prefix_tokens` (600k)`, 0.6 × child model context window)` (D-9). | Accept the fold, or compress the parent session first. Do not raise the cap to force it. |
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
only for cards where auto-handle resolves to on (§4).

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
| `auto` | The passive line posts as today, tagged `[auto-handling…]`. The gateway takes a single-owner claim on `(board, task_id, event_id)` (if the other wake path already holds it, stop). It snapshots the home session, dispatches a boomerang child (fork requested) whose goal is a fixed instruction with the wake text quoted as DATA, writes a ledger entry, and advances the cursor only after dispatch is accepted. The parent's live turn is never interrupted. |
| `surface` | Existing path, unchanged: passive line + wake turn in the live session. Ace sees it exactly as today (INV-5). |
| `none` | Existing path, unchanged. No child, no claim. The merge pass owns the event. |

The dispatcher dispatches a child only on an explicit `auto`. Any other string, or an exception
in `wake_disposition`, is treated as `surface` (fail-safe).

Each `auto` row may carry `mode: fork|summary` (default `fork`, subject to §2 eligibility).
`completed` verification may default to `summary` if fork turns out to lose on cost for that kind
(D-6, AC-4b).

Retries (D-13): a crash-class event gets one automatic retry. The count is persisted per card in
`kanban_auto_handle.json` (`retries[task_id]`), survives restarts, and resets when the card
reaches `completed`/`archived` or when Ace surfaces-and-acts on it. The second occurrence
surfaces.

## 4. Turning auto-handle on and off

**Default: OFF everywhere (D-15).** Apollo enables it by judgment, per card or per session. This
is an experiment; default-on is revisited only on measured data.

**Resolution order at wake time** (tri-state resolved to a bool before any truthiness check):

1. `card.auto_handle`, if set (`true` or `false`). Set with `kanban_create` / `kanban_update`
   `auto_handle: bool`.
2. else the home session's setting: `/autohandle on|off|status` in that session (stored in
   session metadata, read on every wake, so cards already owned by the session follow a change).
3. else the board default.
4. else `false`.

Example: card `auto_handle` unset + home session `/autohandle on` → auto-handle applies.

**Kill switches** (config, read every notifier tick, no restart; INV-7):

| Key | Default | Effect when `false` |
|---|---|---|
| `delegation.boomerang.auto_handle_enabled` | `false` | Disables auto-handle (P3) fleet-wide. Every wake surfaces as today, whatever the card/session/board say. Both this AND the resolved card value must be true for auto-handle to run. |
| `delegation.boomerang.fork_enabled` | on (unset = forks allowed) | Disables fork mode (P2). Every boomerang, including auto-handle children, runs `summary`; logs `reason=fork_enabled`. |

Scope: auto-handle is armed for the `default` (Apollo) profile only (spec §2).

## 5. Lines you will see in the origin chat

**`[auto-handled] <card> — <Outcome>`**
One line per auto-handled wake, rendered from the child's structured handoff (Outcome / Changed
/ Commands-validation / Blockers), with expandable detail (D-7, INV-6). What to do: read it; act
only if the Outcome is wrong. If the child did something that needed you, revert it and treat it
as a mis-disposition (it counts against the D-15 default-on threshold).

**`[auto-handling…]`**
The passive event line for a wake that went to a child. A matching `[auto-handled]` or `⚠️` line
follows. What to do: nothing yet.

**`⚠️ auto-handle failed for <card>: <reason>`**
Child dispatch raised, or the child crashed or timed out. The original wake is re-delivered to
the home session as a normal surfaced wake (INV-6). What to do: handle the surfaced wake as you
would any wake. If the same card keeps producing this, set `auto_handle: false` on it.

**`⚠️ auto-handle lost <card> (reason: gateway restart|timeout)`**
The reconciler (§5.7) found a claimed entry whose child died (gateway restart) or ran past its
`max_minutes` (default 45); in the timeout case it cancelled the child first (bounded 60 s wait).
The original wake is re-surfaced through the normal path, once. What to do: handle the surfaced
wake. Re-surfacing happens at most once per entry; after that it is an ordinary wake.

**`[auto-handle cancelled; ignored]`**
A handoff that arrived from a child after the reconciler cancelled it. It is tagged and not
rendered in the origin chat. What to do: nothing; the re-surfaced wake is the live one.

## 6. Who writes the board: the child proposes, the gateway signs (D-19, spec §5.8)

A boomerang child, fork or summary, runs inside the delegated-child kanban fence
(`hermes_cli/kanban_db.py::_assert_not_delegated_child_mutation`). It opens board DBs read-only,
and its only write is a comment tagged ` (subagent)`, which the review machinery treats as
machine-written. So an auto-handle child cannot complete, send back, re-dispatch or approve
anything itself.

Instead, the child ends its final message with ONE verdict block:

```
<<<BOOMERANG-VERDICT nonce=<hex16>>>>
{"action": "complete" | "request_changes" | "redispatch" | "retry" | "surface",
 "evidence": ["<command + result>", ...], "findings": ["..."], "summary": "<line>"}
<<<END-VERDICT>>>
```

The gateway's executor (`gateway/kanban_auto_handle.py::execute_verdict`) does at most ONE
board write for it:

- **Accept only a real verdict.** The block must be the only one in the child's final message,
  must come last, and must carry the nonce minted for this dispatch. The nonce is never on the
  board, so a worker cannot pre-forge a verdict.
- **Only allowed actions.** The action must be allowed for the wake row recorded at claim time:
  - `completed`: complete, request_changes or surface.
  - `own_repo_review_red`: request_changes, redispatch or surface.
  - `changes_requested`: redispatch or surface.
  - `crash_first_occurrence`: retry or surface.
  The executor never lands, merges, approves a PR, edits config or touches another card.
- **Card, PR and assignee come from the board, never from the child.** If the card moved since
  the claim, nothing is written and the wake is re-surfaced (`reason: state moved`). Moved means a
  newer event other than the child's own comment, a status change, or a new PR head. Redispatch
  goes to the card's current assignee only.
- **Signed as a machine.** Every write carries `operator_kind: machine`, `via: auto-handle` and the
  `delegation_id`, and every comment starts with `[auto-handle]`. The review machinery
  (hermes-home `scripts/lib/operator_identity.py` / `our_authors.py`) therefore never reads an
  executor write as a human hold.
- **Crash-safe.** The state goes `executing` → write → `terminal`. After a restart, the reconciler
  counts only the executor's own `auto_handle_executed` receipt as proof the write happened.
  Quoted text never counts.

Anything rejected writes nothing. You see the `⚠️ auto-handle failed <card>: <reason>` line, and
the original wake is re-surfaced.

**Residual (accepted):** the fence is cooperative scoping, not confinement. A full-toolset fork
(D-11) with `terminal` could still write the board's SQLite file directly. That gap existed
before v2; D-19 does not claim a hard boundary.

Log lines (`logs/agent.log`, from `tools.delegate_tool`): `boomerang.fork_ineligible reason=…`,
`boomerang.fork served_by=… read_share=…`. In `logs/gateway.log`:
`kanban notifier: auto-handled <card> disposition=…`.

## Decisions cited

D-1, D-2, D-6, D-7, D-8, D-9, D-11, D-13, D-14, D-15, D-16, D-17, D-18, D-19 — all in spec §4.
