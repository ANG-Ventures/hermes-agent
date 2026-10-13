# PRD — `/queue` follow-up: release ordering, verb-shape parsing, merged release (v1.0)

**Status:** BUILT (tests green; neighbour sweep branch==base on ace-media) · **Owner:** Aegis · **Date:** 2026-10-13 · **Parent spec:** `2026-10-12_gateway-queue-management-spec.md` (PR #1948, live 23:46 on aegis / 23:22 on default)
**Target:** `ANG-Ventures/hermes-agent` fork/main, then upstream `NousResearch/hermes-agent` (both PRs).

## 0. Why (live E2E 2026-10-13 01:43–01:46, Aegis Telegram DM, gateway log)
`/queue a|b|c` during a busy turn → `/stop` → `op=hold moved=3 held=3`, `PHASE=stale_drain_skipped` → `edit 2 b.2` → `rm 1` → `/queue run` → `op=release moved=2 armed=2` → **0.6 s later `Rescued orphaned FIFO overflow event…`** → **c ran before b.2**. Both ran exactly once; order wrong.

Root cause: `restage_held` arms the slot with item 1 and puts item 2 in overflow. The adapter's post-command drain pops item 1 and feeds it to `_handle_message`, whose `_hm_rescue_orphaned_fifo` sees **slot empty + overflow non-empty** and treats item 2 as an orphan: it runs item 2 as THIS turn and parks item 1 behind it. The I14 rig stubbed `_interrupt_and_clear_session` and never threaded a real `_handle_message`, so the seam was unproven (Momus p4 residual: "the Phase 3 live E2E is the evidence that closes this").

## 1. Ace rulings (2026-10-13 01:52 / 02:14, verbatim intent)
- **R1** fix the ordering bug.
- **R2** "if the prompt for a /queue command is more than one verb — multiple words — then it probably shouldn't act on the verb … fall back to a normal prompt." A message is a verb ONLY when it exactly fits that verb's shape. `/queue add` remains the override for the bare-word collision case (`/queue add clear`).
- **R3** "I would prefer that we release them all on the next turn — release together … that could be a config knob … that's what I want to be my default behavior … we could upstream that too." → merged release is the fleet default (config), per-item stays the upstream/code default.

## 2. Decisions
| # | Decision | Why |
|---|---|---|
| D1 | **Drain-dispatched events are never orphans.** `restage_held` (and every release path) marks the armed head `event._queue_released = True` (process-local field, like `_gateway_accepted`). `_hm_rescue_orphaned_fifo` returns early when the incoming event carries that mark. Mark cleared when the rescue check passes it through. | Smallest guard at the one site that misread the state; the rescue still works for genuine orphans (a new inbound event never carries the mark). |
| D2 | **Verb shape is exact.** `rm N`, `move A B`, `list`, `clear`, `run|fire|release|go`, `help` accept NOTHING after their arguments; any extra word → prompt. `edit N <text>` keeps its text (its shape requires text). `add <text>` forces prompt. | R2. Today `rm 3 things` and `move 1 2 now` parsed as verbs with trailing junk. |
| D3 | **Merged release.** New knob `display.queue_release_mode: per_item \| merged` (code default `per_item`, upstream-compatible; fleet config sets `merged`). With `merged`, `/queue run` (idle, busy, and knob-on `/stop`) joins every released USER item into ONE turn: texts joined by a blank line in queue order, media lists concatenated, reply anchor = the first item's. System events (wakes, goal steps) are never merged; they keep their own turn before the merged one. | R3. One turn with all prompts = one answer in one context, which is what Ace asked for. |
| D4 | Merge happens INSIDE `restage_held` (the single writer of the active store): it builds the merged event, arms it as item 1, and the normal drain dispatches it. No new dispatcher. | Keeps I9's single door; the adapter path is unchanged. |
| D5 | Held-store verbs (`list`, `rm`, `edit`, `move`) are unaffected by the mode: you manage items individually right up to `/queue run`. | Merging at release time, never at queue time, keeps the verbs meaningful. |
| D6 | CLI parity: `/queue run` in the CLI under `merged` joins the pending input the same way (one message into `_interrupt_queue`/`_pending_input`). | D8 of the parent spec (CLI parity). |
| D7 | The mode is read at boot (`_load_queue_release_mode`), same shape as `stop_releases_queue`; invalid values fall back to `per_item` with a WARNING. | Config-drift discipline. |

## 3. Invariants / proofs
- **I1** `test_queue_run_real_inbound_order`: real adapter + REAL `_handle_message` route (not a handler stub): `/stop` with 3 held → `/queue run` runs a, b, c in that order, once each. RED on current main (reproduces c-before-b).
- **I2** parser table: `rm 3 things` / `move 1 2 now` / `clear this thing` / `run the backup` / `list the files` / `go home` → prompt; `rm 3` / `move 1 2` / `edit 2 new text` / `clear` / `run` → verb; `add clear` → prompt "clear".
- **I3** merged mode: 3 held → `/queue run` → exactly ONE agent call whose message is `"a\n\nb\n\nc"`; a system wake in the active store runs as its own turn FIRST.
- **I4** per_item mode unchanged: three calls, in order (existing I14 tests stay green).
- **I5** knob tri-state: absent/`""`/`per_item` → per_item; `merged` → merged; `bogus` → per_item + WARNING.
- **I6** CLI merged: one message delivered, joined with the same separator.

## 4. Files
- `gateway/platforms/event.py`: `_queue_released` field.
- `gateway/queue_mgmt.py`: mark in `restage_held`; `merge_user_events()`; mode-aware restage.
- `gateway/run_inbound.py`: rescue early-return on the mark.
- `gateway/run_config_loaders.py` + `gateway/run.py`: `_load_queue_release_mode`, `_queue_release_mode` attr.
- `hermes_cli/config_defaults.py`: `display.queue_release_mode: "per_item"`.
- `hermes_cli/queue_verbs.py`: exact-shape parsing.
- `hermes_cli/cli_loops_mixin.py`: merged `/queue run`.
- `~/.hermes/config.yaml` + `profiles/aegis/config.yaml`: `display.queue_release_mode: merged` (fleet default, Ace R3) — landed as a config edit, not code.
- Docs: website slash-commands + messaging guide; Obsidian note; parent spec changelog.

## 5. Rollback
Revert the PR; or set `display.queue_release_mode: per_item` to get #1948 behaviour without a revert.
