# Running delegate_task and fork children's tool commands on fleet pool hosts: assessment

Card t_59904095, part 2 of 2 (part 1 is t_b3b69464, detached jobs). Origin: Ace, Discord msg
1570982906569170976, 2026-11-13 18:05 PT: "can we make it so detached jobs and delegate-task subagents can
be allocated to fleet work pool hosts e.g. ci box?" Status: ASSESSMENT. This round changes no code and no
delegation behaviour. Code references are to hermes-agent main at eba69f24f7.

## 0. Verdict

- **Yes, technically.** Option (a) works: the child's model loop stays in the gateway and only its
  terminal, file and execute_code tools run over the `ssh` terminal backend on a pool host. Kanban workers
  already run this way in production. KWLB v0.1 (`hermes_cli/kanban_worker_hosts.py`) put 506 of 751 worker
  spawns on pool hosts in 24 h (Studio placement events, 2026-11-09, plans/2026-11-09 SPEC §1.2). Per-call
  latency over the runtime's own `SSHEnvironment` is 13-25 ms for a no-op, against 10 ms local (§3.1).
- **The payoff is small, and most children cannot move.** Subagent sessions issued **8.3 % of the default
  profile's terminal calls and 18.5 % of its non-sleep terminal wall time** (Studio state.db snapshot,
  2026-06-17..07-26, §4.2). Chat sessions (Discord) issued 77 % of the calls and 62 % of that wall time. Of
  319 children in the same snapshot, **12 % touch no Studio path at all**. A further 10 % touch only a
  repo checkout. 66 % touch the live home tree, state/ or macOS-only tools (§4.1). Moving every portable
  child would take roughly 0.03 cores off the Studio. That is an estimate, derived in §4.3.
- **Recommendation: build the narrow version of (a) behind an off-by-default knob.** The parent opts in
  per task (`placement: "pool"`) for repo-only children: review readers over a pushed ref, evidence sweeps,
  off-box test and lint runs. Do not build (b). (c) already exists for pytest. One independent defect
  turned up: the ssh backend sets no keepalive, so a pool host that dies mid-command holds the tool call
  until the tool timeout. Kanban pool workers have the same problem today (§3.3). Fix it whatever is
  decided here.

## 1. What a child is today (code facts, eba69f24f7)

- A `delegate_task` child is an `AIAgent` built in the gateway process and run on a thread
  (`tools/delegate_tool.py`, `tools/delegate_tool_child_run.py`). A fork child
  (`tools/delegate_tool_fork.py`) does the same and also inherits the parent's transcript and cache seat.
- A child's terminal environment is looked up by key in `tools/terminal_tool.py::_resolve_container_task_id`.
  With a session key present (the gateway sets it per message), every non-docker backend uses the key
  `session:<key>`. A child carries the parent's session key, so **it shares the parent's environment
  object**. `register_container_alias(child, parent)` makes that explicit.
- The backend comes from the profile-scoped `TERMINAL_*` policy (`tools/terminal_scope.py`, a ContextVar),
  then `_reapply_kanban_placement()`. Children run under `contextvars.copy_context()`
  (`delegate_tool.py:2782`, `:4601`), so a per-child scope set inside the child's context reaches only that
  child.
- File tools (`tools/file_tools.py::_get_file_ops`) run their reads, writes and patches as shell commands on
  the same terminal environment. Moving the terminal therefore moves the file tools too.
- Browser, kanban_*, web_*, mem0_*, ha_*, send_message and skill tools do not use the terminal environment.
  They run in the gateway whatever the terminal backend is.
- Background units: the code for "unit = card on the internal `delegations` board"
  (`tools/async_delegation_kanban.py`, `tools/delegation_unit_worker.py`) is on main. It is gated by
  `delegation.background_runtime` (default `in_process`). Its spec (docs/dev/delegate-background-on-kanban-
  runtime.md, I5 and §2) pins the unit and its tools to the dispatching host. Cross-host placement is an
  explicit non-goal there.

## 2. Options

