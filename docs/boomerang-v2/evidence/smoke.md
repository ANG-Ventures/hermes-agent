# Boomerang v2 live smoke evidence (t_989cde1b)

Raw harness output below (`tools/dev_probes/boomerang_v2_smoke/run.py --live`), last run of each step:

- Step 1 (AC-7, INV-5, INV-6): **PASS**, 2026-10-19 16:33, runtime `fa2fe81d`, board `bv2-smoke-r7`.
- Step 2 (AC-8, INV-7): **PASS**, 2026-10-19 16:41, runtime `fa2fe81d`, board `bv2-smoke-r8`.
- Step 3 (AC-9, INV-8): **PASS**, 2026-10-19 22:59, runtime `05071447`. A real fork turn
  (`boomerang.fork served_by=sub-vps-1 read_share=0.9845`) produced 25 wire records from `sub-vps-1`, 0 of them
  carrying an `x-hermes-*` header.
- Step 4 (AC-11, §5.7): **PASS on adjudication**, 2026-10-20 00:05, runtime `c34c07e7` (whole fleet restarted onto
  it). The harness printed FAIL on ONE check. The facts:
  - The fork child `20261020_000336_66a346` for `t_1b09baf2` was dispatched at 00:03:34. Its last transcript row is
    00:04:22, and it never handed off (no `[auto-handled]` line, before or after).
  - The drill restart booted at 00:05:02 (`boot_id 39855 -> 19670`).
  - The reconciler logged `kanban auto-handle: t_1b09baf2 lost reason=gateway restart resurfaced delivered=True`
    at **00:05:30.235**, 27.5 s after boot, in `logs/gateway.log` and `logs/agent.log`. The claim went terminal
    with `outcome=lost`, `resurface_delivered=True`.
  - `⚠️ auto-handle lost t_1b09baf2 (reason: gateway restart)` posted to the origin chat (harness PASS), and no
    late completion arrived (harness PASS).
  - The one FAIL was the harness's window. It polled `2 × tick + slack + tick` = 25 s for the log line, but §5.7's
    reconciler runs at the END of the first notifier tick after startup. That tick starts after adapter wiring
    plus the notifier's 5 s initial delay, and on this host (load ~70) one full tick over every subscription
    takes tens of seconds. The line landed 2.5 s after the window closed. hermes-agent#(this PR) bounds that
    check by the first post-boot tick (`--first-tick-seconds`, default 90).

