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
surfaces.

## 4. The boomerang wake level

A card has one subscription level, and it is the only setting. There is no config knob, no
session `/autohandle` toggle and no board default (Ace 2026-10-22, t_7be18632).

| Level a person sees | Delivery mode on the waker row | What a wake does |
|---|---|---|
| `notify` | `notify` | Passive line only. |
| `wake` | `notify+wake` / `wake` | Passive line + a turn in the subscribed session. |
| `boomerang wake` | `notify+wake` / `wake` (+ `tasks.boomerang_wake = 1`) | Mechanical events go to a background boomerang child (§3 `auto`); everything else wakes as `wake`. |

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

Scope: boomerang wake is armed for the `default` (Apollo) profile only (spec §2).

## 5. Lines you will see in the origin chat

**`[boomerang-wake] <card> — <Outcome>`**
One line per boomerang-wake child, rendered from the child's structured handoff (Outcome / Changed
/ Commands-validation / Blockers), with expandable detail (D-7, INV-6). What to do: read it; act
only if the Outcome is wrong. If the child did something that needed you, revert it and treat it
as a mis-disposition (it counts as a mis-disposition).

**`[boomerang-waking…]`**
The passive event line for a wake that went to a child. A matching `[boomerang-wake]` or `⚠️` line
follows. What to do: nothing yet.

**`⚠️ boomerang-wake failed <card>: <reason>`**
Child dispatch raised, or the child crashed or timed out. The original wake is re-delivered to
the home session as a normal surfaced wake (INV-6). What to do: handle the surfaced wake as you
would any wake. If the same card keeps producing this, `kanban edit <card> --no-boomerang-wake`.

**`⚠️ boomerang-wake lost <card> (reason: gateway restart|timeout)`**
The reconciler (§5.7) found a claimed entry whose child died (gateway restart) or ran past its
`max_minutes` (default 45); in the timeout case it cancelled the child first (bounded 60 s wait).
The original wake is re-surfaced through the normal path, once. What to do: handle the surfaced
wake. Re-surfacing happens at most once per entry; after that it is an ordinary wake.

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
{"action": "complete" | "request_changes" | "redispatch" | "retry" | "land_inherited" | "surface",
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
  - `own_repo_review_red`: request_changes, redispatch, land_inherited or surface.
  - `changes_requested`: redispatch or surface.
  - `crash_first_occurrence`: retry or surface.
  The executor never merges, approves a PR, stamps coverage, edits config or touches another card.
- **`land_inherited` (t_47d1968c), own-repo red row only.** The child claims the PR's red is main's and adds
  an `inherited` proof `{family, job, anchor_or_test, main_sha, pr_files, proof}`. The executor
  (`gateway/kanban_boomerang_land_inherited.py`) treats it as a pointer and re-checks from GitHub:
  (a) the PR's files (`pulls/N/files`) do not touch the red's owning paths. For a mutation-anchor red these
  are the anchored source and its `mutate_*.sh` harness, both owned by the family. Otherwise they are
  the family's `suites.json` / `main-gate-set.json` paths at main's tip. (b) the red is main's: the anchor is
  present at the PR head, absent at main, and still named by main's harness; or the family is red in main's
  newest grade (`state/main-green-streak.json`, fresh). It also needs an operator `review_coverage` APPROVE at
  the head on the card, zero open Prism P0/P1 at the head, and no `--accept-open-p1` / `--bypass-fleetreview` /
  `--break-glass` on the queue row. Any miss degrades to `surface` with the reason, and nothing is written. On
  a pass the one write is `scripts/lib/landing_queue.py enqueue` with the row's existing fm_args plus
  `--allow-red-ci "inherited: <family> red on main at <sha10> (...)"`. fleet-merge still refuses a NEW red
  (rc 9) at land time. Records: a `[boomerang-wake] land_inherited` card comment with the proof, a row in
  `state/boomerang-wake.jsonl`, and one #logs line. Kill switch: `state/boomerang-wake-land-inherited.disabled`,
  read on every verdict.
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
