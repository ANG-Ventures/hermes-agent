# Spec: `kanban set-model --live` — switch a running worker in place

Card t_033a3bb1 (Ace 2026-09-27 11:14 PT): "is there a version of set-model that
does not reset the context — that doesn't abort the running worker?"

## Problem

`set-model` on a RUNNING card is next-dispatch only. The live worker keeps its
old route until it exits. `--reclaim` makes the change immediate, but it
SIGTERMs the worker: the next run is a fresh session seeded from the card, and
the in-flight conversation is lost. There was no way to change route or effort
and keep the conversation.

## Contract

```
hermes kanban set-model <card...> [<model>] [--provider P] [--effort L] --live
```

- Writes the route/effort to the card exactly as without `--live`, with the same
  gates: flagship `--allow-flagship`, `--pin-sub`, aliases, and provider
  validation. The write goes through the same single batch transaction.
- In that transaction, for each selected card that is `running` with a
  `current_run_id`, it appends a `route_changed` event scoped to that run. The
  payload `{live, model, provider, reasoning_effort, touch_model, touch_effort}`
  records what THAT live write asked for. Every explicit `--live` request
  appends a new event, even when the stored route is unchanged, so repeating a
  refused request retries it.
- A run whose worker has recorded `route_live_unsupported` (the
  `codex_app_server` runtime) gets no `route_changed` event; the receipt says
  `applies=next-dispatch` instead of promising a switch.
- Cards that are not running get the plain next-dispatch write. The receipt
  says so per card: `applies=live(run N)` vs `applies=next-dispatch`.
- Refused with exit 2 and nothing written:
  - `--live --reclaim`. The two contradict each other.
  - A clear (`set-model <id> none --live`, `--clear-effort --live`). A clear
    resolves through lane overrides and the capped-pool ladder at dispatch
    time, and a running worker cannot reproduce that. Clear without `--live`.

## Worker mechanics

`agent/conversation_loop.py` calls
`kanban_worker_route.apply_pending_live_route(agent, iteration, active_system_prompt)`
at the top of every loop iteration. That point is the boundary between two
provider calls, after the previous tool results were appended.

1. Inert unless this process is the owning kanban worker (task env +
   `owns_kanban_worker_authority()`). A non-worker pays one env read.
2. Only the first top-level agent in the process follows the card. Delegation
   children (`_delegate_depth > 0`), review forks and helpers are ignored.
3. One indexed read: every `route_changed` event for `(task, run)` newer than
   the cursor. With no event it stops there. The cursor advances only once the
   events are handled (switched, refused, or already on the route); a failed
   read leaves them pending for the next iteration.
4. Re-read the card row. If `current_run_id` no longer equals this run, stop.
   Coalesce the pending events: only fields an event touched (`touch_model`,
   `touch_effort`) change, each taking the value from the latest pending event
   that touched it. A card field written without `--live` never rides along.
5. Re-apply the gates on the route about to go live:
   - Flagship model: requires the card's `flagship override:` comment. This is
     the dispatcher's spawn gate.
   - `validate_route_provider(model, provider, pin_sub_reason=card.pin_sub_reason)`.
     This is the write gate for single-sub pins and refused aliases.

   On failure the worker writes `route_switch_refused {event_id, to, reason}`
   and stays on its current route.
6. Model/provider change: resolve credentials with
   `model_switch.switch_model(..., probe_catalog=False)`, then call
   `AIAgent.switch_model(...)`. This is the mid-session `/model` path. It
   rebuilds the client and credential pool, updates `_primary_runtime` (so
   fallback restore and pin-refusal logic see the new primary), resets
   fallback state and updates the session billing route. Effort travels as
   `session_reasoning_config`.
   - `switch_model` clears `_cached_system_prompt` so the next turn rebuilds
     it. Mid-loop that would be wrong, so the worker keeps the old prompt and
     rewrites only its `Model:`/`Provider:` lines. This is the same rewrite a
     provider failover does. The loop's `active_system_prompt` gets the same
     rewrite.
   - A resolution or rebuild failure goes to `route_switch_refused`.
     `switch_model` rolls itself back.
7. Effort-only change: `agent.reasoning_config` (and
   `_primary_runtime["reasoning_config"]`) is replaced. There is no client
   rebuild, the model and provider are unchanged, and the system prompt bytes
   are unchanged.
