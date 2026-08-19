# Desktop/TUI Session Auto-Resume After Backend Restart — SPEC v0.1

**Status:** DRAFT — design only, no code. Author: Apollo. Date: 2026-07-06.
**Origin:** Ace asked why a safe-restart auto-continues a Discord conversation but NOT a
Desktop-app conversation (MBP→Studio remote mode), and whether it can be made to.

---

## 0. The problem, stated precisely

When the Hermes backend restarts while Ace is mid-conversation:

- **Discord/Telegram (messaging gateway, `gateway/run.py`):** the interrupted session is
  marked `resume_pending` with a recognized `resume_reason`; on startup,
  `_schedule_resume_pending_sessions()` re-synthesizes the next turn through the platform
  adapter, so the agent **auto-continues** with a one-line handoff. Ace does nothing.
- **Desktop app (dashboard backend `tui_gateway`, port 9119):** the session is NOT in the
  messaging `sessions.json`, has no `origin` (platform+chat), and no messaging adapter to
  re-prompt through. The safe-restart watcher correctly **refuses to guess** (an earlier
  version misrouted the handoff to Ace's Telegram DM; that was fixed to a clean no-op with
  `no_origin_chat: true`). So a desktop-initiated restart **cannot auto-wake the agent** —
  Ace has to send a message to continue.

**This asymmetry is by-design today, not a bug** — but it's a real UX gap, and it's
buildable. This spec designs the fix.

## 1. Why the Discord mechanism doesn't transfer directly

The two surfaces are **different subsystems**:

| | Messaging gateway (`gateway/run.py`) | Dashboard backend (`tui_gateway/server.py`) |
|---|---|---|
| Session key | `agent:main:discord:...` (in `sessions.json`) | bare id `20260701_...` (NOT in `sessions.json`) |
| Resume model | **server-initiated**: startup re-prompts via adapter | **client-initiated**: `session.resume` RPC reattaches the WS transport on reconnect |
| Origin | `SessionEntry.origin` = platform + chat | none |
| Re-prompt path | `_schedule_resume_pending_sessions` → adapter | none — client just reconnects to an idle session |

So `session.resume` already exists in tui_gateway (server.py:219) but it only **reattaches a
transport** — it does not proactively continue an interrupted turn. The missing piece is a
**server-initiated continuation** for tui_gateway sessions, analogous to Discord's, but
delivered over the WS transport when the desktop client reconnects rather than pushed through
a messaging adapter.

## 2. Design options (with the recommendation)

### Option A — Synthetic `sessions.json` entry (the "make it look like Discord" approach)
Give the desktop session a synthetic messaging-style entry so
`_schedule_resume_pending_sessions` picks it up.
- ❌ **Rejected.** The desktop session has no messaging `origin` and no adapter; the Discord
  path hard-requires both (`entry.origin is None` → filtered; `adapter is None` → skipped).
  Faking an origin re-introduces exactly the misroute bug we fixed (handoff shoved at a
  Telegram/Discord channel). Wrong subsystem.

### Option B — tui_gateway-native reconnect-continuation (RECOMMENDED)
Mark the interrupted desktop turn `resume_pending` **in the tui_gateway/state.db layer**, and
when the desktop client reconnects (`session.resume`), the backend **surfaces the pending
continuation over the WS transport** — surface-and-wait, exactly mirroring the messaging
side's "restart_interrupted" behavior, but delivered client-side on reconnect instead of
adapter-pushed at boot.
- ✅ Uses the transport the desktop actually has (WS), no fake origin, no messaging adapter.
- ✅ Reuses the existing `session.resume` reattach seam as the delivery trigger.
- ✅ No cross-channel misroute risk — the continuation only ever goes to the reconnecting
  client that owns that session.

### Option C — do nothing; document the manual step
Keep the honest refusal; Ace sends a message to continue.
- The current state. Fine as a fallback, but Ace explicitly wants the auto-continue.

**Recommendation: Option B.**

## 3. Option B — mechanism sketch (v0.2, pass-1 blockers folded)

> **Pass-1 verdict: BLOCK** (Opus, 2026-07-06). Three real blockers — all folded below. The
> core correction: `tui_gateway` does NOT share the messaging gateway's resume primitives, so
> every "mirror/reuse" in v0.1 is actually **net-new design in a second subsystem**. v0.2
> replaces "reuse the seam" with explicit tui_gateway-native design and pins the two ordering
> hazards the reviewer found.