| | (a) loop in gateway, terminal over ssh to an allocated pool host | (b) child becomes a kanban-worker-style process on the pool host | (c) only heavy command classes go remote |
|---|---|---|---|
| What moves | terminal, file and execute_code tool commands | the whole child: model loop and all tools | single commands (pytest, lint, builds) |
| Exists today | for kanban workers (KWLB v0.1 `placement_env`); not for children | no. KWLB v1.0 (whole worker remote) is unbuilt and was ruled "do not build now" (plans/2026-11-09 SPEC §0) | pytest/pyright via `scripts/test-gate` (remote is the default for narrow runs, skill `worker-test-gate` rule 3) |
| Studio CPU moved | the command CPU (§3.2) | the command CPU plus about 0 loop CPU (a remote-loop worker costs 0.007 cores on the Studio, plans/2026-11-09 SPEC §0) | the CPU of that one class |
| Extra start cost | about 0.2-0.4 s, the first ssh connect (§3.1) | +3-5 s and +220-250 MB RSS per child (delegate-background spec §3, Studio) plus a remote boot | none, or per-command rsync for test-gate |
| Sync children | yes | no: the parent would block on a remote process. Workable only for background units | yes |
| Fork children | yes: the loop, transcript and cache seat stay in the gateway | no: the transcript and cache seat cannot cross a process (delegate-background spec §2) | yes |
| Code size (estimate) | small to medium: a per-child terminal scope, an allocator lease, an env-key split (§5) | large: a remote runtime, a remote home, credentials, a result channel | done for pytest; each further class is a separate allowlist entry |

## 3. Measurements

All measured 2026-11-13 18:17-18:35 PST from ACE-MEDIA (uid kanbanw, 32 cores, load1 3.4-4.3) toward
ci-box (192.168.1.89, load1 130 of 256 threads at 18:13 per the placement event) and ACE-AI (192.168.1.216,
load1 16 of 32). ACE-MEDIA stands in for the Studio as the caller. LAN round trips from the Studio are of
the same order but were not measured from it. Scripts are attached to the card (`t599-measure.tgz`).

### 3.1 Latency per tool call (`t599_envbench.py`, the runtime's own environment classes, n=20 per row)

| path | first use | `true` p50 / p90 | ls of a small dir p50 / p90 |
|---|---|---|---|
| `LocalEnvironment` | 156 ms | 9.5 / 9.9 ms | 45.7 / 70.3 ms |
| `SSHEnvironment` -> ACE-AI | 200 ms | 13.2 / 13.3 ms | 67.1 / 67.6 ms |
| `SSHEnvironment` -> ci-box (loaded) | 400 ms | 24.7 / 24.8 ms | 105.1 / 105.2 ms |

Raw ssh (`t599_lat.sh`, n=30 warm / 10 cold): warm ControlMaster p50 4.7 ms (ACE-AI) and 10.6 ms (ci-box).
A cold connect costs 126 ms and 339 ms. The backend's session-snapshot wrapper adds about 9-14 ms per call
on top of raw ssh. Each call therefore costs **+4 ms (ACE-AI) to +15 ms (ci-box)**, plus roughly +20-60 ms on
commands that do file I/O. For scale, a subagent's non-sleep terminal call averages about 8.5 s of wall time
(27.9 h over about 11,760 calls, §4.2), so the extra cost per call is under 1 %.

### 3.2 CPU the caller pays (`t599_envcpu.py`, RUSAGE_CHILDREN deltas, n=30)

| command | caller CPU per call, local | caller CPU per call, over ssh |
|---|---|---|
| `true` | 5.1 ms | 3.5 ms |
| light grep over a stdlib dir | 6.0 ms | 3.6 ms |
| 0.3 s of python CPU | 157.3 ms | 3.8 ms |

The ssh client costs a flat ~3.5-4 ms per call on the caller. Everything the command itself burns lands
on the pool host. A no-op costs slightly less over ssh because the local backend's snapshot and sourcing
wrapper is heavier than the ssh client.

### 3.3 Pool host dies mid-command (`t599_deadhost.sh`)

The ssh transport ran through a local `nc` relay. At t=3 s the relay was SIGSTOPped, which looks exactly
like a host losing power: no RST, no FIN. The remote command was `sleep 40`.

| ssh options | result |
|---|---|
| as the runtime builds them (`tools/environments/ssh.py::_build_ssh_command`: BatchMode, ConnectTimeout=10, ControlPersist=300, **no ServerAlive**) | did not return. Killed by the drill's 60 s cap (rc 124) |
| plus `ServerAliveInterval=5 ServerAliveCountMax=2` | returned after 14.3 s, rc 255, `Timeout, server ... not responding.` |

