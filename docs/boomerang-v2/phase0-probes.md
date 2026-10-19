# Boomerang v2 — Phase 0 ground-truth probes (P0.1–P0.5)

Card t_6b452366 · 2026-10-18 · base `fork/main` c1ff8826c6 · spec
`~/.hermes/plans/2026-10-18_boomerang-fork-autowake-spec.md` §6 Phase 0.

| Probe | Verdict | One line |
|---|---|---|
| P0.1 served-by on main turns | **PASS** | Already captured on every main-turn wire; 17,537 / 17,542 alr main-turn rows (24 h) carry the seat. |
| P0.2 byte-identical replay reads cache | **PASS** | 502,704 of 502,708 prompt tokens read (ratio 1.0000), pinned seat, 26.7 s after the parent. |
| P0.3 `_session_messages` == sent bytes | **PASS (with named send-path transforms)** | No post-hoc rewrite between turns; the wire differs only by deterministic send-path transforms. Production twin (review fork) reads 98.15 % on its first call. |
| P0.4 TTL window | **PASS (1h arm) / 5m arm NOT REPRODUCIBLE on claude-alr** | 1h parent replayed at 51.3 min: 502,706 / 502,710 read. A ttl-less ("5m") parent was billed as 1h by apx and still read 100 % at 34.2 min. |
| P0.5 snapshot primitive + cancel | **PASS** | Primitive named (below); `delegate_task action=stop` unwound a child mid-`sleep 60` in 2.14 s, `interrupted=true`. |

**P0.2 passed, so the load-bearing premise holds: Phase 2 is not re-scoped.** One finding changes
spec text (P0.4: on claude-alr the wire ttl is 1h no matter what the harness asks for, unless the
seat is in overage) and one sharpens §5.5 (P0.5: nothing locks `_session_messages`; read from inside
the parent's own tool call it is either one round stale or the live list with a dangling tool tail,
so the fork prefix should come from the last sent request).

Probe script: `tools/dev_probes/boomerang_v2_probe_replay.py` (stdlib + urllib, `--help` exits 0).
Scratch artifacts (request copy, parent state files, drill scripts) were under
`/Volumes/fleet-scratch/tmp/p0/` and are not committed. They hold a live transcript.

---

## P0.1 — is `x-pool-served-by` reachable on MAIN-turn responses?

**PASS.** The header is snapshotted on every main-turn transport. `relay_headers.py` only
duplicates this for the aux path (`_AuxRoute.served_by`, `agent/fork_ext/relay_headers.py:405`).

Where the main-turn response headers are reachable (file:line, base c1ff8826c6):

- Allow-list of captured headers: `agent/chat_completion_helpers.py:93` `_POOL_HEADER_NAMES`
  (first entry `"x-pool-served-by"`, L94); snapshot fn `_snapshot_pool_headers` L141; stamped on the
  returned message as `.pool_headers` by `_stamp_pool_headers` L157.