### 3.0 Primitive-parity correction (folds B3)
The messaging primitives v0.1 leaned on live ONLY in `gateway/run.py` and have NO tui_gateway
equivalent: `_session_initiated_restart` (run.py:3062), `_schedule_resume_pending_sessions`
(run.py:6930), `_AUTO_RESUME_REASONS` (run.py:6791), `_running_agents` slot-claim. tui_gateway's
own primitives are different: running-guard is `session.get("running")`, concurrency is
`_session_resume_lock` (server.py:660) with documented lock order `resume_lock → sessions_lock`
(#39591), and teardown funnels through `_close_session_by_id` / `_close_sessions_for_transport`
(server.py:627/686). **Design decision D-A:** rather than re-implement four primitives in a
second place (doubling the loop-gate blast radius), extract the **reason-allow-list + the
self-restart signal** into a shared module both subsystems import — so the loop-gate exists
once. (Review this vs. the narrow-waist rule; if extraction is too invasive, the fallback is a
tui_gateway-local reason set with a shared *constant* for the excluded reasons.)

### 3.1 The desktop self-restart discriminator (folds B1 — THE blocker) — v0.3 FAIL-CLOSED
tui_gateway has no signal that "a desktop turn's own tool fired the restart." This must be
**built, not mirrored** — and it must **FAIL CLOSED** (pass-2 B1: a string-matcher that
misses is fail-OPEN → treats a self-restart as external → auto-continues → re-fires → loop).
Design:
- **Signal, not string-sniff.** The messaging reference sets `_session_initiated_restart[key]
  = True` at fixed call sites (run.py:6752, 17430), NOT by matching a shell string. Mirror the
  *mechanism*: tui_gateway sets a per-session `_desktop_session_initiated_restart` flag at the
  point the turn's tool-execution path actually invokes a restart, as a positive signal.
- **🔴 FAIL-CLOSED DEFAULT (mandatory, the v0.2 gap):** the auto-continue-eligible reason is
  assigned ONLY when the restart is **positively known** to be external/operator-initiated.
  **Any uncertainty — unmatched command, unknown cause, missing signal, wrapper/alias, the
  dashboard-kickstart HTTP path — defaults to `desktop_restart_consumed` (EXCLUDED, no
  auto-continue).** A miss must NEVER produce an auto-continue-eligible reason. This inverts
  v0.2's implicit denylist: the allow-to-continue set is the *narrow, positively-proven* case.
- **Reason split** (persisted on the marker): `desktop_restart_consumed` (self OR unknown →
  EXCLUDED, passive "restored" note only) · `desktop_restart_consumed_interrupted` (self +
  mid-turn at drain → surface-and-wait, never replays tools) · `external_restart_interrupted`
  (POSITIVELY external → the ONLY auto-continue-eligible reason).
- **AC-7 (see §6) proves the fail-closed property adversarially** — every ambiguous path
  resolves to a non-continuing reason.

### 3.2 Drain-vs-reap ordering (folds B2/B3/B4 — GROUND-TRUTHED) — v0.3
Ground-truth against real source (2026-07-06):
- **B4 answer:** `close_on_disconnect` **defaults to `False`** (server.py:5258/5695/5768,
  `params.get("close_on_disconnect", False)`) — it is opt-in per `session.new`. **Whether the
  desktop app passes it True is the remaining one-line check** (grep the desktop client's
  `session.new` params); the *backend* default is False, so a plain dashboard session is NOT
  immediately reaped — it goes to the grace-windowed orphan reaper (`_schedule_ws_orphan_reap`).
- **B3 answer:** tui_gateway DOES have a server-side shutdown hook — **`_shutdown_sessions()`
  registered via `atexit` (server.py:856)**, which calls `_close_session_by_id(sid,
  end_reason="tui_shutdown")` for every session on process exit. So there IS an execution
  window before the process dies. **D-B (revised):** inject the marker-write into
  `_shutdown_sessions`, BEFORE each `_close_session_by_id`, for every session with an
  in-flight turn — stamping the fail-closed reason from §3.1. This is the concrete drain hook
  Option B depends on; it exists.
- **Honest limit:** `atexit` does NOT run on `SIGKILL` / hard crash. A `kill -9`'d backend
  writes no marker (same bounded-loss class as messaging's SIGKILL limit). Acceptable; state
  it. For a graceful restart (the common case, and the one tonight's kickstart uses), the
  `atexit` window is real.

### 3.3 Deliver on reconnect (lock-ordering pinned)
In `session.resume`, after transport reattach: check for a fresh marker, and if present +
fresh + not `session.get("running")` + reason is auto-continue-eligible → synthesize the
continuation. **Launch the continuation OUTSIDE `_session_resume_lock`** (after reattach) —
synthesizing a turn while holding the resume lock races the grace-windowed orphan reaper
(`_schedule_ws_orphan_reap`) and reintroduces the #39591 hazard. Clear the marker atomically
on delivery.

### 3.4 Idempotency, freshness, GC
- Single-flight: a delivery token + the `session.get("running")` guard so a flapping reconnect
  can't double-fire.
- Freshness: skip (or downgrade to a passive "resume?" chip) if older than the window (Q3).
- **Marker TTL/GC:** if the client never reconnects (laptop closed for days), markers must
  self-expire so state.db doesn't accrete — a TTL sweep on the marker store, same self-pruning
  discipline as the safe-restart `.done` markers.

## 4. Open questions for review (BLOCK-worthy)

- **Q1 — Where does the marker live?** state.db (survives a full process restart, consistent
  with messaging durability) vs. a tui_gateway JSON store. state.db is the safer default
  (survives crash + restart) but needs a schema touch — weigh against the recently-reverted
  denorm caution.
- **Q2 — Restart vs. crash vs. clean disconnect.** Only a *restart-interrupted* turn should
  auto-continue. A user who deliberately closes the app mid-turn should NOT be surprise-
  continued on next open. Need a clear discriminator (was the turn interrupted by a
  backend-initiated drain, vs. a client-side disconnect?).
- **Q3 — The freshness window.** Desktop reconnects can be minutes-to-hours later (laptop
  closed). Messaging uses `_auto_continue_freshness_window()`. A stale continuation is worse
  on desktop (Ace may have moved on). Probably a *shorter* window + a visible "resume?" chip
  rather than silent auto-continue for anything beyond N minutes.
- **Q4 — Does turn-isolation (dashboard.turn_isolation, currently OFF) change the seam?** The
  process-isolation work orphaned server-side turns on client `kill -9`; interaction with
  this needs checking.
- **Q5 — Multi-client.** If two desktop clients attach to the same session, which one gets
  the continuation? (Messaging has exactly one origin; desktop can have N transports.)

## 4b. 🔴 LOAD-BEARING SAFETY GATE — the self-restart loop (do NOT design around this)

**This is the single most dangerous item and it sank an earlier design.** The messaging-gateway
resume note deliberately says "report restored, ask what's next, do NOT re-execute unfinished
work" — that wording is NOT timidity, it is a **self-restart-loop backstop** (hermes-agent
commits `5191c1c2c`/#45230, `75ed07ace`/#49201/#49243). Mechanism: when a turn's OWN tool
fires the restart (the agent runs `safe-restart.py` / `docker restart` / `systemctl restart`
/ a dashboard kickstart as its last action), telling that session to **"continue"** on resume
**re-issues the exact same restart → infinite loop.** Tonight's ~12-restart thrash is a
live rhyme of this class.

**Consequence for THIS spec:** Option B must **auto-continue ONLY when the interrupt was
NOT self-initiated.** The messaging side gates on `resume_reason` — `restart_consumed`
(clean self-restart) is excluded from `_AUTO_RESUME_REASONS`; `restart_consumed_interrupted`
(self-restart that was also mid-turn at drain) auto-resumes **surface-and-wait only, never
replaying tool calls** (PR #142). The desktop path MUST carry the same discriminator:
- If the interrupted turn **initiated** the backend restart → mark a *clean surface-and-wait*
  reason that is **excluded** from auto-continue (or surfaced as a passive "restored" note,
  never a re-fired turn).
- Only a turn interrupted by an **external/operator** restart (not its own tool) may
  auto-continue.
- **Never re-execute the interrupted tool sequence** — surface where we stopped and wait,
  matching Option B's "preserve-and-prompt" (Ace's 2026-06-30 decision on the messaging side).

**Review instruction:** treat any Option-B mechanism that can re-fire a self-initiated
restart as a hard BLOCK. The reference implementation to mirror is the messaging side's
`_session_initiated_restart` signal + the `restart_consumed` / `restart_consumed_interrupted`
reason split — read the *commit intent* (`git log -S`), not just the resume-site code (five
prior Opus passes on the messaging spec all MISSED this by reading code, not intent).

## 5. Explicitly NOT in scope
- No change to the messaging-gateway auto-resume (it works).
- No synthetic `sessions.json` entries (Option A rejected).
- No live deploy until spec is reviewed + built + proven on a real reconnect E2E.

## 5b. Pass-2 blockers folded (v0.3) — ground-truthed against real source

> **Pass-2 verdict: BLOCK** (Opus). B1/B2/B3 folds from pass-1 confirmed held + lock-ordering
> clean. Four new blockers, all resolved below with real `server.py` line citations.

- **P2-B1 (fail-closed detector) — RESOLVED.** The self-restart detector must **fail CLOSED**:
  if the string-matcher can't positively classify the interrupt as external, treat it as
  **self-initiated** (→ excluded from auto-continue, passive "restored" note only). An
  unknown/ambiguous interrupt NEVER auto-continues. Rationale: a false-negative on "external"
  is the loop; a false-positive costs only a manual "continue." This inverts v0.2's implicit
  default. (§6 AC-7 now tests exactly this.)
- **P2-B2 (dangling AC-7) — RESOLVED.** v0.2 referenced "AC-7" that didn't exist. AC-7 is now
  written (§6): adversarial proof the excluded/self-initiated reason can never reach the
  continuation launch, incl. the fail-closed unknown-reason case.
- **P2-B3 (D-B write point) — GROUND-TRUTHED.** tui_gateway's server-side shutdown hook is
  **`_shutdown_sessions()` registered via `atexit` (server.py:856)**, which closes every
  session via `_close_session_by_id(..., end_reason="tui_shutdown")` (server.py:723-727). This
  IS the pre-close window D-B needs: **the drain-time marker-write must be injected at the top
  of `_shutdown_sessions`, iterating in-flight desktop turns and persisting their resume
  markers BEFORE the close loop.** Caveat: `atexit` is best-effort (a SIGKILL/hard-crash skips
  it) — so the marker store MUST be durable (state.db), and a turn killed by SIGKILL simply
  doesn't get a marker (acceptable: no auto-continue, same as today). A graceful
  restart/`kickstart -k` runs atexit, so the normal path is covered.
- **P2-B4 (is dashboard close_on_disconnect?) — GROUND-TRUTHED.** `close_on_disconnect`
  **defaults to `False`** (`params.get("close_on_disconnect", False)` at server.py:5258/5695/
  5768) — it is opt-in per `session.start`/`session.init`. **Build-phase action:** confirm what
  the desktop app actually passes (grep `apps/desktop` for `close_on_disconnect`). If the
  desktop does NOT set it (likely), dashboard sessions take the **grace-windowed orphan-reaper**
  path (`_schedule_ws_orphan_reap`), not immediate reap — which is softer and gives the D-B
  atexit hook time to run. If the desktop DOES set it, D-B's before-close ordering in
  `_shutdown_sessions` is mandatory. Either way D-B (mark in `_shutdown_sessions` before the
  close loop) is correct; the reap path only changes urgency.

## 6. Acceptance criteria (v0.3)
- **AC-1** A backend restart mid-desktop-turn → on client reconnect, the agent auto-continues
  with the handoff, within the freshness window, exactly once.
- **AC-2** A *client-initiated* disconnect mid-turn does NOT auto-continue on next open
  (Q2 discriminator).
- **AC-3** A stale (> window) marker does NOT silently auto-continue (surfaces a resume
  affordance instead, or is dropped — decide in review).
- **AC-4** No double-continuation under reconnect flapping (idempotency).
- **AC-5** The continuation is delivered ONLY to the session's own reconnecting client, never
  cross-routed to a messaging channel (the misroute regression stays fixed).
- **AC-6** No messaging-gateway behavior change (regression-guard the Discord path).
- **AC-7** (self-restart-loop gate) A turn whose OWN tool fired the restart does NOT
  auto-continue — marked `desktop_restart_consumed`, delivered as a passive "restored" note,
  never a re-fired turn. **AND** an interrupt the detector cannot positively classify as
  external is treated as self-initiated (fail-closed, P2-B1). Adversarial test: assert the
  excluded reason set can never reach the continuation-launch call site.
- **AC-8 → SUPERSEDED by AC-8(corrected) in §8.1** (SIGTERM path, not atexit). See §8.1.
- **AC-9 → SUPERSEDED by AC-9(corrected) in §8.1** (SIGKILL, not "atexit skipped"). See §8.1.
- **AC-10** Marker TTL/GC: a marker whose client never reconnects self-expires within its TTL
  (no state.db accretion). Owner: state.db sweep on dashboard boot (see §9 RC-5).

---

**Status:** v0.3 — 2 Opus review passes (BLOCK→BLOCK, all blockers folded with real source
citations). NOT yet a clean APPROVE — needs ≥1 more pass to verify the v0.3 folds hold, plus
the Q1-Q5 open questions decided. **No code until clean APPROVE.**

**Next step:** pass 3 (verify v0.3 folds), decide Q1 (marker store = state.db, leaning),
Q2/Q3 (freshness window + discriminator), Q4 (turn-isolation interaction), Q5 (multi-client),
then build behind a dormant flag with the AC-7/AC-8 tests as the load-bearing gates.

## 7. Pass-3 blockers RESOLVED (v0.4) — ground-truthed against the working messaging path

Pass 3 (BLOCK) found two deep holes + carried decisions. All resolved below by **grounding
against the messaging gateway, which already solves both** (it is the proven reference, not a
hypothetical).

### 7.1 P3-B1 RESOLVED — the messaging side does NOT use a positive external signal; it uses a RELIABLE self-signal + a loop circuit-breaker
Ground truth (`gateway/run.py:5629-5642`, `_reason_for_shutdown`): the messaging path is
**exactly** "self-flag set ⇒ non-continuing reason; else ⇒ auto-resume reason." It is
absence-based — and it is SAFE not because absence proves "external," but because:
1. **The self-flag is set by TWO independent mechanisms** so it's not missed: F1
   `request_restart` (the tool that fires the restart tags the session) + C1 progress-callback
   `_command_invokes_safe_restart` (matches the actual command EXECUTION at run.py:6752). Either
   one sets `_session_initiated_restart[key]=True`.
3. **F1/F2 circuit-breaker backstop:** even if the self-flag is somehow missed, the
   restart→resume→restart loop is **bounded** (replay-mark suspend, threshold 3 within 300s) —
   so a missed signal costs at most a couple of cycles, never an infinite loop.

**Decision D-C:** the desktop path adopts the SAME model, NOT a new positive-external
breadcrumb (which pass-3 asked for but the messaging side proves is unnecessary and which
would be net-new surface):
- Reliable self-signal: a tui_gateway `_desktop_session_initiated_restart[sid]` set by (a) the
  turn-lifecycle command matcher (mirror `_command_invokes_safe_restart`) AND (b) an explicit
  tag if a desktop turn calls the dashboard-restart RPC/kickstart path.
- **A tui_gateway loop circuit-breaker** (mirror F2): a per-session replay-mark that suspends
  auto-continue after N=3 continue→restart cycles in 300s. This is the REAL backstop that makes
  absence-based classification safe, and it's the load-bearing safety item (AC-7 + new AC-11).
- Fail-closed stays for the AMBIGUOUS case only (matcher error / signal-write failure → treat
  as self-initiated), but the NORMAL external restart is correctly auto-continue via absence +
  the breaker — so AC-1 CAN fire (resolves the pass-3 "feature is inert" horn of the dilemma).

### 7.2 P3-B2 RESOLVED — add a SIGTERM handler to the dashboard (atexit alone is insufficient, confirmed)
Ground truth: `grep` for `signal`/`SIGTERM`/`add_signal_handler` in `tui_gateway/server.py`
returns **NOTHING** — the dashboard has only `atexit` (server.py:856). The dashboard is a
**standalone process** (`hermes dashboard`, its own launchd job `ai.hermes.dashboard`,
confirmed in the plist), so `kickstart -k`/SIGTERM hits it directly and CPython does NOT run
atexit on a bare SIGTERM. **Decision D-D:** add a SIGTERM/SIGINT handler to the dashboard's
async entrypoint that converts the signal into a graceful shutdown which runs the marker-write
BEFORE process exit — mirror the messaging gateway's proven pattern at `gateway/run.py:20030`
(incl. the Windows `add_signal_handler`→`NotImplementedError` fallback). The marker-write moves
from "top of `_shutdown_sessions` (atexit)" to "the SIGTERM-driven graceful-shutdown path,"
with `_shutdown_sessions`/atexit kept as a belt-and-suspenders fallback for the clean-exit case.
AC-8 tests the **SIGTERM** path (send SIGTERM to a live dashboard with an in-flight desktop
turn; assert the marker exists), not the atexit path.

### 7.3 Carried decisions — now DECIDED
- **Q2 (drain-interrupt vs. user app-close) → DECIDED.** Discriminator = the SIGTERM handler
  (D-D). A marker is written ONLY on the backend-initiated SIGTERM-shutdown path. A user
  closing the app is a transport disconnect (no SIGTERM to the backend) → NO marker → NO
  auto-continue. This makes AC-2 testable: app-close = transport drop only, assert no marker.
- **Q5 (which of N transports) → DECIDED.** The continuation is delivered on the transport that
  wins the `session.resume` for that session id (the reconnecting client). If a second client
  is already attached and running the session, the marker is already cleared / `running` guard
  blocks it → no double-delivery. Single-owner-at-resume-time; AC-5 tests exactly one client
  gets it.
- **Q3 (freshness window) → DECIDED.** Reuse the messaging `_auto_continue_freshness_window()`
  value for consistency; beyond it, downgrade to a passive "resume?" affordance (no silent
  auto-fire). Configurable via the same knob.
- **D-A (shared module vs local) → DECIDED: LOCAL.** Per pass-2 guidance and the narrow-waist
  rule, use a tui_gateway-LOCAL reason set + a shared *constant* for the excluded-reason
  strings (a tiny `constants` import, NOT extracting the loop-gate logic). This keeps the
  proven Discord path untouched (AC-6 blast radius = zero).

### 7.4 New ACs from this pass
- **AC-11** (loop circuit-breaker) After N=3 continue→self-restart cycles within 300s for one
  desktop session, auto-continue SUSPENDS (mirrors messaging F2). Mutation-proven: remove the
  breaker → a synthetic self-restart loop runs unbounded (test goes RED).
- **AC-12** (SIGTERM marker) Sending SIGTERM to a live dashboard with an in-flight desktop turn
  writes the durable marker before exit; a plain transport-disconnect (app close, no SIGTERM)
  does NOT.

**STATUS: v0.4 — pass-3 blockers resolved against the proven messaging path.**

## 8. Pass-4 blockers RESOLVED (v0.5) — ground-truthed the two feasibility seams

Pass 4 (BLOCK) sharpened four items; all resolved with real `tui_gateway/server.py` citations.

- **P4-B1 (breaker durability) — RESOLVED, THE key catch.** The circuit-breaker is the
  load-bearing safety mechanism and the loop **spans process restarts** (each cycle is a fresh
  dashboard process), so an in-memory counter resets to 0 and NEVER trips → fail-open. **Fix:**
  the breaker counter MUST be durable. Ground truth: tui_gateway already uses `SessionDB`
  (state.db) at server.py:866-869 — so **both the resume marker AND the breaker replay-count
  persist in state.db keyed by session id** (this also DECIDES Q1: marker store = state.db).
  The messaging reference rides its F2 mark on the persisted `SessionEntry`; the desktop rides
  it on a persisted state.db row. **AC-11 is re-specified to drive MULTIPLE real restart cycles
  with persisted breaker state** (spawn → self-restart → respawn ×3), not an in-process loop —
  a single-process mutation test would falsely pass against an inert breaker.
- **P4-B2 (is the dual self-signal buildable?) — RESOLVED, seam CONFIRMED.** Pass 4 doubted the
  command-execution seam exists. It DOES: tui_gateway wires `_on_tool_start` (server.py:3615)
  and `_on_tool_complete` (3642) into the agent as `tool_start_callback`/`tool_complete_callback`
  (3883-3889). So the desktop CAN observe the agent's own tool invocations and match a
  restart-invoking command at execution time (mirror `_command_invokes_safe_restart`) — signal
  (a). Signal (b) is the explicit tag on the dashboard-restart RPC path. **Dual-redundancy is
  buildable, not asserted.** (Build note: hook the matcher in `_on_tool_start`, excluding
  read-only inspection of the script like the messaging matcher does.)
- **P4-B3 (stale AC-8 says atexit) — RESOLVED.** AC-8 is corrected to test the **SIGTERM**
  graceful-shutdown path (not atexit), consistent with §7.2/D-D. AC-9's parenthetical is
  corrected: the marker-less case is **SIGKILL** (which no handler can catch), not "atexit
  skipped."
- **P4-B4 (Q4 turn_isolation × running-detection) — RESOLVED as a stated interaction.** The
  marker-write enumerates in-flight turns via `session.get("running")` (same predicate as
  `_ws_session_is_orphaned`, server.py:652). When `dashboard.turn_isolation` is ON, a turn runs
  in a compute-host subprocess; the SIGTERM handler MUST enumerate in-flight turns via the
  **same source of truth the isolation path uses to track a running turn** (the compute-host
  frame registry, `_compute_host_turn_frame`/`_on_compute_host_turn_done` at server.py:1037/1099),
  not only the in-process `running` flag — else isolated turns are missed. **Decision D-E:** the
  in-flight enumeration for the marker-write reads BOTH the in-process `running` set AND the
  compute-host frame registry, unioned. AC-13 added.

### 8.1 Corrected / added ACs (supersede the v0.3 wording where noted)
- **AC-8 (corrected)** The drain marker for an in-flight desktop turn is written on the
  **SIGTERM** graceful-shutdown path before process exit — test sends real SIGTERM to a live
  dashboard with an in-flight turn and asserts the state.db marker exists. (Supersedes the v0.3
  atexit wording.)
- **AC-9 (corrected)** A **SIGKILL**'d dashboard (no handler can run) leaves no marker and does
  not auto-continue — no crash/partial marker.
- **AC-11 (re-specified)** Breaker durability: across 3 real spawn→self-restart→respawn cycles
  in 300s (persisted state.db counter), the 4th auto-continue SUSPENDS. Mutation-proven by
  making the counter in-memory → the loop runs unbounded across restarts (RED).
- **AC-13 (new)** With `dashboard.turn_isolation` ON, a compute-host-isolated in-flight turn
  still gets a marker on SIGTERM (enumeration unions the frame registry, D-E) — test with
  isolation ON asserts the isolated turn is not missed.

**STATUS: v0.5 — pass-4 blockers resolved; the two feasibility seams (durable breaker in
state.db, tool-execution observation via `_on_tool_start`) are ground-truthed to exist.**

## 9. Pass-5 = APPROVE-WITH-CHANGES → folded (v0.6). Architecture APPROVED; edges tightened.

Pass 5 cleared BLOCK (4 passes of BLOCK → AWC). Lens notes: architecture sound, security/
identity clean, D-A LOCAL correct. Six required changes, all folded:

- **RC-1 (B1 detached-turn ordering) — the sharp edge.** The marker must mark the RIGHT turns.
  A turn whose client already detached (app closed) THEN a restart happens must NOT
  auto-continue. **Fix:** the SIGTERM marker-write requires a **live transport at drain time** —
  a detached session gets no marker. (Pass-6 B3: reduced to the strictly-groundable
  live-transport predicate — `transport is not _detached_ws_transport` — and DROPPED the
  "idle-since-detach < 60s" fallback, because the orphan reaper uses a `threading.Timer`, not a
  stored detach timestamp, so there is no clock to read; if a timestamp source is added later,
  the idle-fallback can return.) AC-2 reworded: app-closed-then-restarted turn does NOT
  auto-continue (it has no live transport at drain).
- **RC-2 — AC-8/AC-9 wording** already corrected in §8.1; fold AC-8 into the SIGTERM assertion
  (dedupe with AC-12), AC-9 = SIGKILL. (Housekeeping; done in §8.1, remove the stale §6 AC-8/9.)
- **RC-3 (marker idempotency) — D-F.** The marker-write must be **single-source OR idempotent +
  reason-monotonic** across the SIGTERM path (D-D) and the atexit fallback (both can fire on a
  clean exit). **🔴 Race direction is PINNED LOOP-SAFE (pass-6 B1): the non-continuing /
  self-initiated reason ALWAYS wins any SIGTERM↔atexit write race — monotonic strictly TOWARD
  "don't-continue," NEVER toward continuation.** Concretely: if one path stamps
  `external_restart_interrupted` (continue) and the other stamps `desktop_restart_consumed`
  (self/don't-continue), the **consumed reason wins** (a self-restart must never auto-continue —
  §4b). Never "preserve interrupted over clean." Key on session id.
- **RC-4 (state.db-unavailable = fail-CLOSED) — D-G + AC-14.** Ground truth: tui_gateway
  "continues without state.db features" when `SessionDB()` throws (server.py:874). **Decision:**
  no store ⇒ no marker AND no breaker ⇒ **the whole auto-continue feature disables** (never
  continuation-live-but-breaker-dead, which pass-4's B1 fear in disguise). AC-14: with state.db
  unavailable, no auto-continue occurs and no crash.
- **RC-5 (AC-10 GC owner) — named.** The marker/breaker TTL sweep runs as a **state.db sweep on
  dashboard boot** (same place the v3 backfill/open-path maintenance runs), TTL far longer than
  any in-flight restart (mirror safe-restart's 48h `.done` TTL discipline). AC-10 owner = boot sweep.
- **RC-6 (launchd drain budget) — stated.** `ai.hermes.dashboard`'s plist `ExitTimeOut` (and the
  KeepAlive/SIGKILL escalation) MUST give the SIGTERM handler enough wall-clock to enumerate
  in-flight turns (incl. the D-E compute-host union) + write markers before SIGKILL. Build-phase:
  confirm/raise `ExitTimeOut` so AC-9's no-marker stays the crash (SIGKILL) case, not the common
  case. If unset, launchd default is 20s — likely enough for a marker-write, but state it.

### 9.1 Residual (folded as build-phase notes, not blockers)
- **Breaker increment ordering:** the durable breaker increment MUST persist BEFORE the
  continuation launches (the act that can trigger the next self-restart). AC-11's cross-restart
  drive catches it; stated explicitly so the builder doesn't order it after launch.
- **Q3 "resume? chip" is `apps/desktop` UI (cross-cutting):** the stale-marker passive
  affordance is client-side work — flagged as an EXTERNAL DEPENDENCY, not in the backend build.
  The backend delivers a "stale marker present" signal; the desktop renders the chip separately.
- **Multi-client single-flight (Q5 × AC-4):** two clients racing `session.resume` on the same
  marker within the grace window resolve via the `running`-guard + **atomic marker-clear**
  (compare-and-clear) — tie AC-4 to this. One winner, marker cleared atomically.
- **Non-POSIX/Windows:** D-D inherits the messaging Windows `add_signal_handler`→
  `NotImplementedError` marker-file bridge; if the dashboard can't, the feature is **POSIX-only**
  — stated (the fleet dashboard runs on macOS/Linux, so POSIX-only is acceptable v1).

### 9.2 Added ACs
- **AC-2 (reworded)** An app-closed-then-restarted turn (no live transport at drain) does NOT
  auto-continue.
- **AC-14** state.db unavailable ⇒ auto-continue feature fully disabled (no marker, no breaker,
  no continuation), no crash — fail-closed.

**STATUS: v0.6 — APPROVED-WITH-CHANGES folded. Architecture is APPROVED across all lenses.
Remaining items are build-phase notes + one client-side external dependency (Q3 chip). Ready
for a confirming pass 6 (should be clean APPROVE) OR proceed to build behind the dormant flag
with AC-7/AC-8/AC-11/AC-14 as the load-bearing gates — Ace's call, and sequenced AFTER the
perf-win arm.**

---

## FINAL STATUS: APPROVED (pass 7, clean, 2026-07-07)

Convergence: **BLOCK×4 → AWC×2 → APPROVE**. Zero critical blockers remain. Architecture approved
across all lenses (product, architecture, security/identity, DevOps/SRE, testing,
maintainability, config-drift). Key hard problems the review surfaced and the spec now solves:
1. Self-restart loop (§4b) — dual self-signal + **durable** state.db circuit-breaker.
2. Fail-open detector → fail-closed on ambiguity, safe-by-breaker on the normal path.
3. `atexit` doesn't fire on SIGTERM → dedicated SIGTERM handler (D-D).
4. Consent defect — never auto-continue an app-CLOSED-then-restarted turn (live-transport gate, D-F/RC-1).
5. Marker-write race pinned loop-safe (non-continuing reason always wins).

**Load-bearing gates for build:** AC-7 (loop-gate), AC-11 (durable cross-restart breaker,
mutation-proven), AC-2 (consent/detached-turn), AC-8/AC-12 (SIGTERM marker), AC-15 (fail-closed
store). Build behind a dormant flag, POSIX-first, sequenced AFTER the perf-win arm (Ace's call).
Review artifacts: /tmp/desktop-resume-review/pass{1..7}.md.

---

## SHIPPED (2026-07-07) — built, merged, deployed, DORMANT (not yet armed)

Built by kanban worker t_250e3293 against this spec; PR **#229** merged to fork/main
(`d5f137699`) and deployed to the runtime tree. Behind the **default-off** config gate
`dashboard.desktop_auto_resume` — the feature is fully inert until flipped.

**Greptile review:** converged in 2 rounds. One real gap caught + fixed: the AC-10/RC-5 TTL
sweep (`sweep_desktop_auto_resume_state`) was built but never wired — now fires on `_get_db()`
first-open (boot maintenance window), gated + best-effort, mutation-proven.

**Live E2E on shipped runtime code (not mocks):**
- **AC-8/AC-12:** a real SIGTERM to a process with an in-flight desktop turn (flag ON, live
  transport) writes a durable `external_restart_interrupted` marker to state.db before exit. ✓
- **AC-7 (loop safety, the load-bearing gate):** a *self*-initiated restart is EXCLUDED —
  marked `desktop_restart_consumed_interrupted` (surface-and-wait), never the auto-continue
  reason, so it cannot re-fire into a restart loop. ✓
- **AC-11 (durable breaker):** mutation-proven — the breaker replay-count persists across real
  subprocess respawns; breaking the increment turns the test RED.

**Arming (deferred, Ace's call):** flip `dashboard.desktop_auto_resume: true`, restart the
dashboard, then a real reconnect-after-restart E2E. Sequenced after the perf-win arm (done).
