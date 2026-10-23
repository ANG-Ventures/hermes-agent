---
name: boomerang
description: >-
  Run a scoped task in a background child agent that INHERITS this conversation — as a true fork (byte-identical prefix, reads the parent's warm cache; default) or a summary fold (fallback) — and folds only a handoff back. Use on /boomerang <task> or "boomerang this", and for boomerang-wake kanban cards.
version: 2.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    requires: [delegate]
    portability: hermes-only
    tags: [boomerang, delegate, subagent, context-inheritance, autonomous, token-efficient]
    related_skills: [context-cost-discipline, structured-handoff]
---


> 🧭 **Worker wrong (esp. repeatedly)? Suspect the CONTEXT SOURCE first** — a stale doc/comment/default the workers all read, not a bad worker. Fix the source, then machine-guard, THEN steer. → `swarm-dispatch-resilience` § SUBAGENT GUARDRAILS + `coding-guardrails` § fix-the-source.

# Boomerang

Hand a task to a background child agent that **inherits this conversation**, runs it
**autonomously**, and returns **only a structured handoff**. The token-heavy middle (file
reads, edits, command output) lives and dies in the child's window.

Built on `delegate_task` `inherit_context` + `inherit_mode`. Spec:
`~/.hermes/plans/2026-10-18_boomerang-fork-autowake-spec.md` (v2); operator guide:
`docs/boomerang-v2/README.md` in hermes-agent.

## When to use it

- The user typed `/boomerang <task>`, or said "boomerang this".
- A **scoped, self-contained** job that would otherwise bury this thread in probe noise (a
  bug fix that reads 10 files, a focused investigation, a build-and-verify loop).
- The child genuinely needs the **live session state** — otherwise a plain `delegate_task`
  with a written `context` brief is cheaper.

**When NOT to boomerang:** a 2-3 tool-call task (do it inline); or a task that needs nothing
from this conversation (a subagent dispatch (`act:delegate`) without inheritance). See
`context-cost-discipline`.

## Step 0: register every background boomerang in the liveness ledger

Before the `delegate_task` call, append the dispatch to the standing
`delegation-ledger.jsonl`. This command is a gate: if it exits nonzero, do not
launch an unwatched boomerang.

```bash
TASK_SUMMARY="<short task summary>"; ARTIFACT="<absolute incremental artifact path>"; MAX_MINUTES=45
HH="${HERMES_HOME:-$HOME/.hermes}"; DL="$HH/scripts/delegation_ledger.py"
DELEGATION_ID="$(python3 "$DL" mint)" && \
python3 "$DL" dispatch \
  --delegation-id "$DELEGATION_ID" \
  --task-summary "$TASK_SUMMARY" \
  --expected-artifact "$ARTIFACT" \
  --max-minutes "$MAX_MINUTES"
```

Repeat `--expected-artifact` for multiple outputs. Add `--pgrep-pattern` only for
a recognizable side-effect process, never for the child thread itself. The
append prints the minted id; copy that observed `del-...` value into the goal:
**"Delegation ID `<DELEGATION_ID>`: include it verbatim in your final
summary, and write `<expected artifact>` incrementally as you work."** When the
summary re-enters the parent conversation, close the entry:

```bash
HH="${HERMES_HOME:-$HOME/.hermes}"
python3 "$HH/scripts/delegation_ledger.py" close \
  --delegation-id "<observed del-...>" --reason completed
```

## The two modes (D-14)

| Mode | What the child gets | Cache |
|---|---|---|
| **`fork`** (≡ **`branch`**, D-16) — default for this skill (D-1) | The parent's exact system prompt, tool schemas and message array up to the last completed turn, plus one framing user turn with your goal. Same lane, same model, same cache TTL; first request pinned to the parent's seat (D-3). | Reads the parent's warm prefix. |
| **`summary`** (the v1 fold) — explicit, or the automatic fallback | One labelled "INHERITED CONTEXT FROM PARENT SESSION" message summarizing recent turns (≤ `boomerang.inherit_max_tokens`, ~50k, clamped to a quarter of the child window), under a slimmer child system prompt. | Shares nothing with the parent; its saving is isolation, not cache. |

"Fork" and "branch" are the same mode; accept either, never argue the distinction. A fork is a
fork only when everything in the cached prefix matches; if any check fails, the child runs
`summary` and the log says `boomerang.fork_ineligible reason=<reason>` (INV-2). Never describe a
`summary` child as cache-aware.

## How to run it

Dispatch a subagent (`act:delegate`) with `inherit_context=true` and the mode:

Capability ids in the block below (bound per harness in `skills-shared/_bindings/`): `act:delegate`.

```text
delegate(
    goal="<the task>. Execute autonomously: make reasonable assumptions, do NOT stop "
         "to ask questions. If you hit a genuine blocker, complete what you can and "
         "report the blocker in your summary. End with a structured handoff:\n"
         "  Outcome: <what happened>\n"
         "  Changed: <files/state mutated, or 'none'>\n"
         "  Commands/validation: <what you ran + pass/fail, or 'none'>\n"
         "  Blockers: <blockers, or 'none'>",
    inherit_context=true,
    inherit_mode="fork",      # or "branch" (same); "summary" for the fold
)
```

Mode by entry point (spec §5.6):

| Entry point | Mode |
|---|---|
| this skill / `/boomerang <task>` | `fork` requested |
| `/boomerang --summary <task>` | `summary` |
| `/boomerang --fork <task>` ≡ `--branch` | `fork` requested (explicit) |
| raw `delegate_task(inherit_context=true)` with no `inherit_mode` | `summary` — unchanged from v1 (INV-1) |
| kanban boomerang-wake dispatch | `fork` requested; the disposition row may say `summary` (D-6) |

Notes:
- **Delivery is asynchronous.** The child's handoff re-enters this conversation as a later
  message. Acknowledge the dispatch ("boomerang running; the summary will land here") and
  continue.
- **Autonomous-execution discipline goes in the `goal`** (the child can't see this skill).
  In fork mode the gateway wraps it in a framing turn telling the child the turns above are its
  own (INV-4); keep the structured-handoff shape regardless.
- **Pick `summary` on purpose** when the parent is not on a pool lane, the parent cache is cold,
  or you changed tools/skills/effort since the last turn — the fork would downgrade anyway.

## What a forked child may and may not do

- It carries the parent's full tool schemas and SOUL (D-11; byte identity needs them), bounded
  by `max_iterations`, the ledger `max_minutes`, and the lifecycle guards.
- At dispatch it refuses gateway-restart-class `terminal` commands, `clarify`, and
  `inherit_mode=fork`; the child sees a tool error (D-18).
- It cannot fork again: a nested fork downgrades with `reason=fork_depth` (D-17).

## Fork eligibility: reasons in `boomerang.fork_ineligible reason=<…>`

Each is a downgrade to `summary`, not a failure.

| `reason=` | Means | Do |
|---|---|---|
| `same_lane` | Parent not on a pool-affinity lane, or child lane differs (cross-lane = delegation; `claude-bpr` cannot fork). | Expected off pool lanes. |
| `same_model` | Child wire kwargs still differ from the parent's after the fork forced model/tier/provider. | Bug report; defence-in-depth tripped. |
| `same_request_params` | Tools/schemas, thinking/effort, system bytes, or cache TTL differ. | Expected after a mid-session tool/skill/effort change; else bug report. |
| `turn_complete` | Parent had in-flight tool calls at dispatch (D-8). | Retry after the turn, or accept the fold. |
| `seat_known` | No `last_served_by` recorded for the parent. | Nothing. |
| `warm` | `now − last_served_at ≥ ttl_s − 300 s`; cache expired or about to. | Nothing; a cold fork costs more than the fold. |
| `fork_enabled` | `delegation.boomerang.fork_enabled: false` (kill switch). | Intended if flipped. |
| `fork_depth` | Caller is itself a fork (D-17). | Nothing. |
| `pin_refused` | Relay refused the pinned seat (draining/cooling); pinned requests are served or refused, never rerouted (INV-3). | Nothing; fold ran on the parent's lane. |

**`warm` reads the TTL from the parent's last response usage split, not `_cache_ttl`:**
`ephemeral_1h_input_tokens > 0` → 3600 s; else `ephemeral_5m_input_tokens > 0` → 300 s; else
`_cache_ttl`. (apx on `claude-alr` bills 1h unless the seat is in overage — Phase 0, P0.4.)
**5m-TTL parents never fork, by design** (300 − 300 = 0): forks are for human-paced sessions.

## Boomerang-wake kanban cards

A card's subscription level is `notify`, `wake` or `boomerang wake`. At `boomerang wake` the
gateway sends a mechanical wake to a boomerang child instead of interrupting the live session,
unless the wake needs human judgment.

- **The card is the only setting.** No config knob, no session toggle, no board default. Set it
  with `kanban create --boomerang-wake`, `kanban edit <id> --boomerang-wake|--no-boomerang-wake`,
  `notify-subscribe --boomerang-wake`, or `kanban_create` `boomerang_wake: true`. The
  subscription row stays `notify+wake`; the level is `tasks.boomerang_wake` only.
- **Emergency stop:** the file `<HERMES_HOME>/state/boomerang-wake.disabled` (present = every
  wake surfaces; one #logs line per transition), plus `delegation.boomerang.fork_enabled` for
  fork mode. Both read every tick, no restart.
- **Disposition precedence** (first tier that matches): card tag `irreversible`/`spend`/
  `outbound` → `surface`; `needs_input`, any `blocked`, `block_loop_detected` → `surface`;
  own-repo `review_requested` with PR green/pending → `none` (merge pass owns it); own-repo red
  `review_requested`, `completed`, `changes_requested`, first crash-class event → `auto`;
  everything else (incl. upstream-repo reviews, a second crash) → `surface`. Only an explicit
  `auto` dispatches a child.
- **`land_inherited`** (own-repo red row only): the child proves the red is main's (`inherited` proof).
  Before enqueueing with `--allow-red-ci inherited:<family>`, the executor re-checks: (a) the PR touches none
  of the red's owning files (the anchor source + harness, or the family's suites.json paths); (b) the anchor
  is absent at main, or the family is red in main's newest grade. It also needs the operator coverage at the
  head and zero open P0/P1. Any miss surfaces it.
- **Chat lines:** `[boomerang-wake] <card> — <Outcome>` (one per handled wake; read it, act only if
  wrong); `⚠️ boomerang-wake failed <card>: <reason>` or `⚠️ boomerang-wake lost <card> (reason:
  …)` → the original wake was re-surfaced; handle it like any wake.

- **The child cannot write the board (D-19).** It runs inside the delegated-child kanban fence:
  board reads only, plus ` (subagent)` comments. It verifies and ends its final message with one
  `<<<BOOMERANG-VERDICT nonce=…>>>` block (`complete|request_changes|redispatch|retry|land_inherited|surface`
  + evidence). The gateway executor checks the nonce, the allowed-action list and that the card
  has not moved, then does the ONE write, signed `[boomerang-wake]` as a machine. It never merges,
  approves or stamps coverage (`land_inherited` only enqueues the operator's own approved head). If you are the child: no verdict block, or a malformed one, means nothing
  is written and the wake goes back to the session.

Full tables and downstream effects: `docs/boomerang-v2/README.md`.

## Contract with the user

- Confirm the dispatch briefly, then keep going — the summary arrives on its own later.
- When the summary re-enters, **act on it or relay it** — synthesize, don't just paste it.
- If the child reports a blocker, surface it honestly; never dress a blocked run as done.
- When asked whether a boomerang reused the cache, answer from the log: a `fork_ineligible`
  line means it ran `summary` and shared nothing.