Connecting to a dead address returns in 10 s (ConnectTimeout, `t599_deadhost` probe to 10.255.255.1).
Connecting to an address with no route returns in 3.1 s. A command that is already running has no bound
except the tool's own timeout (foreground default 180 s, `terminal_tool.py`). This affects KWLB pool
workers today, not only a future child placement.

## 4. Which children are portable (census)

### 4.1 Source and method

No pool-readable copy of the live delegation ledger exists. `fleetw-studio read` refuses
`state/async-delegations.json` and `state.db` ("not in the file allowlist", checked 18:20). The newest
`delegations.kanban.db` backup (`/mnt/nas-backups/kanban/20261113-181855/`) holds 173 archived unit cards,
2026-10-26..11-03. Their bodies carry only ids, not tool calls. The usable source is the **Studio
`~/.hermes/state.db` copy in `/mnt/nas-backups/machines/mac-studio-m3u/pre-ssd-swap/`** (mtime
2026-07-26). It holds 319 `source='subagent'` sessions from 2026-06-17 to 2026-07-26, with 16,509 tool
calls, 12,047 of them `terminal`. That is 4 months old: it predates test-gate (2026-09-25), KWLB and the
fleetw split. Treat the shares as the shape, not current counts.

Each child is classified by the strongest Studio need found in any of its host-touching tool-call
arguments (`t599_census2.py`/`t599_census3.py`; regexes are in the scripts). Classes, strongest first:
live_state (kanban.db, state.db, `~/.hermes/state|sessions|logs|cron|memories|secrets`, `hermes
kanban|gateway|cron|config`, sqlite3, `op`, `.env`), mac_only (launchctl, osascript, /Applications,
/Library, imsg, ...), hermes_tree (`~/.hermes/skills|scripts|hooks|plugins|runtime|config`), and
repo_checkout (`/Users/alexgierczyk/...` or `~/Projects`). The regexes over-match: a child that only
*mentions* kanban.db in a grep pattern counts as live_state. The portable shares are therefore lower
bounds.

| class | children | share | of which write files (patch/write_file) | read-only | host-touching calls | share |
|---|---|---|---|---|---|---|
| live_state | 132 | 41.4 % | 84 | 48 | 8,996 | 57.9 % |
| mac_only | 8 | 2.5 % | 7 | 1 | 495 | 3.2 % |
| hermes_tree | 70 | 21.9 % | 42 | 28 | 4,123 | 26.5 % |
| repo_checkout | 32 | 10.0 % | 17 | 15 | 796 | 5.1 % |
| **no Studio reference** | 39 | **12.2 %** | 2 | 37 | 1,120 | 7.2 % |
| hostless tools only (web, skills, memory) | 14 | 4.4 % | 0 | 14 | 0 | 0 % |
| no tool calls | 24 | 7.5 % | 0 | 24 | 0 | 0 % |

So:
- **Portable today with no state at all: 39 children (12.2 %), 37 of them read-only.** They are worth
  moving because they run commands. The hostless and no-tool children (11.9 %) are portable trivially, but
  moving them saves nothing: they run no commands.
- **Portable with a checkout:** the read-only repo_checkout children (15, 4.7 %) can move if the pool host
  has the repo at the right ref. Fleet mirrors exist on pool hosts
  (`~/.hermes/mirrors/<owner>/<repo>.git` on ACE-MEDIA, kanbanw). The 17 writers need a push back (§5.2).
- **Not portable:** live_state + mac_only + hermes_tree = 210 children (65.8 %). The read-only
  hermes_tree children (28) could be served by the pool's `hermes-home` main copy
  (`/home/kanbanw/repo`, synced to origin/main 18:17). That copy is **main, not the live tree**, so a child
  inspecting live drift would read the wrong bytes.

### 4.2 How much of the Studio's terminal load is children (same snapshot, all sources)

| session source | terminal calls | share | terminal wall h | non-sleep wall h | share of non-sleep |
|---|---|---|---|---|---|
| discord (chat) | 110,871 | 76.8 % | 199.9 | 93.1 | 61.7 % |
| subagent | 12,047 | 8.3 % | 44.6 | 27.9 | 18.5 % |
| desktop | 6,550 | 4.5 % | 14.4 | 7.9 | 5.2 % |
| cron | 5,683 | 3.9 % | 8.2 | 8.0 | 5.3 % |
| cli / tui / telegram | 9,266 | 6.4 % | 17.6+ | 12.0+ | 8.0 %+ |

