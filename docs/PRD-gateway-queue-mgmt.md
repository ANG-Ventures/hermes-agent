# PRD — Gateway `/queue` management verbs, `/queue run`, and `/stop` queue preservation

**Author:** Aegis · **Requested by:** Ace (Telegram, 2026-10-12 09:12) · **Status:** v1.5 — Momus p4 APPROVE-WITH-CHANGES folded (terminal: no further pass warranted). BUILT + Momus p3 folded (B4' proven by I14; RC1-RC5 fixed) on branch `aegis/queue-mgmt-gateway` (worktree /private/tmp/queue-mgmt off fork/main 32546878c9); Momus p1+p2 BLOCKs folded; pass 3 reviews spec + code diff · RULED (D3/D5/D8/D11-D13 by Ace 2026-10-12), ready for Momus review → build. Phase 0 PROVEN (§0b).
**Repo:** `ANG-Ventures/hermes-agent` (fork/main) → upstream PR to `NousResearch/hermes-agent`
**Ground truth tree:** `~/.hermes/runtime/hermes-agent` @ fork/main (2026-10-12)

---

## 0. Ground truth (measured, not assumed)

| Claim | Evidence |
|---|---|
| `/queue list / edit N / rm N / move A B / clear / add` **already exist — CLI only** | upstream `ce318290bdd` (2026-08-26, "Inspired by Factory Droid"); `hermes_cli/cli_loops_mixin.py::_queue_list/_queue_remove/_queue_edit/_queue_move/_queue_clear` |
| The gateway (Telegram/Discord/Slack/…) has **none** of them | `gateway/run_busy.py::_busy_queue_command` treats the whole arg string as the prompt; `gateway/run_inbound.py::_hm_cmd_queue` (idle) forwards the payload to the model as a plain user turn |
| …but the registry **advertises** them fleet-wide | `hermes_cli/commands.py:152` `CommandDef("queue", "…or list/edit/rm/move/clear queued prompts", subcommands=("list","edit","rm","move","clear","add"))` — one CommandDef feeds `/help` on every surface. On Telegram `/queue rm 2` today queues (busy) or sends (idle) the literal prompt "rm 2" → the model hallucinates a queue op. This is `hermes-slash-command-authoring` PITFALL 3 (advertised, undispatched) in subcommand form. |
| Gateway queue storage = **slot + overflow** | slot: `adapter._pending_messages[session_key]` (one event); overflow: `SessionState.conversation.queued_events` (list). `_enqueue_fifo` fills slot first, then overflow. Depth = slot(0/1) + len(overflow). Cap `_BUSY_QUEUE_MAX_PENDING`. |
| `/status` shows a **count** only | `slash_commands_status.py:255,320` → `gateway.status.queued` |
| **`/stop` is inconsistent with the queue** | `run_agent_cache.py::_interrupt_and_clear_session` L643-661: pops the slot (`get_pending_message`) and **discards it** if human (keeps `internal` wakes). It never touches non-internal overflow items. |
| Orphaned overflow **resurrects on your next message** | `run_busy.py::_rescue_orphaned_overflow` (called from `run_inbound.py:1374`): on the next inbound event for an idle session, the oldest orphan runs as THAT turn and the new message is enqueued *behind* it. |
| i18n keys for the CLI verbs exist under `cli.queue.*` (en.yaml:2541-2549); gateway has only `gateway.queue.usage/queued/queued_depth` (en.yaml:807-810) | — |
| Docs already claim it: "In the CLI, `/queue` with no arguments lists…" | `website/docs/reference/slash-commands.md:52` |

### Direct answers to Ace's two questions

**Q: `/queue X` then `/stop` — do you get X before the next turn?**
No, and worse, it depends on how many you queued:
- 1 queued → it sat in the slot → `/stop` **silently drops it**. Reply says only "Stopped."
- N≥2 queued → #1 dropped; #2..N survive as orphans and **run ahead of whatever you type next**, in order, with no notice.

That is a stop-command honesty bug (same class as `undo-redo-stop-command-honesty` modes 1-8): the reply reports a state that isn't true.

**Q: Is there `/unqueue`?** No. The verbs you want exist as `/queue rm|clear|list` on the CLI only. The gateway needs the port, not a new command.

---

### Drop-path inventory (every place a queued user message can vanish, measured 2026-10-12)

| # | Path | Code | User told? | Verdict |
|---|---|---|---|---|
| 1 | `/stop` pops the slot item and discards it (human only; `internal` wakes kept) | `run_agent_cache.py:643-661` | **No** | **BUG, fix in Phase 3** |
| 2 | `/stop` leaves overflow items as orphans → they run ahead of the next message | `run_busy.py::_rescue_orphaned_overflow` | No (log WARNING only) | Not a drop, but a surprise; fixed by #1's policy |
| 3 | `/new`, `/reset`, `/resume`, auto-reset, compression-exhausted reset wipe `conversation.queued_events` AND the slot | `_clear_conversation_scope` (5 call sites) | **No** | Intended (new conversation), but **must say "dropped N queued"** → Phase 3 |
| 4 | Depth cap 32 → 33rd item dropped | `run_busy.py:418`, `_BUSY_QUEUE_MAX_PENDING=32` | **No** (log only) | Low likelihood; add a reply → Phase 3 |
| 5 | A queued item whose text starts with a registered slash command is discarded at dequeue | `run_turn.py:4050-4062` | No | Intended (commands are never model input). Caught earlier at `/queue` time now that verbs parse → Phase 2 rejects `/queue /model x` up front |
| 6 | Gateway restart / drain | `run_shutdown.py:2269` spools slot+overflow; `run_startup.py:201` replays | n/a | **Safe**, already persisted |
| 7 | Gateway crash (SIGKILL/OOM) between enqueue and spool | in-memory only | n/a | Accepted (same as any in-flight message); out of scope |

**Ace ruling 2026-10-12:** queued messages must never drop silently. `/stop` must preserve them.

### 0b. Phase 0 result — PROVEN on the real `_handle_message` path (2026-10-12 11:05)

Harness: `tests/gateway/test_queue_command.py::_make_runner` (+ `adapter.get_pending_message` wired to pop); scratch `phase0_queue_probe.py`.
```
'/queue a'     -> 'Queued for the next turn.'
'/queue b'     -> 'Queued for the next turn. (2 queued)'
'/queue c'     -> 'Queued for the next turn. (3 queued)'
'/queue list'  -> 'Queued for the next turn. (4 queued)'     ← verb not parsed; "list" became prompt #4
  [after queues] slot='a' overflow=['b','c','list'] depth=4
'/stop'        -> '⚡ Stopping — finishing the current step and halting. You can continue this session.'
  [after /stop]  slot=None overflow=['b','c','list'] depth=3  ← 'a' DROPPED, reply silent
  next-message rescue would run first: 'b'                     ← orphans run ahead of the user's next message
```
Ace reproduced line 4 live on Apollo/Telegram 10:31.

**Mechanical constraint on any "auto-release after /stop" design (grounded):** `/stop` bumps the session run generation (`_interrupt_and_clear_session`). The queued-follow-up recursion inside the stopped turn (`run_turn.py::_run_agent_queued_followup` → `_run_agent(run_generation=<old>)`) is gated by `_is_session_run_current` (`run_turn.py:2380`, PR #814 zombie-chain fix) and is **dropped as stale by design**. Therefore the queue can only be released by a FRESH dispatch under a new generation: the same thing `_rescue_orphaned_overflow` does on the next inbound, or an explicit verb. This is why D7 (auto-drain from the stopped turn) is superseded by D11-D13.

### 0c. Momus pass 1 (BLOCK) — both blockers GROUNDED TRUE against the live tree (2026-10-12)

| Finding | Live-code evidence | Disposition |
|---|---|---|
| **B1** the slot is a run-next trigger, not a parking spot | `gateway/platforms/base.py:4927 _drain_pending_after_session_command` ("Tail of /stop, /new, /reset … spawn a fresh processing task for any follow-up queued meanwhile") pops the slot and starts processing it; `_finish_session_task` (5459) and the post-processing drain (5574) also consume it. | **ACCEPTED.** D12 cannot live in the slot. → D14 held store. |
| **B2** the stopped turn's post-turn drain consumes the queue | `gateway/run_turn.py::_run_agent_drain_pending` calls `_dequeue_pending_event` + `_promote_queued_event` with **no generation check**; the interrupted turn reaches it while unwinding. | **ACCEPTED.** → D15 currency gate on the drain. |
| **B2 aggravator (new, ours)** | The currency gate on the queued-follow-up recursion (PR #814, "stop stale queued turn recursion") was **REVERTED** 2026-09-26 by #1163 ("fork-PR audit DROP … no measurement found"). The stale recursion therefore RUNS a consumed item as a zombie turn whose output is discarded (mode 5 of `undo-redo-stop-command-honesty`), rather than dropping it. | D15 gates the POP (narrower than #814, which gated the recursion). The Phase 3 real-turn test supplies the measurement the audit found missing. |
| **Phase 0 was a proxy** | The probe ran `_handle_message` with a MagicMock agent: no real turn reached its drain. It proved slot-drop + mis-parse but NOT what the stopped turn does. | Phase 3 adds a real-turn harness (see I12). |
| RC1 media-only `/queue` | `_busy_queue_command`: empty text + `media_urls` is a valid prompt | ACCEPTED → I6b. |
| RC2 `/queue` uncapped | `run_busy.py:418` cap is on `_queue_or_replace_pending_event` only; `_busy_queue_command` → `_enqueue_fifo` has no depth check | ACCEPTED → cap moves onto `/queue` (I8). |
| RC3 unsafe rewrite | `_enqueue_fifo` returns silently when the adapter lacks `_pending_messages` | ACCEPTED → I5b. |
| RC4 restart contradiction | `run_shutdown.py:2269` flushes overflow; nothing flushes a new held list | ACCEPTED → I13. Non-goal line corrected. |
| RC5 namespace | parser path vs grep path | ACCEPTED → the parser lives beside the CLI mixin (same package as `commands.py`); I10's grep names that package. |
| RC6 stale rollback text | references the dropped alias | ACCEPTED → §7 rewritten. |

### 0d. Momus pass 2 (BLOCK) — dispositions

| Finding | Disposition | Evidence |
|---|---|---|
| **B3** verbs don't know `held_queue`; a rewrite via `_enqueue_fifo` re-arms the slot | **ACCEPTED → D20.** Already the built shape: `gateway/queue_mgmt.py::_store_for_verbs` picks ONE store (held if non-empty, else active); `_write_store("held")` mutates `held_queue` in place and never touches the slot. Test `test_mutate_held_keeps_rest_held_and_never_arms_the_slot`. | worktree `aegis/queue-mgmt-gateway` |
| **B4** dispatch from inside a guard / double dispatch on the knob path | **ACCEPTED → D16 re-specified to Momus's alternative:** release = restage into slot + overflow and RETURN; the adapter's existing single door dispatches (the post-processing drain `base.py:5574` / `_finish_session_task` for `/queue run`, which is an inline-reply command and does not cancel the running task; `_drain_pending_after_session_command` for knob-on `/stop`). No code calls `_start_session_processing`. Test through the real adapter base (I14). | `/queue` CommandDef is `busy_policy="dispatch"` → `_dispatch_inline_reply`, no cancel; `should_bypass_active_session` in commands.py |
| RC1 auto-reset wipes held silently | **ACCEPTED → D21.** | `ConversationState.clear()` resets every field |
| RC2 §5 unsafe rewrite text | ACCEPTED: §5 text replaced by D20. | — |
| RC3 namespace | **DISMISSED: reviewer-view artifact.** On disk the spec, the pack and the `run_turn` safety-net import name the same package literal (grep: 3 / 1+1 / 1 matches); Phase 1 tests import it and pass. | `grep -o '[a-z_0-9]*/queue_verbs.py'` over spec+pack |
| RC4 stale knob/§5/D5 text | ACCEPTED: struck below. | — |
| RC5 spool asserted, not shown | **ACCEPTED → D22**; I13 test drives the real shutdown flush. | — |
| RC6 non-goal says no run_turn change | ACCEPTED: §2 amended (D15 edits the drain call site). | — |
| Lens: non-stop gen bumps leave rescue-orphans | **Enumerated, acceptable:** generation bumps are exactly `/stop`* (held), `queue_run` (held then released), `new_command`/`session_reset` (scope cleared, count reported), reaped-session eviction (items stay active; next inbound rescues them — today's behaviour, unchanged). Ordinary interrupt-mode messages use `agent.interrupt()` with NO bump, so D15 never touches them. | grep `_invalidate_session_run_generation` / `_interrupt_running_turn` callers |
| Lens: await inside `/queue run` | Stated: the only await is `_interrupt_and_clear_session`, whose hold runs BEFORE its first await (code: hold immediately after the synchronous generation bump). `/queue` arrivals during the await append to held (D14), so the restage still sees every item. | `run_agent_cache.py` |

### 0e. Momus pass 3 (BLOCK: no new design-class blocker) — dispositions

| Finding | Disposition | Evidence |
|---|---|---|
| **B4'** release dispatch unproven; I9 named the wrong busy dispatcher | **FIXED.** I9 reworded: on busy `/queue run` the stopped turn's runner drain is skipped by D15; the dispatcher is the ADAPTER's end-of-task drain of the interrupted task's `_process_message_background` (`base.py` post-processing block that pops `_pending_messages` and spawns a drain task). Proven end to end by I14 (`tests/gateway/test_queue_release_real_adapter.py`: busy run, idle run, knob-on /stop each run a→b→c once with max 1 live turn; default /stop runs nothing). Mutation (restage no-op) kills all 3 release cases. | branch |
| RC1 `edit` to a slash command | FIXED: `edit` calls `refuse_command_prompt`; storage checked before mutating the shared event. Test `test_edit_refuses_a_slash_command_and_leaves_the_item`. | — |
| RC2a unclassified reasons wipe held silently | FIXED: only `session_reset`/`resume` drop (counted); every other reason CARRIES (+ WARNING when unclassified). Test enumerates every `_clear_conversation_scope(reason=...)` literal in `gateway/*.py`. | — |
| RC2b auto-reset drops active overflow | FIXED: auto resets carry held + active user items into held. | — |
| RC3 unguarded imports on /stop and the funnel | FIXED: both call sites wrapped, fail open. | — |
| RC4 docstring invariant false under `busy_input_mode: queue` | FIXED by correcting the semantics (D14: a plain message runs normally): `/queue list` shows later active items in an unnumbered "Also waiting" section; `/queue run` restages oldest first (held, then later active). New key `gateway.queue.active_after_held` (17 locales). | — |
| RC5 spec↔code drift | FIXED in this revision (D22, I13, rollback, D16, Phase 1 `move 1`). | — |
| DevOps: failed hold then discard | FIXED: if a user item is still in the slot after the hold, /stop re-seats it (overflow head, then slot) instead of discarding. | — |
| Tests: idempotent `release` says "Released N" when nothing moved | ACCEPTED as-is: the items ARE queued to run next, so the reply is true; no extra key. | — |

## 1. Summary & Goal

Make the gateway honor what its own `/help` already promises: `/queue list|edit|rm|move|clear|add` on every adapter with CLI-identical semantics. Add ONE new verb, `/queue run` (aliases `fire`, `release`, `go`), that releases the whole queue now: on an idle session it starts draining immediately; on a busy session it interrupts the current turn (exactly `/stop`) and then drains. Make `/stop` never lose a queued message and always say how many are waiting. Document the model in Obsidian + product docs.

Success = Ace types `/queue list` on Apollo/Telegram and sees the numbered queue; `/stop` with 3 queued says "3 queued, held"; `/queue run` fires them in order.

## 2. Non-Goals

- No new top-level `CommandDef` (`/unqueue`, `/fire`). Slack is at the 50-slash cap (PITFALL 1). `run` is a SUBCOMMAND of `/queue`.
- No LIFO / last-N removal form (D8).
- No NEW persistence mechanism: restart persistence already exists (shutdown spool, §0 inventory #6); this spec only extends it to the held store (I13).
- No cross-session queue ops; no editing of `internal` wake events or `/goal` continuations.
- No change to `/steer` or `/busy` modes. `run_turn.py` changes ONLY at the post-turn drain call site (D15 currency gate); the recursion itself is untouched. The restart spool gains only the D22 marker.

## 3. Constitution / Invariants

- **I1 — One queue view.** All verbs operate on the unified ordered list `[slot] + overflow` with 1-based indices in that order. *Proof:* `test_gateway_queue_mgmt.py::test_indices_span_slot_and_overflow_and_slot_stays_item_one`.
- **I2 — Slot-occupied invariant preserved.** After any mutation, if the list is non-empty the slot holds item 1 (so `_promote_queued_event` / `_rescue_orphaned_overflow` keep working and a mid-chain arrival can't jump the queue). *Proof:* `test_indices_span_slot_and_overflow_and_slot_stays_item_one`, `test_busy_management_verbs_keep_the_slot_armed`; scoped by D23 (idle verbs never arm).
- **I3 — Internal events are untouchable.** `event.internal` (async-delegation wakes) and `/goal` continuation events are listed as `[system]`, skipped by `clear`, and refused by `rm/edit/move` with an explicit message. Mirrors `_clear_goal_pending_continuations`. *Proof:* negative tests.
- **I4 — Render what was DONE, not what was asked.** Replies name the removed/edited item (index + preview); out-of-range → explicit error; empty → "Queue is empty." Never silent no-op (the mode-7 rule). *Proof:* assertions on reply text in each test.
- **I5 — Reads and writes happen on the event loop, no new locks.** `_pending_messages` and `queued_events` are loop-owned today; handlers stay `async` on the loop and do no `await` between read and write of the queue. *Proof:* code review + grep for `await` inside the mutation helper.
- **I6 — Management verbs are never queued as prompts.** `/queue list` on a busy session returns the listing, not "Queued for the next turn." `add` is the escape hatch for a prompt starting with a verb word (CLI parity). *Proof:* `test_busy_queue_verb_not_enqueued`.
- **I6b — Media-only `/queue` is still a prompt.** Bare `/queue` lists ONLY when the event carries no `media_urls`; with media it enqueues (today's behaviour). *Proof:* `test_media_only_queue_still_enqueues`.
- **I5b — Rewrite is fail-safe.** `_queue_rewrite` refuses (returns an error reply, mutates nothing) when the adapter has no `_pending_messages` dict; it mutates `queued_events` IN PLACE (`del lst[:]`/`extend`), never by assignment. *Proof:* `test_rewrite_refuses_without_slot_storage`, `test_rewrite_keeps_overflow_list_identity`.
- **I12 — The stopped turn consumes nothing.** With a REAL turn in flight (harness runs `_run_agent` to its drain, not a MagicMock), `/stop` then turn-unwind leaves `held_queue` == the pre-stop user items in order, slot/overflow hold only internal wakes, zero follow-up turns start, and `PHASE=stale_drain_skipped` is logged. Same for busy `/queue run`: every item runs exactly once, in order. *Proof:* `test_stale_turn_drain_consumes_nothing`, `test_busy_queue_run_releases_each_item_once_in_order`.
- **I14 — One door, one task.** Through the REAL adapter base (`BasePlatformAdapter` subclass with real `_pending_messages`/guards, stub transport): idle `/queue run`, busy `/queue run`, and knob-on `/stop`, each with 3 items → at most one session processing task at any instant, order a→b→c, each item exactly once; `test_mutate_held_keeps_rest_held_and_never_arms_the_slot` (`/stop` 3 → `/queue rm 1` → held == [b,c], slot unarmed) + `test_plain_message_after_stop_runs_and_held_items_stay_held` (real adapter: `ping` runs, held stay held). *Proof:* `tests/gateway/test_queue_release_real_adapter.py`.
- **I13 — Held items survive a restart as HELD.** Per D22, held events ride the restart-follow-up spool with `reason: queue_held`; startup restores them to `held_queue`. *Proof:* `test_held_queue_spool_roundtrip` drives the real `run_shutdown` flush path (not `flush_overflow_to_file` directly) and the real startup restore.
- **I7 — `/stop` never drops a queued user message and states the count.** Every pre-stop user item is in `held_queue` afterwards, same order (D14); reply appends "N queued, held (`/queue run` to release, `/queue clear` to drop)" when N>0. *Proof:* `test_stop_suffix_reminds_held_count`.
- **I8 — Every intentional drop is loud; `/queue` is capped.** `/new` `/reset` `/resume` reply with the dropped count (active + held) when >0. `/queue` refuses past `_BUSY_QUEUE_MAX_PENDING` (32, counted over held + slot + overflow) with a reply; today it is uncapped. *Proof:* `test_user_reset_drops_and_reports`, `test_cap_counts_held_and_active`.
- **I9 — `/queue run` releases through the adapter's single door.** It restages held → active (slot = item 1) and returns. Dispatcher, per path: busy `/queue run` → the interrupted turn's adapter task, whose post-processing block pops the slot and spawns a drain task (the stopped turn's runner-level drain is skipped by D15); idle `/queue run` → the `/queue` command's own adapter task, same block; knob-on `/stop` → `_drain_pending_after_session_command` (`/stop` is interrupt_then_dispatch). Items 2..N promote through the NEW turn's runner drain. No code calls `_start_session_processing`. *Proof:* I14.
- **I10 — One parser.** CLI and gateway import the same `hermes_cli/queue_verbs.py`; no second verb table. *Proof:* `grep -rn "QUEUE_VERB_ALIASES\|_QUEUE_VERBS =" hermes_cli gateway` → one definition.
- **I11 — Surface-cap + orphan gates stay green.** `tests/hermes_cli/test_commands.py`, `tests/gateway/test_gateway_command_handler_parity.py` unchanged-green.

## 4. Resolved Decisions (ledger)

| # | Decision | Why |
|---|---|---|
| D1 | Port the CLI verb set verbatim (`list/edit/rm/move/clear/add`) rather than invent `/unqueue` | Registry + docs already promise it on every surface; parity with CLI and upstream |
| D2 | Shared verb parser extracted to a pure module used by BOTH CLI and gateway | Two hand-rolled parsers = `duplicated-normalizer-drift`. CLI keeps its `queue.Queue` store; gateway gets a slot+overflow store; parser is common |
| D3 | **RULED 2026-10-12 (Ace): no `/unqueue`, no alias.** The existing `/queue rm|clear|list` verbs are the surface; document them well. Replaces the earlier alias proposal. | Verbs already exist; one surface, one vocabulary |
| D5 | **RULED 2026-10-12 (Ace): queued messages never drop silently; `/stop` preserves them.** `/stop` halts the current turn only; the queue is kept (held, D12/D14) and the user is told the held count. Resolves old Q2 as option B. | Ace: "we don't want queued messages ever dropping" |
| D7 | ~~auto-drain after `/stop`~~ **SUPERSEDED 2026-10-12 11:0x by D11-D13** (Ace: "we should have a verb and not by default release when you stop"). Also mechanically unsound as written: see §0b, stale-generation gate. | — |
| D11 | **RULED 2026-10-12 (Ace): new verb `/queue run`** (aliases `fire`, `release`, `go`) releases the entire queue now, FIFO, one full turn each. Idle: starts immediately. Busy: interrupts the current turn (same as `/stop`) then starts. Reply: "Released 3 queued prompts." | Ace: "there should be a verb for this… whether you're stopped or during chat, release it into chat and interrupt the chat that way" |
| D12 | **RULED 2026-10-12 (Ace): `/stop` does NOT auto-release.** It halts the turn, keeps the queue intact, and reminds: "Stopped. 3 queued, held (`/queue run` to release, `/queue clear` to drop)." | Ace: "when it's stopped it could mention if there's a queue to remind you" |
| D13 | Config knob `display.stop_releases_queue: false` (default false = D12). When true, `/stop` behaves as `/queue run` (busy branch). Shipped in v0.1 because it is one `if` at one site and Ace named it as a parameter; documented; no restart-less toggle promised (runner reads config at boot). | Ace: "it could be a parameter that we configure" |
| D8 | **RULED 2026-10-12 (Ace): ship CLI parity** — verbs `list/edit N/rm N/move A B/clear/add`, positional indices, no LIFO form. Resolves Q1 as A. | — |
| D14 | **Held store** `SessionState.conversation.held_queue: list[MessageEvent]`. `/stop` (default, D12) MOVES every non-internal item from slot + overflow into `held_queue`, in order; internal wakes stay exactly where today's code leaves them. No drain, rescue, or promotion path reads `held_queue`. While `held_queue` is non-empty, a new `/queue <prompt>` APPENDS to it (reply says "added to held queue") so FIFO holds. A plain (non-`/queue`) message runs normally as its own turn; the held items stay held. `/queue run` and `/queue clear` are the only exits. `/new`/`/reset`/`/resume` clear it and report the count. | Grounds B1: the slot has ≥3 automatic consumers; only a store none of them knows about can hold. |
| D15 | **Currency gate on the post-turn drain.** At the drain call site in `_run_agent` (the `_run_agent_drain_pending` call), if `not self._is_session_run_current(session_key, run_generation)`, skip the drain entirely (pop nothing) and log `PHASE=stale_drain_skipped session=… gen=…`. Items stay for the current owner. | Grounds B2. Narrower than reverted #814: it never pops, so nothing needs re-queueing and no zombie can run. |
| D16 | ~~original mechanics, steps 2-3 SUPERSEDED by I9 (Momus p2 B4: no `_enqueue_fifo` restage, no `_start_session_processing`)~~ `/queue run` mechanics: (1) if a turn runs → `_interrupt_and_clear_session(…, invalidation_reason="queue_run")`, which under D14 moves items to held; (2) move held → active via `_enqueue_fifo` in order; (3) pop the slot head and hand it to the adapter's own fresh-dispatch path (`_start_session_processing`, the call `_drain_pending_after_session_command` already uses), so it runs under a new generation; (4) items 2..N promote through the normal post-turn drain, which D15 keeps away from any stale turn. Idle with held empty and active non-empty: same step 3. | One dispatch path, already used by `/stop`'s tail. |
| D17 | `display.stop_releases_queue` (sibling of `display.busy_input_mode`; DEFAULT_CONFIG entry in `config_defaults.py`) is read with the house `is_truthy_value(cfg_get(...), default=False)`: absent / null / "false" → hold. When true, `/stop` restages the held items (I9) so `/stop`'s own tail drain releases them. | Momus config-drift note. |
| D18 | Indices are TOCTOU across messages (a turn ending between `list` and `rm 2` renumbers). Accepted for v0.1: every mutation reply echoes index + preview (I4), so a wrong target is visible immediately. | Momus test-lens note; prevention (ids) is not worth the surface. |
| D19 | `edit N` on a media-bearing item replaces the TEXT only; `media_urls`/`media_types` are kept. Reply notes "(attachment kept)". | Momus residual. |
| D20 | **Single helper.** Every read/write of either store goes through `gateway/queue_mgmt.py`: `_store_for_verbs` (held if non-empty, else active), `_write_store` (held: in place; active: `rewrite_active`, which fills slot then overflow and refuses without a slot dict), `hold_user_items`, release. I1/I2 are scoped: indices span the ONE store being shown; I2 (slot holds item 1) applies to the active store only. | Momus p2 B3 + lens note. |
| D21 | **Automatic resets carry held items; user resets drop them loudly.** `_clear_conversation_scope` with any reason other than `session_reset`/`resume` (auto resets, and any unclassified future reason with a WARNING) saves `held_queue` PLUS active user items, clears, and restores them into the fresh `held_queue`. `/new` `/reset` `/resume` (reason `session_reset` / `resume`) drop held + active user items and the command reply states "dropped N queued". | Idle auto-reset fires exactly when held items sit idle; there is no message to reply to, so silently dropping would violate D5. |
| D22 | **Spool: held items ride the EXISTING restart-follow-up spool** (`gateway/fork_ext/restart_followups.py`, the same one that already carries slot + overflow across restarts) with `reason: "queue_held"`. Boot restores those records into `held_queue` (acked immediately), replays the rest as turns; `queue_held` records are exempt from the 6 h `MAX_REPLAY_AGE_S` staleness rule. | Momus p2 RC5: a new file would be lost on rollback. |
| D23 | **Management verbs never arm the slot on their own** (Momus p4 RC-B). `rewrite_active(arm=None)` keeps the slot's armed state: a busy queue stays armed (the running turn drains it); an idle orphan queue stays in overflow, so `/queue rm`/`edit`/`move`/`clear` never start a turn. Release paths (`/queue run`, knob-on `/stop`, hold) arm explicitly; `/queue run` re-arms idle orphans so "Released N" is true. | A verb that edits the queue must not run it. |
| D24 | **Flake policy for the polling rigs** (I14, I12): every wait is bounded (≤5 s settle, ≤10 s thread wait) and asserts on final state, never on elapsed time. Measured 5/5 green (~4 s each) at land. A red on CI is re-run ONCE; a second red is a real failure (no quarantine without a card). | Momus p4 test lens. |
| D9 | Spec of record = repo `docs/PRD-gateway-queue-mgmt.md` (Phase 1 PR); this file is the draft | Workers card off the repo |
| D10 | Preview = first 60 chars, newline-collapsed, `[image]`/`[voice]` tags; `[system]` prefix for internal/goal items | Telegram width |
| D6 | Every remaining drop path (inventory #3, #4) replies with a count; #5 is refused at `/queue` time | Silence is the bug, not the drop |
| D4 | Bare `/queue` on the gateway = `list` (CLI parity). Today bare `/queue` returns usage; `usage` moves to `/queue help` | Matches CLI and docs line 52 |

## 5. Design

```
hermes_cli/queue_verbs.py  (NEW, pure)            gateway/run_busy.py
  parse_queue_command(text) -> QueueOp              _busy_queue_command  ──┐
    run aliases: run|fire|release|go                gateway/run_inbound.py │
    kinds: add|list|edit|rm|move|clear|run|help     _hm_cmd_queue  ───────┤
                                                                            ▼
hermes_cli/cli_loops_mixin.py (uses parser)      gateway/queue_mgmt.py (NEW mixin)
                                                    _queue_items(key, adapter) -> list[MessageEvent]
                                                    _queue_rewrite(key, adapter, items)   # restores I2
                                                    _handle_queue_mgmt(event, key, adapter) -> reply
                                                    _format_queue_list(items)   # 1. [you] preview… / 2. [system] …
```

- `_queue_items`: `[slot] if slot else []` + `list(overflow)`.
- ~~`_queue_rewrite`: `pending_slot.pop(key)`; `state.conversation.queued_events = []`; then re-`_enqueue_fifo` each item in order. (Reuses the existing writer → I2 by construction.)~~ SUPERSEDED by D20/D17 (Momus p2).
- Preview: first 60 chars, voice-sentinel aware (CLI `#65827` handling reused), media-only items shown as `[image]`.
- Idle path: `_hm_cmd_queue` dispatches verbs (list shows orphans → fixes the invisible-orphan surprise); a bare prompt still sends as a turn.
- Busy path: `_busy_queue_command` dispatches verbs before the existing enqueue branch.
- ~~`/stop` keeps items in the slot~~ SUPERSEDED by D14 (Momus B1). Original text kept for the record: `/stop` (D12): `_interrupt_and_clear_session` stops popping a HUMAN slot item (keeps the `internal`-wake handling exactly as is). `_handle_stop_command` / `_busy_stop_command` append `t("gateway.stop.queue_waiting", count)` when depth>0. The next inbound message then runs ahead of the queue via today's `_rescue_orphaned_overflow` order? **No**: with the slot intact the next inbound is a busy-path arrival and enqueues BEHIND the queue (FIFO preserved, which is what "never drop, never reorder" means). `/queue list` makes that visible; `/queue run`/`clear` are the controls.
- ~~old `/queue run` mechanics~~ SUPERSEDED by D16. Original: `/queue run` (D11): `_queue_run(key, adapter, source)`: if a turn is running → `_interrupt_and_clear_session(..., invalidation_reason="queue_run")`; then `head = _queue_items[0]` removed from the stores with the slot re-staged (reuse `_rescue_orphaned_overflow`'s body, factored into `_pop_queue_head_for_dispatch`), and `asyncio.create_task(self._handle_message(head))` so it dispatches as a fresh inbound under a new generation (I9). Remaining items drain via the existing post-turn promotion. Reply counts what was released.
- ~~D13 knob: in `_busy_stop_command`, `if cfg.stop_releases_queue: return await self._queue_run(...)`.~~ SUPERSEDED by D20/D17 (Momus p2).
- Idle `/queue run` with empty queue → `empty`. `/queue run` when the head is an `internal` wake → it runs (it was going to anyway); counted separately in the reply.
- Discord native slash: `/queue` is auto-registered from the registry with an `args` option (not hand-registered like `/undo` was in mode 7) — verify in Phase 2, don't assume.

## 6. Phases

- **Phase 0 — Prove the bug.** DONE, §0b. *Verify with:* `HERMES_HOME=$SCRATCH/p0home venv/bin/python $SCRATCH/phase0_queue_probe.py` → `slot=None` after `/stop`; `'/queue list' -> 'Queued…'`.

- **Phase 1 — Shared parser + CLI refactor.** `hermes_cli/queue_verbs.py` (`parse_queue_payload(payload) -> QueueOp`, the one alias table incl. existing `ls show set remove delete del pop` + new `run fire release go`); `cli_loops_mixin._cmd_queue` delegates; CLI gains `run` too (CLI: drain `_pending_input` immediately = send head now; if agent running, interrupt first — mirror of gateway semantics).
  - *Unit:* `tests/hermes_cli/test_queue_verbs.py` table: `""→list`, `list`, `ls`, `clear`, `rm 2`, `pop 2`, `edit 1 new text`, `move 2 1`, `run`, `fire`, `add rm 2`→add, `rm`→add("rm"), `clear the cache`→add, `run the tests`→add, `edit 1`→edit(empty→usage), `move 1`→move(to=None) (handler renders usage_move), `/model x`→add (gateway refuses it).
  - *E2E:* existing CLI tests from `ce318290bdd` still green.
  - *Negative:* prompt text `"list the files"` → kind `add` (CLI rule: only an exact first-token verb with valid arity is a verb).
  - *Verify with:* `venv/bin/pytest tests/hermes_cli -k queue -q` → all pass.

- **Phase 2 — Gateway verbs + `/queue run` + i18n.** `gateway/queue_mgmt.py`; wire busy + idle paths; `gateway.queue.*` keys (list_header, item, item_system, empty, removed, updated, moved, cleared_one/other, item_not_found, refused_system, refused_command, usage_edit, usage_move, help, released, cap_reached, dropped_on_reset) + `gateway.stop.queue_waiting`. Bare `/queue` → list. `/queue run` per D11/I9.
  - *Unit:* `tests/gateway/test_gateway_queue_mgmt.py` (harness = `test_queue_command.py::_make_runner` + popping `get_pending_message`); covers I1-I6, I9 (`test_busy_queue_run_releases_each_item_once_in_order`: busy + 3 queued → `/queue run` → interrupt called once, head dispatched via `_handle_message` with a new generation, slot re-staged with item 2, reply "Released 3").
  - *E2E:* live on Aegis Telegram: `/queue a` `/queue b` `/queue list` → `1. a / 2. b`; `/queue rm 1` → `b` promoted to slot; `/queue list` → `1. b`; wait for turn end → `b` runs once.
  - *Negative:* `/queue rm 1` while item 1 is an `internal` wake → refused with message, queue unchanged. `/queue clear` with a `/goal` continuation present → continuation survives.
  - *Discord:* `/queue rm 1` via native slash → `gateway.log` shows `slash '/queue rm 1' invoked` (args carried). If only `/queue` → mode-7 class, fix the registration in this phase.
  - *E2E (run):* Aegis Telegram: long busy turn, `/queue a` `/queue b`, `/queue run` → current turn stops, `a` then `b` answer as separate turns, nothing else typed.
  - *Verify with:* `venv/bin/pytest tests/gateway/test_gateway_queue_mgmt.py tests/gateway/test_queue_command.py tests/gateway/test_queue_consumption.py tests/gateway/test_stop_zombie_turn_lease.py tests/hermes_cli/test_commands.py tests/gateway/test_gateway_command_handler_parity.py -q` → green.

- **Phase 3 — Held store (D14), drain currency gate (D15), `/queue run` dispatch (D16), knob (D17), cap + loud drops (I8), spool (I13).**
  - *Unit:* `test_hold_moves_user_items_in_order_and_leaves_system_events` (slot human + 2 overflow + 1 internal → held=[a,b,c], internal untouched, reply "3 queued, held"); `test_stop_tail_drain_runs_nothing_held` (drive `_drain_pending_after_session_command` after `/stop`: no processing started); `test_plain_message_after_stop_runs_and_held_items_stay_held`; `test_new_queue_item_joins_held`; `test_idle_queue_run_releases_held_items_without_another_message`; `test_stop_releases_queue_knob_is_true_only` (absent/"false"/null → hold); `test_user_reset_drops_and_reports` (active + held); `test_cap_counts_held_and_active`; `test_held_queue_spool_roundtrip`.
  - *Real-turn harness (I12, answers Momus B2 + the #1163 "no measurement" audit):* run `_run_agent` with a stub agent whose `run_conversation` blocks on an Event, `/stop` mid-turn, release the Event so the turn unwinds through `_run_agent_drain_pending`; assert I12. Prove RED on base: the same test on unmodified fork/main must show the queue head consumed (that red IS the measurement #814 lacked).
  - *E2E:* Aegis Telegram: long turn, `/queue a` `/queue b` `/queue c`, `/stop` → "3 queued, held"; wait 2 min → nothing runs; `/queue list` → 3 held; send "ping" → ping answers, held still 3; `/queue run` → a, b, c answer in order, once each. `grep -c PHASE=stale_drain_skipped` ≥ 1 over the window.
  - *Negative:* `/queue clear` then `/stop` → plain "Stopped."; knob true → `/stop` with 3 queued releases them (D17).
  - *Verify with:* `test-gate venv/bin/python -m pytest tests/gateway -k "held or stale_drain or queue_run or queue_cap or reset_reports or spool" -q`.

- **Phase 4 — Docs (the deliverable Ace asked for).** `website/docs/reference/slash-commands.md:52` (drop "In the CLI", add `run`, add the `/stop` sentence; lines 271/317 consistent); `website/docs` config reference gains `display.stop_releases_queue`. NEW Obsidian `AI/Hermes Agent/Hermes Harness — /queue, /steer, /stop Semantics.md`: slot+overflow diagram; index numbering; full verb+alias table; the "verb + prose = prompt" rule with examples and the `add` escape; exactly what `/stop`, `/queue run`, `/new`, `/reset`, `/resume`, `/queue clear` do to the queue; the drop inventory; restart spool; cap 32; `/status` count; Slack `!` prefix; the knob; link to spec + PRs. `undo-redo-stop-command-honesty` gains **mode 9**; `hermes-slash-command-authoring` PITFALL 3 gains the subcommand corollary. mem0 conclusion with final semantics.
  - *Verify with:* Obsidian note exists, every verb in it has a matching passing test name cited; `slash-commands.md` no longer says "In the CLI".

- **Phase 5 — Land.** Fork PR (deploys) + upstream PR (clean, off `origin/main`). PITFALL 2: expect the two `commands.py` conflicts if the CommandDef line changes (it shouldn't — args_hint may). Gateway restart is gated: ask Ace, record approval, batch.

## 7. Security / Ops / Observability

- No new trust boundary: all verbs act on the caller's own session key (same scope as `/stop`). No cross-session index.
- Log at INFO: `queue_mgmt op=<kind> session=<key> depth_before=<n> depth_after=<m>`.
- Rollback: revert the fork PR. Behaviour returns to today's (verbs mis-parse as prompts; `/stop` drops the slot item). Partial rollback of only the `/stop` hold: set `display.stop_releases_queue: true` (D17) at the next restart. Spool: a rolled-back (old) binary reads `queue_held` records as ordinary restart follow-ups: fresh ones replay as TURNS (they run), and ones older than `MAX_REPLAY_AGE_S` are renamed `*.stale` (kept on disk, not replayed). No data is deleted.

## 8. Risks

| Risk | Mitigation |
|---|---|
| A prompt that starts with a verb word gets eaten (`/queue clear the cache`) | CLI rule: exact token + valid arity only; `clear the cache` has extra tokens → `add`. `add` is the escape hatch. Documented. |
| Breaking the FIFO promotion path by rewriting slot/overflow | `rewrite_active` is the only active-store writer (slot then overflow, in place); see I2 proofs |
| Discord hand-registration drops args (mode 7 repeat) | Phase 2 Discord E2E before merge |
| Changing `/stop` surprises a workflow that relied on orphan survival | Nobody can have relied on it knowingly (undocumented, depth-dependent). Reply now states it. |

## 9. Open Questions (need Ace's ruling)

**Q1 — RULED → D8.** **Q2 — RULED → D5.** **Q2b — RULED → D7.**

**Q3 — RULED by default (D9):** spec of record = repo `docs/PRD-gateway-queue-mgmt.md`, landed in the Phase 1 PR; this file is the draft.

None open.

## 10. Acceptance Criteria

- [ ] On Telegram, `/queue list` shows numbered pending items with `[you]`/`[system]` tags; empty → "Queue is empty." Evidence: Phase 2 E2E transcript.
- [ ] `/queue rm N` · `edit N <p>` · `move A B` · `clear` · `add <p>` + all CLI aliases behave identically on busy and idle sessions. Evidence: `test_gateway_queue_mgmt.py` + E2E.
- [ ] `/queue <verb + prose>` is still a prompt. Evidence: `test_queue_verbs.py`.
- [ ] `/stop` with 3 queued → all 3 in `held_queue`; reply "3 queued, held (`/queue run` …)"; nothing runs until told, including after the stopped turn unwinds and after a plain message. Evidence: I12 real-turn test + Phase 3 E2E.
- [ ] Post-turn drain skips when stale: `PHASE=stale_drain_skipped` logged; RED on unmodified fork/main. Evidence: I12 test run on both trees.
- [ ] `/queue run` idle → drains FIFO as separate turns; busy → interrupts once then drains; fresh generation; 0 `Rescued orphaned` lines. Evidence: `test_busy_queue_run_releases_each_item_once_in_order` + E2E.
- [ ] `display.stop_releases_queue: true` makes `/stop` behave as `/queue run`. Evidence: `test_stop_releases_queue_knob_is_true_only`.
- [ ] `/new` `/reset` `/resume` with N queued say "dropped N queued"; 33rd `/queue` says cap. Evidence: tests.
- [ ] `tests/hermes_cli/test_commands.py` + `test_gateway_command_handler_parity.py` green (no Slack eviction, no orphan).
- [ ] Docs: slash-commands.md updated, Obsidian note created, skill mode 9 added. Evidence: file paths + git SHA.


## 11. Field report
- 2026-10-12 10:31, Ace on Telegram with Apollo: `/queue list` "not working". Expected given §0: the gateway has no verb parser; idle path sent the literal `list` as a prompt (or queued it if Apollo was busy). This is Phase 2's exact deliverable, not a new spec.
- 2026-10-12 ~11:00, Ace (voice): keep D5; prefers a VERB to release the queue (idle or busy, interrupting if busy); `/stop` should NOT auto-release but should remind "3 queued"; auto-release could be a config parameter. → D11/D12/D13; D7 superseded.
- 2026-10-12 ~20:00 Momus pass 1 BLOCK (B1 slot-is-a-trigger, B2 stale-drain-consumes); both grounded true; #814 found reverted by #1163. Folded as D14-D19, I5b/I6b/I12/I13, Phase 3 rewritten with a real-turn harness.
- 2026-10-12 ~21:30 Momus pass 2 BLOCK (B3 verbs blind to held, B4 dispatch-from-guard) — both already the built shape in the worktree; spec re-specified to it (D20, D16/I9, I14). RC1 auto-reset → D21; RC5 spool → D22; RC3 dismissed with grep evidence.
- 2026-10-12 ~23:00 BUILT. Evidence: `test_queue_stale_drain_real_turn.py::test_stale_turn_drain_consumes_nothing` RED on fork/main 32546878c9 ("stale turn consumed the queue head"), GREEN on branch; 41 new gateway unit tests + 2 spool round-trip + 3 CLI `run` + parser table; neighbor sweep (78 gateway files + test_web_server) branch-vs-base: 93 failures identical on both trees (harness HOME artifact), 2 branch-only (funnel test feeding a non-list state) fixed by making every core-path hook fail open. Mutation check: routing held rewrites through the slot is killed by `test_mutate_held_keeps_rest_held_and_never_arms_the_slot`.
- Grounding corrections during build: (1) restart: `_spool_adapter_pending_for_restart` already spools slot + overflow to the replaying restart spool (inventory #6 stands); D22 extends it to held with `reason: queue_held`. (2) Knob lives at `display.stop_releases_queue` beside `display.busy_input_mode`.
- OUT OF SCOPE, observed: `_spool_adapter_pending_for_restart` spools overflow items WITHOUT removing them, so the later `flush_overflow_to_file` may also write them as transcript rows (possible double delivery: replayed turn + history row). Needs its own card (Apollo dispatch).
- 2026-10-13 ~00:30 Momus pass 3 BLOCK (B4' proof + RC1-RC5, "no new design-class blocker", next pass should be a short confirmation). All folded; I14 end-to-end test added and mutation-checked.
- 2026-10-13 ~01:30 Momus pass 4 APPROVE-WITH-CHANGES (no blocker; "no further Momus pass is warranted"). RC-A citations corrected to real test names + plain-message E2E added; RC-B fixed (D23) with idle-orphan tests; /stop imports guarded at all sites; flake policy D24 (5/5 measured). Remaining assurance = live E2E after restart.