8. The worker writes `route_switched {event_id, from, to, iteration, kind}` on
   the run and clears the card-pin cache, because the pin may have moved.
9. Idempotent: if the card route already equals the live route (for example,
   the event was written between claim and spawn), nothing happens.

## Costs

Measured in a live E2E on 2026-09-27: a real `hermes chat -q` worker process
built from this branch, a temp kanban DB, and session `20260927_112932_d9a6fa`
in the `daedalus-opus` agent.log.

| call | route | input tok | cache read |
|---|---|---|---|
| 1 | claude-bpr / claude-haiku-4-5 | 30,638 | cold (first call) |
| 2 | **live switch** → openai-codex / gpt-6-luna-900k | 24,495 | none (miss) |
| 3 | openai-codex / gpt-6-luna-900k | 24,547 | 24,064 (98%) |
| 4 | **live switch** → claude-bpr / claude-haiku-4-5 | 30,833 | 30,042 (97%) |
| 5–8 | claude-bpr / claude-haiku-4-5 | ~31–33k | 96–100% |

- A switch onto a provider with no warm prefix costs **one full-history
  cache write** on the first call (call 2). The next call is warm again.
- Switching back to a provider whose prefix is still inside its cache TTL is
  a **hit** (call 4, 97%). The `Model:`/`Provider:` rewrite restores the
  original bytes.
- Cross-vendor tool history round-trips both ways in one session. Claude
  `toolu_*` calls were followed by Codex `call_*` calls and then Claude again,
  with no errors. The session has one user message and a continuous
  assistant/tool chain (15 messages). This is the same transport conversion
  the gateway fallback path uses.
- On the `claude-bpr` **pool face**, the switch back showed no reseed penalty
  (97% hit). **Not measured:** a single-sub `claude-bpx-N` pin, where the CLI
  session lives on one box. A switch onto a different box's sub is expected to
  cost at least one cold call there (the lost-session recovery path). Measure
  it before relying on it for large contexts.
- Effort-only: there is no client swap and the system prompt and model are
  byte-identical (unit-tested). Only the request's reasoning parameter
  changes. **Not measured live:** on Anthropic, a thinking-budget change can
  invalidate cached *message* blocks (the system and tools prefix stays
  cached).
- Latency: the switch lands at the next iteration boundary. A long-running
  tool call delays it until that call returns. The poll is one indexed SQLite
  read per provider call.

## Out of scope / not supported

- Capacity-aware placement. The dispatcher's capped-pool fallback ladder does
  not run for a live switch; the worker goes to the named route. If that route
  is capped, the worker's normal runtime failover and pin rules apply. Use
  `--reclaim` when you want the dispatcher to choose.
- The `codex_app_server` runtime. It bypasses the conversation loop, so the
  hook never runs. Its worker writes a run-scoped `route_live_unsupported`
  marker at turn start: pending live events are refused on the board, and later
  `--live` writes to that run land as next-dispatch. Use `--reclaim`.
- Surfaces: CLI only. The dashboard model dropdown and the `kanban_*` tools
  keep next-dispatch semantics.
- The "model switched" user-message note the interactive `/model` adds. A
  worker loop cannot inject a synthetic user message mid-loop because of the
  alternation invariant. The identity lines in the system prompt carry the
  change instead.

## Tests

`tests/hermes_cli/test_kanban_set_model_live.py`:

- CLI:
  - A running card gets the route plus a run-scoped `route_changed`.
  - A non-running card gets a next-dispatch write only.
  - The batch receipt names the run.
  - The three refusals write nothing.
  - `--reclaim` is unchanged and writes no live event.
- Worker, through the real `run_conversation` loop:
  - A provider switch mid-run keeps the full history. The request to the new
    client carries system, user, assistant(tool_call) and tool. Workspace
    writes survive, and `route_switched` lands on this run at iteration 2.
  - An effort-only switch changes only the reasoning parameter: same client,
    same model, byte-identical system message, no `switch_model` call.
  - A flagship route with no override comment is refused and recorded.
  - An event for another run is ignored.
  - Inert outside a worker (no DB read).
  - Only the first top-level agent follows the card.

Mutation check: with the loop hook removed, the three loop tests fail.