Wall time = tool-result timestamp minus the issuing assistant message's timestamp. "Non-sleep" drops
commands whose text contains `sleep` (polling and waits). Wall time is not CPU. It is the best proxy the
transcript offers.

Inside the subagent wall (`t599_cmdtime.py`, 12,044 of 12,047 calls matched): sleep and wait 28.8 %,
pytest/test 22.6 % (pre-test-gate), gh/curl/ssh 20.2 %, python/node scripts 10.3 %, other 9.4 %,
grep/find/cat/ls 6.2 %, git 2.2 %, build/install 0.2 %.

### 4.3 What moving portable children would save (estimate, stated as one)

The card body's operator measurement on the Studio, 11-13 17:38: of the default gateway owner's 1.9
cores, 1.09 were terminal-tool commands from chat sessions and their subagents. If subagents' share of
that CPU matches their share of non-sleep terminal wall time (18.5 %), children account for about **0.2
cores**. The children with no Studio reference plus all repo_checkout children make up 12.3 % of the
host-touching calls (7.2 % + 5.1 %, §4.1), so moving them saves about **0.025 cores**, call it 0.03. This assumes CPU is proportional to calls or wall time, which nothing here measured.
The operator should check it with the Studio count in §6 before weighing it.

## 5. Losses per option

### 5.1 What the child loses

| loses | (a) terminal over ssh | (b) whole child remote | (c) heavy class remote |
|---|---|---|---|
| live home tree `~/.hermes` (skills, scripts, hooks, plugins at live bytes) | lost for commands. The pool has a hermes-home **main** copy and the skills content repo, not the live tree. Skill *tools* (`skill_view`) still work, since they run in the gateway | lost entirely, unless a remote home is synced. Today's KWLB keeps credentials and home on the Studio (`TERMINAL_SSH_SYNC_FILES=false`, `kanban_worker_hosts.py`) | none: that one command is shipped with its tree |
| `state/`, `state.db`, logs, sessions | lost for commands | lost | none |
| `kanban.db` | kept: `kanban_*` tools run in the gateway. Shell access to the db is lost; the pool's `~/.hermes/kanban.db` is a refusal directory (t_95cc255d) | lost, unless the child goes through the board RPC a v1.0 worker would need | none |
| the vault (Obsidian) | lost for commands (Studio path) | lost | none |
| browser sessions | **kept**: browser tools run in the gateway, not the terminal | lost (the CDP and browser-lease plumbing is Studio-side) | none |
| operator token and credentials | commands run as `fleetw`/`kanbanw` with the pool's own creds (e.g. GH_TOKEN = `ang-fleet-workers[bot]` on ACE-MEDIA). Operator `gh`, `op` and `.env` keys are not there. That is a feature for read-only children and a blocker for anything that mutates as the operator | same, plus the model credentials would have to reach the remote process | none |
| macOS-only tools (launchctl, osascript, imsg, Keychain) | lost (pool hosts are Linux) | lost | none |
| the parent's cwd | lost. The child starts in a pool workspace, not the parent's checkout | lost | the command runs on a copy |

### 5.2 How edited files get back

- (a): edits land on the pool host's disk. `/Volumes/fleet-scratch` on ACE-MEDIA is a local ext4 disk,
  not a shared mount (`stat -f`, 18:33). Edits come back only through git: the child commits to a branch
  and pushes, as a ported kanban worker does (`worktree_ported` event). The parent then fetches. A child
  that edits the **parent's working tree** (the Studio checkout the parent is in) cannot be placed. Its
  edits would be invisible to the parent, and the post-run `file_state.writes_since` sibling-write check
  (`delegate_tool_child_run.py:966`) would be blind to them.
- (b): same as (a), plus the result travels by card or record.
- (c): no edits. The command's outputs (test logs) come back as tool output.

### 5.3 What a read-only child needs (the common case: review readers, evidence sweeps)

1. The repo at the reviewed ref on the pool host: a `--shared` worktree off the fleet mirror plus `git
   fetch` of the PR head. It is cheap, but someone must prepare it before the first command. The child can
   do it itself only if the parent's goal names the ref.
2. Its `gh` reads to work with the pool identity (`ang-fleet-workers[bot]` on ACE-MEDIA, `gh auth status`
   18:17). Reads succeed on repos that app is installed on and fail on the rest. Not enumerated here.
3. No Studio paths in its goal. That is a parent-side decision: the parent knows what the child will
   read, the allocator does not.