Earlier runs that read FAIL/INCOMPLETE were harness or setup faults, each fixed and re-run:
- seat naming (#2140)
- log file (#2130)
- the busy-parent summary fallback
- drill children that finished before the restart landed

Two were product bugs:
- the fork child received no parent transcript (QA F-1, #2144)
- the reconciler outcome had no log line (#2150)


Written by `tools/dev_probes/boomerang_v2_smoke/run.py`; one section per step, replaced on every run of that step.

<!-- bv2-smoke:step1:begin -->
## Step 1: auto-handle origin-chat lines (AC-7, INV-5, INV-6)

- mode: `live`  started: 2026-10-19T16:32:58-07:00  finished: 2026-10-19T16:34:31-07:00
- verdict: **PASS**
- board `bv2-smoke-r7`; gateway log `/Users/alexgierczyk/.hermes/logs/gateway.log`; origin chat `discord:1561469136796782633`
- card `s1_completed` = `t_78080b3d`
- card `s1_needs_input` = `t_7fc05eca`
- card `s1_failure` = `t_dcfcc7f0`

### Commands

```text
$ hermes kanban boards create bv2-smoke-r7 --name 'BV2 smoke (scratch)' --description 'Boomerang v2 smoke harness scratch board (t_989cde1b); safe to archive'   # exit 0
$ hermes kanban --board bv2-smoke-r7 create 'bv2 smoke s1: completed -> auto-handled' --session 20261018_130347_87039e56 --assignee human:apollo --json   # exit 0
$ hermes kanban --board bv2-smoke-r7 notify-subscribe t_78080b3d --platform discord --chat-id 1561469136796782633 --chat-type channel --delivery-mode notify+wake   # exit 0
$ hermes kanban --board bv2-smoke-r7 edit t_78080b3d --auto-handle true   # exit 0
$ hermes kanban --board bv2-smoke-r7 create 'bv2 smoke s1: needs_input -> surface' --session 20261018_130347_87039e56 --assignee human:apollo --json   # exit 0
$ hermes kanban --board bv2-smoke-r7 notify-subscribe t_7fc05eca --platform discord --chat-id 1561469136796782633 --chat-type channel --delivery-mode notify+wake   # exit 0
$ hermes kanban --board bv2-smoke-r7 edit t_7fc05eca --auto-handle true   # exit 0
$ hermes kanban --board bv2-smoke-r7 create 'bv2 smoke s1: forced child failure -> warning' --session 20261018_130347_87039e56 --assignee human:apollo --json   # exit 0
$ hermes kanban --board bv2-smoke-r7 notify-subscribe t_dcfcc7f0 --platform discord --chat-id 1561469136796782633 --chat-type channel --delivery-mode notify+wake   # exit 0
$ hermes kanban --board bv2-smoke-r7 edit t_dcfcc7f0 --auto-handle true   # exit 0
$ hermes kanban --board bv2-smoke-r7 complete t_78080b3d --summary 'bv2 smoke: forced completed event'   # exit 0
$ hermes kanban --board bv2-smoke-r7 block t_7fc05eca --kind needs_input 'bv2 smoke: forced needs_input event. unblock_when: after:+1d'   # exit 0
$ hermes kanban --board bv2-smoke-r7 edit t_dcfcc7f0 --session 20991231_000000_bv2fault   # exit 0
$ hermes kanban --board bv2-smoke-r7 complete t_dcfcc7f0 --summary 'bv2 smoke: forced completed event'   # exit 0
```

### Raw output

```text
--- board_create (exit 0)
Board 'bv2-smoke-r7' created.
  Display name: BV2 smoke (scratch)
  DB path:      /Users/alexgierczyk/.hermes/kanban/boards/bv2-smoke-r7/kanban.db
  Use `hermes kanban boards switch bv2-smoke-r7` to make it current.
--- create (exit 0)
{
  "id": "t_78080b3d",
  "title": "bv2 smoke s1: completed -> auto-handled",
  "body": "origin: cli · session 20261018_130347_87039e56 · 2026-10-19",
  "assignee": "human:apollo",
  "status": "ready",
  "priority": 0,
  "tenant": null,
  "workspace_kind": "scratch",
  "workspace_path": null,
  "branch_name": null,
  "project_id": null,
  "created_by": "user",
  "created_at": 1792452781,
  "started_at": null,
  "completed_at": null,
  "result": null,
  "skills": [],
  "max_runtime_seconds": null,
  "max_retries": null,
  "model_override": null,
  "provider_override": null,
  "session_id": "20261018_130347_87039e56",
  "workflow_template_id": null,
  "current_step_key": null,
  "completion_contract": "local-only",
  "last_failure_error": null,
  "reasoning_effort": null,
  "brain": null,
  "pin_sub_reason": null,
  "pin_sub_fallback": false,
  "unhomed": false,
  "no_worker": false
}
--- notify_subscribe (exit 0)
Subscribed discord:1561469136796782633 to t_78080b3d
--- set_auto_handle (exit 0)
t_78080b3d: auto-handle: true
note: no session identity — home-session guard skipped
--- create (exit 0)
{
  "id": "t_7fc05eca",
  "title": "bv2 smoke s1: needs_input -> surface",
  "body": "origin: cli · session 20261018_130347_87039e56 · 2026-10-19",
  "assignee": "human:apollo",
  "status": "ready",
  "priority": 0,
  "tenant": null,
  "workspace_kind": "scratch",
  "workspace_path": null,
  "branch_name": null,
  "project_id": null,
  "created_by": "user",
  "created_at": 1792452785,
  "started_at": null,
  "completed_at": null,
  "result": null,
  "skills": [],
  "max_runtime_seconds": null,
  "max_retries": null,
  "model_override": null,
  "provider_override": null,
  "session_id": "20261018_130347_87039e56",
  "workflow_template_id": null,
  "current_step_key": null,
  "completion_contract": "local-only",
  "last_failure_error": null,
  "reasoning_effort": null,
  "brain": null,
  "pin_sub_reason": null,
  "pin_sub_fallback": false,
  "unhomed": false,
  "no_worker": false
}
--- notify_subscribe (exit 0)
Subscribed discord:1561469136796782633 to t_7fc05eca
--- set_auto_handle (exit 0)
t_7fc05eca: auto-handle: true
note: no session identity — home-session guard skipped
--- create (exit 0)
{
  "id": "t_dcfcc7f0",
  "title": "bv2 smoke s1: forced child failure -> warning",
  "body": "origin: cli · session 20261018_130347_87039e56 · 2026-10-19",
  "assignee": "human:apollo",
  "status": "ready",
  "priority": 0,
  "tenant": null,
  "workspace_kind": "scratch",
  "workspace_path": null,
  "branch_name": null,
  "project_id": null,
  "created_by": "user",
  "created_at": 1792452789,
  "started_at": null,
  "completed_at": null,
  "result": null,
  "skills": [],
  "max_runtime_seconds": null,
  "max_retries": null,
  "model_override": null,
  "provider_override": null,
  "session_id": "20261018_130347_87039e56",
  "workflow_template_id": null,
  "current_step_key": null,
  "completion_contract": "local-only",
  "last_failure_error": null,
  "reasoning_effort": null,
  "brain": null,
  "pin_sub_reason": null,
  "pin_sub_fallback": false,
  "unhomed": false,
  "no_worker": false
}
--- notify_subscribe (exit 0)
Subscribed discord:1561469136796782633 to t_dcfcc7f0
--- set_auto_handle (exit 0)
t_dcfcc7f0: auto-handle: true
note: no session identity — home-session guard skipped
--- complete (exit 0)
Completed t_78080b3d
note: no session identity — home-session guard skipped
--- block_needs_input (exit 0)
Blocked t_7fc05eca: bv2 smoke: forced needs_input event. unblock_when: after:+1d
note: no session identity — home-session guard skipped
--- inject_failure (exit 0)
Home session of t_dcfcc7f0: 20991231_000000_bv2fault
note: no session identity — home-session guard skipped
--- complete (exit 0)
Completed t_dcfcc7f0
note: no session identity — home-session guard skipped
--- chat match
[auto-handled] t_78080b3d — Smoke scratch card t_78080b3d finished with the expected forced-completed summary; accept the completion (delegation del-20261019-163315-237d).
--- log match
2026-10-19 16:33:15,520 INFO gateway.kanban_auto_handle: kanban notifier: auto-handled t_78080b3d disposition=auto row=completed path=notifier delegation=del-20261019-163315-237d
--- chat match
⚠️ auto-handle failed t_dcfcc7f0: child failed: dispatch failed: home session not live here
```

### Checks

| # | check | result | detail |
|---|---|---|---|
| 1 | origin chat: `[auto-handled] t_78080b3d - <Outcome>` | PASS | [auto-handled] t_78080b3d — Smoke scratch card t_78080b3d finished with the expected forced-completed summary; accept the completion (delegation del-20261019-163315-237d). |
| 2 | gateway log: `kanban notifier: auto-handled t_78080b3d disposition=auto` | PASS | 2026-10-19 16:33:15,520 INFO gateway.kanban_auto_handle: kanban notifier: auto-handled t_78080b3d disposition=auto row=completed path=notifier delegation=del-20261019-163315-237d |
| 3 | needs_input card t_7fc05eca: no `[auto-handled]` line | PASS | none (waited 20s) |
| 4 | needs_input card t_7fc05eca: notifier disposition is not auto | PASS | no auto-handled log line (surfaced as today) |
| 5 | origin chat: `warning auto-handle failed t_dcfcc7f0: <reason>` | PASS | ⚠️ auto-handle failed t_dcfcc7f0: child failed: dispatch failed: home session not live here |

<!-- bv2-smoke:step1:end -->

<!-- bv2-smoke:step2:begin -->
## Step 2: kill switches take effect on the next tick (AC-8, INV-7)

- mode: `live`  started: 2026-10-19T16:40:42-07:00  finished: 2026-10-19T16:42:27-07:00
- verdict: **PASS**
- the harness never edits config: it prints each one-line diff for Apollo and waits on the ack file
- card `s2_fork_off` = `t_307a459e`
- fork_enabled: event forced 2026-10-19T16:40:46-07:00, downgrade logged 2026-10-19T16:41:21-07:00 (34.5s; tick 5s)
- card `s2_auto_off` = `t_cd55f96a`

### Commands

```text
# APOLLO (manual): config.yaml: delegation.boomerang.fork_enabled: true -> false   ack: fork_enabled=false
$ hermes kanban boards create bv2-smoke-r8 --name 'BV2 smoke (scratch)' --description 'Boomerang v2 smoke harness scratch board (t_989cde1b); safe to archive'   # exit 0
$ hermes kanban --board bv2-smoke-r8 create 'bv2 smoke s2: fork_enabled=false -> summary' --session 20261018_130347_87039e56 --assignee human:apollo --json   # exit 0
$ hermes kanban --board bv2-smoke-r8 notify-subscribe t_307a459e --platform discord --chat-id 1561469136796782633 --chat-type channel --delivery-mode notify+wake   # exit 0
$ hermes kanban --board bv2-smoke-r8 edit t_307a459e --auto-handle true   # exit 0
$ hermes kanban --board bv2-smoke-r8 complete t_307a459e --summary 'bv2 smoke: forced completed event'   # exit 0
# APOLLO (manual): config.yaml: delegation.boomerang.auto_handle_enabled: true -> false   ack: auto_handle_enabled=false
$ hermes kanban --board bv2-smoke-r8 create 'bv2 smoke s2: auto_handle_enabled=false -> surface' --session 20261018_130347_87039e56 --assignee human:apollo --json   # exit 0
$ hermes kanban --board bv2-smoke-r8 notify-subscribe t_cd55f96a --platform discord --chat-id 1561469136796782633 --chat-type channel --delivery-mode notify+wake   # exit 0
$ hermes kanban --board bv2-smoke-r8 edit t_cd55f96a --auto-handle true   # exit 0
$ hermes kanban --board bv2-smoke-r8 complete t_cd55f96a --summary 'bv2 smoke: forced completed event'   # exit 0
# APOLLO (manual): restore delegation.boomerang.fork_enabled: true and delegation.boomerang.auto_handle_enabled: true   ack: restore
```

### Raw output

```text
--- config stage 0
delegation.boomerang.auto_handle_enabled=True delegation.boomerang.fork_enabled=True
--- ack request
config.yaml: delegation.boomerang.fork_enabled: true -> false
ack: echo fork_enabled=false >> /Users/alexgierczyk/.hermes/var/bv2-smoke/ack
--- board_create (exit 0)
Board 'bv2-smoke-r8' created.
  Display name: BV2 smoke (scratch)
  DB path:      /Users/alexgierczyk/.hermes/kanban/boards/bv2-smoke-r8/kanban.db
  Use `hermes kanban boards switch bv2-smoke-r8` to make it current.
--- create (exit 0)
{
  "id": "t_307a459e",
  "title": "bv2 smoke s2: fork_enabled=false -> summary",
  "body": "origin: cli · session 20261018_130347_87039e56 · 2026-10-19",
  "assignee": "human:apollo",
  "status": "ready",
  "priority": 0,
  "tenant": null,
  "workspace_kind": "scratch",
  "workspace_path": null,
  "branch_name": null,
  "project_id": null,
  "created_by": "user",
  "created_at": 1792453249,
  "started_at": null,
  "completed_at": null,
  "result": null,
  "skills": [],
  "max_runtime_seconds": null,
  "max_retries": null,
  "model_override": null,
  "provider_override": null,
  "session_id": "20261018_130347_87039e56",
  "workflow_template_id": null,
  "current_step_key": null,
  "completion_contract": "local-only",
  "last_failure_error": null,
  "reasoning_effort": null,
  "brain": null,
  "pin_sub_reason": null,
  "pin_sub_fallback": false,
  "unhomed": false,
  "no_worker": false
}
--- notify_subscribe (exit 0)
Subscribed discord:1561469136796782633 to t_307a459e
--- set_auto_handle (exit 0)
t_307a459e: auto-handle: true
note: no session identity — home-session guard skipped
--- complete (exit 0)
Completed t_307a459e
note: no session identity — home-session guard skipped
--- log match
2026-10-19 16:41:21,098 INFO tools.delegate_tool: boomerang.fork_ineligible reason=fork_enabled parent_session=20261018_130347_87039e56
--- ack request
config.yaml: delegation.boomerang.auto_handle_enabled: true -> false
ack: echo auto_handle_enabled=false >> /Users/alexgierczyk/.hermes/var/bv2-smoke/ack
--- create (exit 0)
{
  "id": "t_cd55f96a",
  "title": "bv2 smoke s2: auto_handle_enabled=false -> surface",
  "body": "origin: cli · session 20261018_130347_87039e56 · 2026-10-19",
  "assignee": "human:apollo",
  "status": "ready",
  "priority": 0,
  "tenant": null,
  "workspace_kind": "scratch",
  "workspace_path": null,
  "branch_name": null,
  "project_id": null,
  "created_by": "user",
  "created_at": 1792453318,
  "started_at": null,
  "completed_at": null,
  "result": null,
  "skills": [],
  "max_runtime_seconds": null,
  "max_retries": null,
  "model_override": null,
  "provider_override": null,
  "session_id": "20261018_130347_87039e56",
  "workflow_template_id": null,
  "current_step_key": null,
  "completion_contract": "local-only",
  "last_failure_error": null,
  "reasoning_effort": null,
  "brain": null,
  "pin_sub_reason": null,
  "pin_sub_fallback": false,
  "unhomed": false,
  "no_worker": false
}
--- notify_subscribe (exit 0)
Subscribed discord:1561469136796782633 to t_cd55f96a
--- set_auto_handle (exit 0)
t_cd55f96a: auto-handle: true
note: no session identity — home-session guard skipped
--- complete (exit 0)
Completed t_cd55f96a
note: no session identity — home-session guard skipped
--- ack request
config.yaml: restore delegation.boomerang.fork_enabled: false -> true and delegation.boomerang.auto_handle_enabled: false -> true
ack: echo restore >> /Users/alexgierczyk/.hermes/var/bv2-smoke/ack
```

### Checks

| # | check | result | detail |
|---|---|---|---|
| 1 | precondition: auto_handle_enabled=true and fork_enabled=true | PASS | auto_handle_enabled=True fork_enabled=True |
| 2 | ack `fork_enabled=false` received | PASS | /Users/alexgierczyk/.hermes/var/bv2-smoke/ack |
| 3 | config reads delegation.boomerang.fork_enabled=false after ack | PASS | read false (stage 1) |
| 4 | gateway log: `boomerang.fork_ineligible reason=fork_enabled` after the flip | PASS | 2026-10-19 16:41:21,098 INFO tools.delegate_tool: boomerang.fork_ineligible reason=fork_enabled parent_session=20261018_130347_87039e56 |
| 5 | origin chat: `[auto-handled] t_307a459e` still posts (summary-mode child) | PASS | [auto-handled] t_307a459e — Smoke step 2 fork-off scratch card completed with the expected forced summary; fork_enabled=false downgrade logged; accept the completion. |
| 6 | ack `auto_handle_enabled=false` received | PASS | /Users/alexgierczyk/.hermes/var/bv2-smoke/ack |
| 7 | config reads delegation.boomerang.auto_handle_enabled=false after ack | PASS | read false (stage 2) |
| 8 | card t_cd55f96a: not auto-handled within one tick of the flip | PASS | no auto-handle (waited 20s) |
| 9 | ack `restore` received | PASS | /Users/alexgierczyk/.hermes/var/bv2-smoke/ack |
| 10 | config restored: both switches true | PASS | stage 3 |

<!-- bv2-smoke:step2:end -->

<!-- bv2-smoke:step3:begin -->
## Step 3: no x-hermes-* header upstream on a fork turn (AC-9, INV-8)

- mode: `live`  started: 2026-10-19T22:58:59-07:00  finished: 2026-10-19T23:06:06-07:00
- verdict: **PASS**
- wire capture root `/Users/alexgierczyk/.claude-wirelog-fleet` (relay-wire-forensics apx/bpx JSONL schema)
- card `s3_fork` = `t_41518181`
- served by `sub-vps-1`: its capture is on that box; run `wirelog-fleet.py sync` and point BV2_WIRELOG_DIR at ~/.claude-wirelog-fleet before trusting step 3

### Commands

```text
$ python3 ~/.hermes/scripts/wirelog-coverage-check.py   # confirm the serving lane is capturing before trusting a clean result
$ hermes kanban boards create bv2-smoke-r12 --name 'BV2 smoke (scratch)' --description 'Boomerang v2 smoke harness scratch board (t_989cde1b); safe to archive'   # exit 0
$ hermes kanban --board bv2-smoke-r12 create 'bv2 smoke s3: one fork turn for wire capture' --session 20261018_130347_87039e56 --assignee human:apollo --json   # exit 0
$ hermes kanban --board bv2-smoke-r12 notify-subscribe t_41518181 --platform discord --chat-id 1561469136796782633 --chat-type channel --delivery-mode notify+wake   # exit 0
$ hermes kanban --board bv2-smoke-r12 edit t_41518181 --auto-handle true   # exit 0
$ hermes kanban --board bv2-smoke-r12 complete t_41518181 --summary 'bv2 smoke: forced completed event'   # exit 0
```

### Raw output

```text
--- board_create (exit 0)
Board 'bv2-smoke-r12' created.
  Display name: BV2 smoke (scratch)
  DB path:      /Users/alexgierczyk/.hermes/kanban/boards/bv2-smoke-r12/kanban.db
  Use `hermes kanban boards switch bv2-smoke-r12` to make it current.
--- create (exit 0)
{
  "id": "t_41518181",
  "title": "bv2 smoke s3: one fork turn for wire capture",
  "body": "origin: cli · session 20261018_130347_87039e56 · 2026-10-19",
  "assignee": "human:apollo",
  "status": "ready",
  "priority": 0,
  "tenant": null,
  "workspace_kind": "scratch",
  "workspace_path": null,
  "branch_name": null,
  "project_id": null,
  "created_by": "user",
  "created_at": 1792475941,
  "started_at": null,
  "completed_at": null,
  "result": null,
  "skills": [],
  "max_runtime_seconds": null,
  "max_retries": null,
  "model_override": null,
  "provider_override": null,
  "session_id": "20261018_130347_87039e56",
  "workflow_template_id": null,
  "current_step_key": null,
  "completion_contract": "local-only",
  "last_failure_error": null,
  "reasoning_effort": null,
  "brain": null,
  "pin_sub_reason": null,
  "pin_sub_fallback": false,
  "unhomed": false,
  "no_worker": false
}
--- notify_subscribe (exit 0)
Subscribed discord:1561469136796782633 to t_41518181
--- set_auto_handle (exit 0)
t_41518181: auto-handle: true
note: no session identity — home-session guard skipped
--- complete (exit 0)
Completed t_41518181
note: no session identity — home-session guard skipped
--- log match
2026-10-19 22:59:27,011 INFO [20261019_225910_d3f0c8] tools.delegate_tool: boomerang.fork served_by=sub-vps-1 read_share=0.9845 prefix_tokens=340825 child_session=20261019_225910_d3f0c8
--- upstream header scan
916331-6964 lane=apx sub=1 headers=42
916331-6965 lane=apx sub=1 headers=42
916331-6966 lane=apx sub=1 headers=42
916331-6967 lane=apx sub=1 headers=42
916331-6968 lane=apx sub=1 headers=42
916331-6971 lane=apx sub=1 headers=42
916331-6972 lane=apx sub=1 headers=42
916331-6970 lane=apx sub=1 headers=42
916331-6973 lane=apx sub=1 headers=42
916331-6974 lane=apx sub=1 headers=42
916331-6969 lane=apx sub=1 headers=42
916333-1857 lane=apx sub=1 headers=42
916333-1858 lane=apx sub=1 headers=42
916333-1859 lane=apx sub=1 headers=42
916331-6975 lane=apx sub=1 headers=42
916331-6976 lane=apx sub=1 headers=42
916333-1860 lane=apx sub=1 headers=42
916333-1861 lane=apx sub=1 headers=42
916333-1862 lane=apx sub=1 headers=42
916333-1863 lane=apx sub=1 headers=42
916333-1864 lane=apx sub=1 headers=42
916333-1865 lane=apx sub=1 headers=42
916333-1866 lane=apx sub=1 headers=42
916333-1868 lane=apx sub=1 headers=42
916331-6977 lane=apx sub=1 headers=42
```

### Checks

| # | check | result | detail |
|---|---|---|---|
| 1 | gateway log: `boomerang.fork served_by=<seat>` (a fork turn happened) | PASS | 2026-10-19 22:59:27,011 INFO [20261019_225910_d3f0c8] tools.delegate_tool: boomerang.fork served_by=sub-vps-1 read_share=0.9845 prefix_tokens=340825 child_session=20261019_225910_d3f0c8 |
| 2 | wire records captured in the fork-turn window | PASS | 25 record(s), seat `sub-vps-1` |
| 3 | no `x-hermes-*` header in any upstream request | PASS | 0/25 records carry one |

<!-- bv2-smoke:step3:end -->

<!-- bv2-smoke:step4:begin -->
## Step 4: restart drill observer: reconciler re-surfaces within one tick (AC-11, 5.7)

- mode: `live`  started: 2026-10-20T00:02:53-07:00  finished: 2026-10-20T00:05:51-07:00
- verdict: **FAIL**
- the harness never restarts anything; gateway pid file `/Users/alexgierczyk/.hermes/gateway.pid` boot_id before: `39855:1792476429.477781`
- card `s4_restart` = `t_1b09baf2`

### Commands

```text
$ hermes kanban boards create bv2-smoke-r14 --name 'BV2 smoke (scratch)' --description 'Boomerang v2 smoke harness scratch board (t_989cde1b); safe to archive'   # exit 0
$ hermes kanban --board bv2-smoke-r14 create 'bv2 smoke s4: child in flight across a gateway restart' --session 20261018_130347_87039e56 --assignee human:apollo --json   # exit 0
$ hermes kanban --board bv2-smoke-r14 notify-subscribe t_1b09baf2 --platform discord --chat-id 1561469136796782633 --chat-type channel --delivery-mode notify+wake   # exit 0
$ hermes kanban --board bv2-smoke-r14 edit t_1b09baf2 --auto-handle true   # exit 0
$ hermes kanban --board bv2-smoke-r14 edit t_1b09baf2 --body 'bv2 smoke s4 restart-drill fixture (t_989cde1b, board bv2-smoke-r14). Accepting this forced completion requires these checks, each run and quoted in your evidence: (1) `hermes kanban --board bv2-smoke-r14 show t_1b09baf2`; (2) `hermes kanban --board bv2-smoke-r14 ls`; (3) `git -C ~/.hermes/runtime/hermes-agent log -1 --format='"'"'%H %s'"'"'`; (4) `test -f ~/.hermes/var/bv2-smoke/plan.json && echo plan-present`; (5) `python3 ~/.hermes/scripts/wirelog-coverage-check.py` (it takes about a minute; quote its last 3 lines). If all five ran, accept the completion.'   # exit 0
$ hermes kanban --board bv2-smoke-r14 complete t_1b09baf2 --summary 'bv2 smoke: forced completed event'   # exit 0
# APOLLO (manual): restart the gateway NOW via safe-gateway-restart (single-use approval), while the auto-handle child for t_1b09baf2 is still running   ack: restart
```

### Raw output

```text
--- board_create (exit 0)
Board 'bv2-smoke-r14' created.
  Display name: BV2 smoke (scratch)
  DB path:      /Users/alexgierczyk/.hermes/kanban/boards/bv2-smoke-r14/kanban.db
  Use `hermes kanban boards switch bv2-smoke-r14` to make it current.
--- create (exit 0)
{
  "id": "t_1b09baf2",
  "title": "bv2 smoke s4: child in flight across a gateway restart",
  "body": "origin: cli · session 20261018_130347_87039e56 · 2026-10-20",
  "assignee": "human:apollo",
  "status": "ready",
  "priority": 0,
  "tenant": null,
  "workspace_kind": "scratch",
  "workspace_path": null,
  "branch_name": null,
  "project_id": null,
  "created_by": "user",
  "created_at": 1792479775,
  "started_at": null,
  "completed_at": null,
  "result": null,
  "skills": [],
  "max_runtime_seconds": null,
  "max_retries": null,
  "model_override": null,
  "provider_override": null,
  "session_id": "20261018_130347_87039e56",
  "workflow_template_id": null,
  "current_step_key": null,
  "completion_contract": "local-only",
  "last_failure_error": null,
  "reasoning_effort": null,
  "brain": null,
  "pin_sub_reason": null,
  "pin_sub_fallback": false,
  "unhomed": false,
  "no_worker": false
}
--- notify_subscribe (exit 0)
Subscribed discord:1561469136796782633 to t_1b09baf2
--- set_auto_handle (exit 0)
t_1b09baf2: auto-handle: true
note: no session identity — home-session guard skipped
--- s4_hold (exit 0)
Edited t_1b09baf2
note: no session identity — home-session guard skipped
--- complete (exit 0)
Completed t_1b09baf2
note: no session identity — home-session guard skipped
--- log match
2026-10-20 00:03:34,759 INFO gateway.kanban_auto_handle: kanban notifier: auto-handled t_1b09baf2 disposition=auto row=completed path=notifier delegation=del-20261020-000334-9d80
--- ack request
restart the gateway NOW via safe-gateway-restart (single-use approval), while the auto-handle child for t_1b09baf2 is still running
ack: echo restart >> /Users/alexgierczyk/.hermes/var/bv2-smoke/ack
--- log match
2026-10-20 00:05:02,703 INFO gateway.run: Gateway running with 5 platform(s)
```

### Checks

| # | check | result | detail |
|---|---|---|---|
| 1 | auto-handle child dispatched for t_1b09baf2 (disposition=auto) | PASS | 2026-10-20 00:03:34,759 INFO gateway.kanban_auto_handle: kanban notifier: auto-handled t_1b09baf2 disposition=auto row=completed path=notifier delegation=del-20261020-000334-9d80 |
| 2 | ack `restart` received | PASS | /Users/alexgierczyk/.hermes/var/bv2-smoke/ack |
| 3 | gateway restart observed in the log | PASS | 2026-10-20 00:05:02,703 INFO gateway.run: Gateway running with 5 platform(s) |
| 4 | gateway boot_id changed | PASS | 39855:1792476429.477781 -> 19670:1792479871.777492 |
| 5 | reconciler re-surfaced the wake for t_1b09baf2 | FAIL | no line matching /kanban auto-handle: .*t_1b09baf2\|auto-handle lost t_1b09baf2/ |
| 6 | origin chat: `warning auto-handle lost t_1b09baf2 (reason: gateway restart)` | PASS | ⚠️ auto-handle lost t_1b09baf2 (reason: gateway restart) |
| 7 | no late `[auto-handled] t_1b09baf2` within waited 20s after the re-surface | PASS | none |

<!-- bv2-smoke:step4:end -->