- **Non-streaming anthropic_messages** (`claude-alr`'s api_mode): `chat_completion_helpers.py:1447-1469`
  passes the call-scoped `_capture_with_pool_headers` (L1453) as `on_response` to
  `agent._anthropic_messages_create` (`agent/client_lifecycle.py:1050`, `on_response` L1062) for
  pooled providers (`_POOLED_PROVIDERS` L112 includes `claude-alr`).
- **Streaming anthropic_messages**: `_StreamingCall` snapshots the httpx response at
  `chat_completion_helpers.py:5543-5544` and stamps it at L5640-5641.
- **Streaming chat_completions** (bpr): L5067-5069, stamped L5478.
- Consumer that already reads the seat on success: `_record_successful_api_call` L543 →
  `headers = getattr(response, "pool_headers", None)` L579 → `_emit_api_call_record` →
  `_route_identity` L265, `served_by = headers.get("x-pool-served-by")` L275 → Blackbox
  `turn_api_calls.sub_key` with `attribution='wire'`.

So P1 needs no new capture plumbing: setting `parent_agent.last_served_by` is one read of
`response.pool_headers["x-pool-served-by"]` at the `_record_successful_api_call` site (or wherever
P1 chooses), with the same `_POOL_SUB_KEY_RE` validation `_route_identity` applies.

Live evidence (Blackbox, read-only):

```
$ sqlite3 -readonly "file:$HOME/.hermes/blackbox/turns.db?mode=ro" \
  "select provider, attribution, count(*), sum(sub_key is not null) from turn_api_calls
   where ts > strftime('%s','now')-86400 and provider like 'claude-alr%' group by 1,2;"
claude-alr|aux:compression|93|93
claude-alr|aux:title_generation|6|6
claude-alr|wire|17751|17746
```

(Re-run at 16:0x PT: `wire|17542|17537`, 17,397 of them `sub-vps-%`/`local`.) `attribution='wire'`
is only written when `x-pool-served-by` was present on the response (L277-278).

## P0.2 — byte-identical replay, pinned to the serving seat, reads the cache (LOAD-BEARING)

**PASS: cache_read 502,704 / 502,708 prompt tokens = 1.0000 ≥ 0.85.**

Source request: a real Apollo main-turn request saved by the harness on 2026-10-17 10:15
(`~/.hermes/sessions/request_dump_20260921_002254_3f4af45e_20261017_101517_262480.json`,
claude-alr `/anthropic/messages`, 713 messages, 53 tools, 2 system blocks, 4 `cache_control`
markers all `ttl: 1h`, 1,222,313 body chars). Its original model `claude-fable-5-1` was capped on
23/25 seats (`/health.fable_allowance`), so the probe overrode the model to `claude-opus-5-5`
(a model override changes the cache key consistently for both calls; the claim tested is the
replay, not the model). Normalisation applied once, in the parent step, and then reused
byte-for-byte: `x-hermes-*` headers dropped and replaced with a scratch session id
(`boomerang-p0-<hex>`, never Apollo's), a random scratch nonce prefixed to `system[0]` so the
parent write is provably cold and any later read is ours, `thinking`/`output_config` dropped,
`max_tokens` 256.

Parent (cold write):

```
$ python3 tools/dev_probes/boomerang_v2_probe_replay.py parent --request $S/req.json \
    --state $S/p02_parent.json --model claude-opus-5-5 --drop-thinking --ttl 1h
{"step": "parent", "session": "boomerang-p0-3324c8961f11", "model": "claude-opus-5-5", "messages": 713, "tools": 53, "body_bytes": 1208629, "pin": null}
{"elapsed_s": 17.0, "http": 200, "route_id": "d7ac7d78541b40b8b25683361094570e", "served_by": "sub-vps-16", "step": "parent", "usage": {"cache_creation": {"ephemeral_1h_input_tokens": 502704, "ephemeral_5m_input_tokens": 0}, "cache_creation_input_tokens": 502704, "cache_read_input_tokens": 0, "inference_geo": "global", "input_tokens": 4, ... "output_tokens": 256, ...}}
{"state": "/Volumes/fleet-scratch/tmp/p0/p02_parent.json", "prompt_tokens": 502708}
```

Replay (parent bytes unchanged + parent's reply as an assistant turn + ONE user turn,
`x-hermes-pin-sub: sub-vps-16`):

```
$ python3 tools/dev_probes/boomerang_v2_probe_replay.py replay --state $S/p02_parent.json
{"step": "replay", "session": "boomerang-p0-3324c8961f11", "pin": "sub-vps-16", "age_s": 26.7, "parent_prompt_tokens": 502708, "pre_messages_bytes_identical": true}
{"elapsed_s": 35.1, "http": 200, "route_id": "992536e792c64d1e867ffba1e99d17f7", "served_by": "sub-vps-16", "step": "replay", "usage": {"cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0}, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 502704, "inference_geo": "global", "input_tokens": 47, ... "output_tokens": 54, ...}}
{"verdict": "PASS", "expect": "hit", "cache_read": 502704, "cache_creation": 0, "parent_prompt_tokens": 502708, "read_ratio": 1.0, "threshold": 0.85, "served_by": "sub-vps-16", "age_s": 26.7}
rc=0
```

Notes: `x-hermes-pin-sub` is honoured by the relay (`claude_pool_relay.py:1201`, served-or-refused,
never re-routed); `served_by` on the replay equals the pin. The appended assistant+user turns were
47 uncached input tokens, i.e. the whole parent prefix was read.

## P0.3 — does `agent._session_messages` at dispatch equal the bytes the last request sent?

**PASS, with the transform chain named.** There is no post-hoc compaction or scrub rewrite of
`_session_messages` between turns; the request is derived from it by a deterministic, send-path-only
chain. Compaction is the one sanctioned rewrite, and it rewrites `messages` in place (it is then the
new truth both for `_session_messages` and the next wire). The fork prefix must be built by the SAME
chain, not from raw `_session_messages`.

Where `_session_messages` is (re)bound: `agent/turn_tool_round.py:231` (after every tool round),
`agent/session_persistence.py:677` (`_persist_session`, every exit path), plus the terminal paths
in `turn_final_response.py:191/316/387`, `turn_finalizer.py:722`, `turn_stop_gates.py:123`,
`turn_empty_response.py:241`, `turn_truncation.py:91/286/339/635/678`. It is always the live
`messages` list; nothing writes a transformed copy back.

Send-path chain (`agent/turn_request_assembly.py::assemble_api_request`, order is load-bearing):

1. `build_api_messages` (`agent/turn_context.py:1327`):
   `canonicalize_replay_history(messages[:split])` (L1359; `agent/replay_cleanup.py:154`:
   strip interrupted tool tails, strip a dangling tool-call tail, expire dangerous confirmations
   older than 60 s against the turn's frozen clock); drop interrupt-close rows on
   transcript-owning providers (L1366-1369); drop metadata-only tool notices (L1372); replay
   `api_content` sidecars (exact bytes sent live, L1382-1407); strip persistence-only fields
   (`timestamp`, `finish_reason`, `reasoning`, `_row_id`, … L1383-1415); prepend the system prompt
   (`_cached_system_prompt` + `ephemeral_system_prompt`, L1437-1441).
2. MoA context (only when MoA), `prefill_messages` insertion (L134-139).
3. `_apply_context_engine_selection` (L146) — inert on this fleet: no in-tree engine and not LCM
   (`context.engine: lcm`) overrides `select_context` (rg over plugins: 0 hits).
4. `_sanitize_api_messages` (L152), `evict_stale_outbound_tool_images` (L159).
5. `_drop_thinking_only_and_merge_users` (L178).
6. `.strip()` on string content (L185-187), `_canonicalize_api_tool_calls` (L188;
   `conversation_loop.py:1065`, compact JSON of tool-call arguments), surrogate strip (L192).
7. `build_prompt_cache_plan` (L205): cache_control markers, applied last.
8. Provider adapter (anthropic_messages conversion) after that.

In-process drill (real `AIAgent`, stub OpenAI-shape server recording every body, temp
`HERMES_HOME`; two turns, the first with a tool call):

```
$ env -i ... HERMES_HOME=$S/home TERMINAL_CWD=$S/work PYTHONPATH=<worktree> \
    nice -n 19 ~/.hermes/hermes-agent/venv/bin/python p0_drill.py <worktree>
{"probe": "P0.3", "wire_requests": 3, "last_wire_roles": ["system", "user", "assistant", "tool", "assistant", "user"], "session_roles": ["user", "assistant", "tool", "assistant", "user", "assistant"], "session_prefix_equals_last_wire": false, "diffs": [{"i": 1, "session": {"role": "assistant", "content": "", "tool_calls": [{"id": "call_stub_1", "name": "terminal", "args": "{\"command\": \"echo p03\"}"}]}, "wire": {"role": "assistant", "content": "", "tool_calls": [{"id": "call_stub_1", "name": "terminal", "args": "{\"command\":\"echo p03\"}"}]}}], "n_diffs": 1, "equal_after_args_json_canon": true, "session_tail_after_last_wire": ["assistant"], "session_only_keys": ["finish_reason", "name", "reasoning", "timestamp", "tool_name"]}
```

Read: the only content difference is step 6 (`_canonicalize_api_tool_calls`); with it applied the
two are equal. `session_tail_after_last_wire: ["assistant"]` is the final reply, which by definition
was never sent. Persistence-only keys are stripped by step 1.

Production twin (no new traffic): `agent/background_review.py::build_cache_parity_fork` (L1130)
already forks from `list(messages)` (`turn_finalizer.py:867`) with the parent's
`_cached_system_prompt` (L1182) and frozen tool surface (L1212), through the same chain. Its first
call's cache read, claude-alr, 3 days, prompts > 100k (fork turn ids carry a UUID middle segment):

```
k     n     read>=0.85  read<0.5  mean_read_ratio  min_prompt  max_prompt
fork  638   633         5         0.9815           124120      653067
live  2570  2097        472       0.8174           100921      835757
```

633 / 638 (99.2 %) of production forks built from the parent's in-memory transcript read ≥ 85 % of
their prompt on the first request. That is the strongest available evidence that the in-memory
transcript reproduces the sent bytes.

## P0.4 — TTL window

**1h arm: PASS** — a 1h-ttl parent replayed at 51.3 min read 502,706 / 502,710 (1.0000).
**5m arm: NOT REPRODUCIBLE on claude-alr (spec premise does not hold on this lane).** The parent was
sent with every `ttl` key removed (Anthropic default = 5m; verified in the saved bytes:
`cache_control 4, ttl 0`). The usage block billed it as `ephemeral_1h_input_tokens: 502703`, and a
replay at 34.2 min (> 6× the 5m window) read 502,703 / 502,707.

Cause, read from the deployed apx (`~/.hermes/deploy/claude-apx-0`, b03d61c):
`proxy.js:4337-4346` "stamps it on EVERY cache_control block, including caller-authored ones",
`effectiveCacheControlTtl` L6060-6064 returns `1h` unless the seat `isUsingOverage()` (then 5m).
So on claude-alr the wire ttl is decided by apx per request, not by the harness's `_cache_ttl`.

```
# 1h arm (parent 15:19:39 PDT, replay 16:10:56 PDT)
$ python3 tools/dev_probes/boomerang_v2_probe_replay.py parent --request $S/req.json --state $S/p04_1h.json --model claude-opus-5-5 --drop-thinking --ttl 1h
{"elapsed_s": 10.2, "http": 200, "route_id": "66164beba01e459594d324325834bcf3", "served_by": "sub-vps-15", "step": "parent", "usage": {"cache_creation": {"ephemeral_1h_input_tokens": 502706, "ephemeral_5m_input_tokens": 0}, "cache_creation_input_tokens": 502706, "cache_read_input_tokens": 0, "input_tokens": 4, ...}}
$ python3 tools/dev_probes/boomerang_v2_probe_replay.py replay --state $S/p04_1h.json --threshold 0.85
{"step": "replay", "session": "boomerang-p0-ee87783ce85a", "pin": "sub-vps-15", "age_s": 3077.9, "parent_prompt_tokens": 502710, "pre_messages_bytes_identical": true}
{"elapsed_s": 7.5, "http": 200, "route_id": "7a596a60f27040da80bd45d5320b8c15", "served_by": "sub-vps-15", "step": "replay", "usage": {"cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0}, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 502706, "input_tokens": 47, ...}}
{"verdict": "PASS", "expect": "hit", "cache_read": 502706, "cache_creation": 0, "parent_prompt_tokens": 502710, "read_ratio": 1.0, "threshold": 0.85, "served_by": "sub-vps-15", "age_s": 3077.9}

# "5m" arm (parent sent with no ttl keys; replay at 34 min, expecting a miss)
$ python3 tools/dev_probes/boomerang_v2_probe_replay.py parent --request $S/req.json --state $S/p04_5m.json --model claude-opus-5-5 --drop-thinking --ttl 5m
{"elapsed_s": 37.8, "http": 200, "route_id": "e4cc48ae92c24d019c877346d74d91f6", "served_by": "sub-vps-5", "step": "parent", "usage": {"cache_creation": {"ephemeral_1h_input_tokens": 502703, "ephemeral_5m_input_tokens": 0}, "cache_creation_input_tokens": 502703, "cache_read_input_tokens": 0, "input_tokens": 4, ...}}
$ python3 tools/dev_probes/boomerang_v2_probe_replay.py replay --state $S/p04_5m.json --expect miss --threshold 0.01
{"step": "replay", "session": "boomerang-p0-46f5dd57704d", "pin": "sub-vps-5", "age_s": 2050.9, "parent_prompt_tokens": 502707, "pre_messages_bytes_identical": true}
{"elapsed_s": 49.3, "http": 200, "route_id": "5a29fac8e20a4cca9a33e813b0cbb25d", "served_by": "sub-vps-5", "step": "replay", "usage": {"cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0}, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 502703, "input_tokens": 47, ...}}
{"verdict": "FAIL", "expect": "miss", "cache_read": 502703, "cache_creation": 0, "parent_prompt_tokens": 502707, "read_ratio": 1.0, "threshold": 0.01, "served_by": "sub-vps-5", "age_s": 2050.9}
rc=1
```

Spec consequences (for Apollo): (a) the 1h window holds at ~50 min, so "warm ≥ 60 min" stays an
upper bound, not a measured one; (b) on claude-alr, deriving `warm` from the parent's `_cache_ttl`
is conservative: a parent configured 5m is actually written at 1h unless the seat is in overage,
and an overage seat writes 5m regardless of what the harness asked. The honest warm signal on this
lane is the parent's last response usage split (`cache_creation.ephemeral_1h_input_tokens` vs
`ephemeral_5m_input_tokens`), not `_cache_ttl`. "5m-ttl parents never fork" (pass-3 lens) is still
safe, merely unreachable on alr except during overage.

## P0.5 — per-session serialisation primitive for the snapshot; does `action=stop` cancel?

**PASS.**

**There is no lock on `_session_messages`.** It is a plain attribute rebound to the live `messages`
list (P0.3 sites). What serialises a session:

- Gateway, in-process: the per-session turn slot `TurnState.agent` (`gateway/session_state.py:22`,
  `None` = idle), exposed as the legacy view `GatewayRunner._running_agents`
  (`gateway/run.py:4499`, `legacy_dict_property`, `session_state.py:263`) and consumed by the busy
  path (`gateway/run_busy.py`, `GatewayBusySessionMixin` L92; `_snapshot_running_agents`
  `run.py:5204`). While it is non-None, inbound messages for that key are queued, never run.
- Cross-process: the durable session turn lease, taken by `TurnFacadeMixin.run_conversation`
  (`agent/turn_facade.py:57`) via `admit_durable_turn_lease` (`agent/turn_facade_lease.py:281`)
  → `SessionDB.acquire_session_turn_lease` (`hermes_state_compression.py:598`).

Two dispatch contexts, two answers:

1. **`delegate_task` (spawned from the parent's own tool call):** the parent is mid-turn by
   construction and nothing serialises the read (`tools/delegate_tool.py:2040`). What it sees
   depends on the round:
   - **Round 1 of a turn (drilled):** `_session_messages` is still the list bound at the previous
     persist, not the live `messages`. Drill (stub server, real AIAgent) read it while the first
     tool call was running and got only the user row, i.e. exactly the last request's
     conversation, without the in-flight assistant `tool_calls` row:

     ```
     {"probe": "P0.3-mid-tool", "session_messages_during_tool": {"roles": ["user"], "last_has_tool_calls": false}, "same_list_object_as_final": false, "first_wire_roles": ["system", "user"]}
     ```
   - **Round ≥ 2 (read from code, not drilled; the stub only tool-calls once per conversation):**
     `turn_tool_round.py:231` binds `_session_messages = messages`, the LIVE list. The next
     round's assistant `tool_calls` row and every tool result already finished in that round
     (`tool_executor.py:1303` appends per call; batches run on a worker pool,
     `_MAX_TOOL_WORKERS = 8` at L131) are visible through it, including a dangling tail with the
     `delegate_task` call's own result missing. A `list(...)` copy taken while a sibling tool
     result is being appended is a race on a plain list.

   So the fork snapshot must NOT read `_session_messages` raw from inside a tool call. Use the last
   SENT request instead (P0.3: build it with `assemble_api_request`, or keep the last request's
   message list on the agent), which is the prefix INV-2 actually needs; the
   `strip_dangling_tool_call_tail` step of `canonicalize_replay_history` already removes a dangling
   tail on the next send, which is why the wire stays coherent.
2. **Auto-handle wake (§5.5, gateway notifier, outside a turn):** take the snapshot only while
   `TurnState.agent is None` for the home session key, on the event loop (so no turn can be
   admitted between the check and the `list(...)` copy, since admission sets the slot on the same
   loop). If the slot is busy, wait off-loop (asyncio task, as §5.5 already says). For cross-process
   safety (Desktop/CLI resume on the same session) the snapshotter can hold the durable lease for
   the copy; that is the only cross-process primitive that exists.

**Cancel path: works.** Chain: `delegate_task(action="stop", subagent_id=...)`
→ `tools/delegate_tool.py:3763` (control actions bypass spawn) → `_handle_control_action`
(`tools/delegate_tool_registry.py:298`; ownership check `_owns_subagent_record` L229) →
`interrupt_subagent` (L99, called at L322) → `request_hard_interrupt` (`agent/interrupt_compat.py:22`)
→ `InterruptControlMixin.hard_interrupt` → `interrupt(..., hard_cancel=True)`
(`agent/interrupt_control.py:115`): sets `_interrupt_requested`, signals the child's tool thread
(L193), fans out to grandchildren (L202). Spawned children are registered at
`tools/delegate_tool_child_run.py:362`.

Drill: real parent + real child `AIAgent` (stub server), child registered with the same record
shape as L362, child running `touch <marker> && sleep 60` through the terminal tool; parent sends
`stop` through the real handler 0.5 s after the tool started:

```
{"probe": "P0.5", "tool_started": true, "stop_reply": {"action": "stop", "subagent_id": "sa-p05-1", "status": "interrupt_requested", "note": "The subagent stops at its next iteration boundary (in-flight tool calls are asked to cancel). Its partial result still re-enters the conversation as a completion message — do not wait or poll."}, "child_alive_after_30s": false, "unwind_s": 2.14, "interrupted": true, "exc": null, "final": "None"}
  ┊ 💻 $         touch .../child_tool_started + 1 command  3.3s [exit 130]
```

The in-flight `sleep 60` was killed (exit 130) and the child's turn returned `interrupted=true`
2.14 s after the stop: well inside the reconciler's 60 s bounded wait. Caveat: the drill registered
the record directly rather than through a full `delegate_task` spawn, so it exercises the stop path
and ownership check, not spawn registration. A first attempt with a mis-keyed record
(`owner_session_id` instead of `owner_agent_session_id`) was correctly refused
(`No live subagent ... in this conversation's spawn tree`), which also confirms the ownership gate.

---

## Proposed spec §0 change-log row (Apollo applies; the spec lives outside this repo)

| 2026-10-18 | **Phase 0 probes run (t_6b452366, `docs/boomerang-v2/phase0-probes.md`).** P0.1 PASS: main-turn `x-pool-served-by` is already captured (`chat_completion_helpers.py` `_POOL_HEADER_NAMES` L93, anthropic non-stream L1453, stream L5544; read at `_route_identity` L275); P1 reads `response.pool_headers`, no new capture. **P0.2 PASS: 502,704/502,708 read (1.0000) on a pinned replay of a real 713-msg Apollo request; P2 is NOT re-scoped.** P0.3 PASS: no post-hoc rewrite; wire = `_session_messages` through the send-path chain (`canonicalize_replay_history`, field strip, sanitize, user-merge, strip, tool-arg JSON canon, cache plan); fork prefix MUST be built by `assemble_api_request`, never raw; review-fork twin reads ≥85 % on 633/638 first calls. P0.4: 1h arm PASS at 51.3 min (1.0000); 5m arm not reproducible on claude-alr: apx stamps `ttl=1h` on every block unless the seat is in overage (`proxy.js` L4337/L6060), so `warm` should derive from the parent's last usage split (`ephemeral_1h` vs `ephemeral_5m`), not `_cache_ttl`. P0.5 PASS: serialisation = gateway `TurnState.agent` (`session_state.py:22`) + durable turn lease (`turn_facade_lease.py:281`); nothing locks `_session_messages`; inside the parent's own tool call it is one round stale (round 1) or the LIVE list with a dangling tool tail (round ≥2), so the fork snapshot comes from the last SENT request, not raw `_session_messages`; `action=stop` unwound a child mid-`sleep 60` in 2.14 s. |