4. A bounded failure when the host dies (§3.3).

### 5.4 What the user sees when the pool host dies mid-child

- (a), today's ssh options: the child's current terminal or file call hangs until the tool timeout (180 s
  foreground default), then errors. The child's loop keeps running in the gateway: it can retry, report the
  failure or finish with what it has. The parent and the chat are unaffected. Uncommitted edits on that
  host are lost. With ServerAlive 5x2 the hang drops to about 14 s (§3.3). Each later call fails at connect
  in 3-10 s until the child gives up, unless the lease is re-placed (not proposed for the first slice).
- (b): the whole child is gone. With the delegations-board runtime the record is finalized as an error
  and delivered (I3: no automatic re-run). The parent sees a failed subagent and decides whether to
  re-delegate.
- (c): that one command fails or falls back. test-gate falls back to local loudly
  (`PHASE=test_gate_remote_fallback`).

## 6. What the operator must count on the Studio to replace the snapshot numbers

The snapshot is 4 months old. To get current shares, run `t599_census2.py` and `t599_cmdtime.py`
(attached, read-only, `mode=ro&immutable=1`) on the Studio against a **copy** of `~/.hermes/state.db`
(it was 2.8 GB in July). Both scripts filter on `sessions.source='subagent'`. Add
`and started_at > strftime('%s','now','-14 days')` to the session queries for a two-week window. For the
CPU split, attribute 60-120 s of `ps -o time=` deltas for the terminal-tool subprocesses whose ancestor is
a child's tool call (the t_4a046fb0 method, `t4a_cpu_attr.py`). Splitting chat and subagent requires
tagging terminal subprocesses with the session source, which the runtime does not do today. The
`HERMES_SESSION_KEY` is in the env, but a child shares its parent's key (§1).

## 7. Recommendation

**Do not build (b).** It costs the most, cannot serve sync or fork children, and the only part of a child
that costs Studio CPU is its commands. (a) moves exactly that.

**(c) stays as is.** pytest/pyright already go remote through test-gate. Do not grow it to more command
classes for children: the rest of the subagent wall is sleep, network calls and short reads, which have
no CPU worth moving.

**Build (a) narrow, opt-in per task, off by default.**

First shippable slice (one PR, hermes-agent):
1. A config knob `delegation.pool_placement: off | opt_in`, default `off`. It is the kill switch: with
   `off` the new field is ignored and every child runs exactly as today. Flipping it back affects only
   children started after the flip; running children finish where they are.
2. A per-task field `placement: "pool"` on delegate_task task items, settable by the parent. The tool
   description says what it means: the child only needs a repo at a ref, does not read live Studio state,
   and does not edit the parent's checkout. No heuristics and no automatic classification: the census
   shows regexes cannot tell portable from Studio-bound reliably.
3. On an eligible child: lease a host from the Fleet Work Allocation System (the same
   `alloc_client` path KWLB uses), build the `TERMINAL_*` values with `kanban_worker_hosts.placement_env`,
   and bind them as a terminal scope inside the child's copied context only. Key the child's environment
   by its own task id instead of the parent's `session:<key>`, so the child never shares the parent's
   local environment and the parent never inherits the child's remote one. Release the lease when the
   child ends, on every exit path.
4. Fail open to local with a loud line in the child's live transcript and a counter whenever there is no
   lease, the knob is off or the probe fails. That keeps the "no Studio cap on delegated children" rail:
   placement never makes a child wait or refuses one.
5. Add `ServerAliveInterval=5`, `ServerAliveCountMax=2` to `SSHEnvironment._build_ssh_command`. This
   could ship first as its own PR, since it fixes KWLB pool workers today (§3.3).

Acceptance for that slice (engineered, not soaked): one sync child and one background child, each with
`placement: "pool"`, run `hostname; id -un` through the terminal tool and through `read_file` of a file
that exists only on the pool host. Both must print the pool host. A parent `terminal` call in the same
turn prints the Studio. The dead-host drill of §3.3, run against a child's live environment, must end the
call in under 20 s with the child still producing a final answer. With the knob `off`, the same calls
print the Studio.

Not in the first slice: re-placing a child whose host died, children that write and push, and moving
chat-session commands (the larger share, 62-77 % in §4.2). If Studio CPU is the goal, a chat session's own
heavy commands are the bigger lever. That is a separate question from children and is not assessed here.
