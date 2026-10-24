# Boomerang v2 smoke evidence: ISOLATED scratch gateway (puppet lane)

Written by `tools/dev_probes/boomerang_v2_smoke/run.py --live` driven by hermes-home
`skills-shared/hermes-harness/puppet-lane/scripts/bv2-smoke-lane.sh` (t_b901c171). The gateway under test
is a scratch `HERMES_HOME` running `gateway run` from this worktree, wearing the Aegis bot in a #daedalus
thread; its own kanban board (`HERMES_KANBAN_SANDBOX=1`), its own home session, the step-2 flips on the
scratch config.yaml, the step-4 restart a SIGTERM + relaunch of that process. No launchd gateway was touched.

- Steps 1, 2, 4: run `pl-1020044514`, scratch code `7ddb3450504b7ed74bf9e52a122b911e80c13373` (fork/main c679ce97 + BV2_ACK_CMD), 2026-10-20 04:45-06:00 PT, Studio load ~75.
- Step 3: run `pl-1020081834`, scratch code `2069c63a9ba7f277b8792e067be3709cfe2b5c62` (fork/main 2ed05c61 incl. #2159 + this PR), 2026-10-20 08:18-09:1x PT. Two earlier step-3 runs were INCOMPLETE: `pl-1020062840` (wirelog_sync > 600 s hard cap) and `pl-1020072034` (canary PASS on sub-19, fork seat sub-vps-9 never synced).
- Zero-spill (puppet-lane teardown): PASS on every run (live turns.db=0, aegis turns.db=0, live/aegis sessions.json keys=0).
- Launchd gateway boot_ids, read before and after inside each run: UNCHANGED (run 3: default 19670:1792479871.777492, aegis 79485:1792479746.396528, argus 10081:1792491467.706466, athena 11424:1792491471.53408, cadmus 13856:1792491475.42258, chiron 16926:1792491480.160911, daedalus-opus 18649:1792491483.335642, daedalus 20432:1792491486.365672, momus 23012:1792491490.527867; run 6: default 19670:1792479871.777492, aegis 67232:1792503110.049519, argus 68228:1792503113.620851, athena 69712:1792503118.116972, cadmus 70816:1792503121.891355, chiron 71924:1792503126.647972, daedalus-opus 72658:1792503129.506035, daedalus 73664:1792503132.395199, momus 75779:1792503137.194615; the profile gateways moved between the two runs by a fleet deploy at ~06:51 PT, not by the lane).

<!-- bv2-smoke:step1:begin -->
## Step 1: auto-handle origin-chat lines (AC-7, INV-5, INV-6)

- mode: `live`  started: 2026-10-20T04:47:44-07:00  finished: 2026-10-20T04:53:38-07:00
- verdict: **PASS**
- board `bv2-smoke-pl`; gateway log `/Volumes/fleet-scratch/tmp/pl-1020044514/home/logs/gateway.log`; origin chat `discord:1562069703856496752`
- card `s1_completed` = `t_e42ebe9e`
- card `s1_needs_input` = `t_409a2b60`
- card `s1_failure` = `t_3c21146b`

### Commands

```text
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh boards create bv2-smoke-pl --name 'BV2 smoke (scratch)' --description 'Boomerang v2 smoke harness scratch board (t_989cde1b); safe to archive'   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl create 'bv2 smoke s1: completed -> auto-handled' --session 20261020_044727_01aab3eb --assignee human:apollo --json   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl notify-subscribe t_e42ebe9e --platform discord --chat-id 1562069703856496752 --chat-type channel --delivery-mode notify+wake   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl edit t_e42ebe9e --auto-handle true   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl create 'bv2 smoke s1: needs_input -> surface' --session 20261020_044727_01aab3eb --assignee human:apollo --json   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl notify-subscribe t_409a2b60 --platform discord --chat-id 1562069703856496752 --chat-type channel --delivery-mode notify+wake   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl edit t_409a2b60 --auto-handle true   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl create 'bv2 smoke s1: forced child failure -> warning' --session 20261020_044727_01aab3eb --assignee human:apollo --json   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl notify-subscribe t_3c21146b --platform discord --chat-id 1562069703856496752 --chat-type channel --delivery-mode notify+wake   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl edit t_3c21146b --auto-handle true   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl complete t_e42ebe9e --summary 'bv2 smoke: forced completed event'   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl block t_409a2b60 --kind needs_input 'bv2 smoke: forced needs_input event. unblock_when: after:+1d'   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl edit t_3c21146b --session 20991231_000000_bv2fault   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl complete t_3c21146b --summary 'bv2 smoke: forced completed event'   # exit 0
```

### Raw output

```text
--- board_create (exit 0)
Board 'bv2-smoke-pl' created.
  Display name: BV2 smoke (scratch)
  DB path:      /Volumes/fleet-scratch/tmp/pl-1020044514/home/kanban/boards/bv2-smoke-pl/kanban.db
  Use `hermes kanban boards switch bv2-smoke-pl` to make it current.
--- create (exit 0)
{
  "id": "t_e42ebe9e",
  "title": "bv2 smoke s1: completed -> auto-handled",
  "body": "origin: cli · session 20261020_044727_01aab3eb · 2026-10-20",
  "assignee": "human:apollo",
  "status": "ready",
  "priority": 0,
  "tenant": null,
  "workspace_kind": "scratch",
  "workspace_path": null,
  "branch_name": null,
  "project_id": null,
  "created_by": "user",
  "created_at": 1792496913,
  "started_at": null,
  "completed_at": null,
  "result": null,
  "skills": [],
  "max_runtime_seconds": null,
  "max_retries": null,
  "model_override": null,
  "provider_override": null,
  "session_id": "20261020_044727_01aab3eb",
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
Subscribed discord:1562069703856496752 to t_e42ebe9e
--- set_auto_handle (exit 0)
t_e42ebe9e: auto-handle: true
note: no session identity — home-session guard skipped
--- create (exit 0)
{
  "id": "t_409a2b60",
  "title": "bv2 smoke s1: needs_input -> surface",
  "body": "origin: cli · session 20261020_044727_01aab3eb · 2026-10-20",
  "assignee": "human:apollo",
  "status": "ready",
  "priority": 0,
  "tenant": null,
  "workspace_kind": "scratch",
  "workspace_path": null,
  "branch_name": null,
  "project_id": null,
  "created_by": "user",
  "created_at": 1792497022,
  "started_at": null,
  "completed_at": null,
  "result": null,
  "skills": [],
  "max_runtime_seconds": null,
  "max_retries": null,
  "model_override": null,
  "provider_override": null,
  "session_id": "20261020_044727_01aab3eb",
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
Subscribed discord:1562069703856496752 to t_409a2b60
--- set_auto_handle (exit 0)
t_409a2b60: auto-handle: true
note: no session identity — home-session guard skipped
--- create (exit 0)
{
  "id": "t_3c21146b",
  "title": "bv2 smoke s1: forced child failure -> warning",
  "body": "origin: cli · session 20261020_044727_01aab3eb · 2026-10-20",
  "assignee": "human:apollo",
  "status": "ready",
  "priority": 0,
  "tenant": null,
  "workspace_kind": "scratch",
  "workspace_path": null,
  "branch_name": null,
  "project_id": null,
  "created_by": "user",
  "created_at": 1792497080,
  "started_at": null,
  "completed_at": null,
  "result": null,
  "skills": [],
  "max_runtime_seconds": null,
  "max_retries": null,
  "model_override": null,
  "provider_override": null,
  "session_id": "20261020_044727_01aab3eb",
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
Subscribed discord:1562069703856496752 to t_3c21146b
--- set_auto_handle (exit 0)
t_3c21146b: auto-handle: true
note: no session identity — home-session guard skipped
--- complete (exit 0)
Completed t_e42ebe9e
note: no session identity — home-session guard skipped
--- block_needs_input (exit 0)
Blocked t_409a2b60: bv2 smoke: forced needs_input event. unblock_when: after:+1d
note: no session identity — home-session guard skipped
--- inject_failure (exit 0)
Home session of t_3c21146b: 20991231_000000_bv2fault
note: no session identity — home-session guard skipped
--- complete (exit 0)
Completed t_3c21146b
note: no session identity — home-session guard skipped
--- chat match
[auto-handled] t_e42ebe9e — Smoke card t_e42ebe9e is legitimately done via forced completed event with auto_handle set; board state consistent, no write needed.
--- log match
2026-10-20 04:52:00,868 INFO gateway.kanban_auto_handle: kanban notifier: auto-handled t_e42ebe9e disposition=auto row=completed path=notifier delegation=del-20261020-045200-be12
--- chat match
⚠️ auto-handle failed t_3c21146b: child failed: dispatch failed: home session not live here
```

### Checks

| # | check | result | detail |
|---|---|---|---|
| 1 | origin chat: `[auto-handled] t_e42ebe9e - <Outcome>` | PASS | [auto-handled] t_e42ebe9e — Smoke card t_e42ebe9e is legitimately done via forced completed event with auto_handle set; board state consistent, no write needed. |
| 2 | gateway log: `kanban notifier: auto-handled t_e42ebe9e disposition=auto` | PASS | 2026-10-20 04:52:00,868 INFO gateway.kanban_auto_handle: kanban notifier: auto-handled t_e42ebe9e disposition=auto row=completed path=notifier delegation=del-20261020-045200-be12 |
| 3 | needs_input card t_409a2b60: no `[auto-handled]` line | PASS | none (waited 20s) |
| 4 | needs_input card t_409a2b60: notifier disposition is not auto | PASS | no auto-handled log line (surfaced as today) |
| 5 | origin chat: `warning auto-handle failed t_3c21146b: <reason>` | PASS | ⚠️ auto-handle failed t_3c21146b: child failed: dispatch failed: home session not live here |

<!-- bv2-smoke:step1:end -->

<!-- bv2-smoke:step2:begin -->
## Step 2: kill switches take effect on the next tick (AC-8, INV-7)

- mode: `live`  started: 2026-10-20T04:53:38-07:00  finished: 2026-10-20T04:58:13-07:00
- verdict: **PASS**
- the harness never edits config: it prints each one-line diff for Apollo and waits on the ack file
- card `s2_fork_off` = `t_f79d72f9`
- fork_enabled: event forced 2026-10-20T04:53:39-07:00, downgrade logged 2026-10-20T04:54:42-07:00 (62.8s; tick 5s)
- card `s2_auto_off` = `t_c4b945db`

### Commands

```text
# APOLLO (manual): config.yaml: delegation.boomerang.fork_enabled: true -> false   ack: fork_enabled=false
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-ack.sh fork_enabled=false   # scripted ack, exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh boards create bv2-smoke-pl --name 'BV2 smoke (scratch)' --description 'Boomerang v2 smoke harness scratch board (t_989cde1b); safe to archive'   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl create 'bv2 smoke s2: fork_enabled=false -> summary' --session 20261020_044727_01aab3eb --assignee human:apollo --json   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl notify-subscribe t_f79d72f9 --platform discord --chat-id 1562069703856496752 --chat-type channel --delivery-mode notify+wake   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl edit t_f79d72f9 --auto-handle true   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl complete t_f79d72f9 --summary 'bv2 smoke: forced completed event'   # exit 0
# APOLLO (manual): config.yaml: delegation.boomerang.auto_handle_enabled: true -> false   ack: auto_handle_enabled=false
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-ack.sh auto_handle_enabled=false   # scripted ack, exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl create 'bv2 smoke s2: auto_handle_enabled=false -> surface' --session 20261020_044727_01aab3eb --assignee human:apollo --json   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl notify-subscribe t_c4b945db --platform discord --chat-id 1562069703856496752 --chat-type channel --delivery-mode notify+wake   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl edit t_c4b945db --auto-handle true   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl complete t_c4b945db --summary 'bv2 smoke: forced completed event'   # exit 0
# APOLLO (manual): restore delegation.boomerang.fork_enabled: true and delegation.boomerang.auto_handle_enabled: true   ack: restore
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-ack.sh restore   # scripted ack, exit 0
```

### Raw output

```text
--- config stage 0
delegation.boomerang.auto_handle_enabled=True delegation.boomerang.fork_enabled=True
--- ack request
config.yaml: delegation.boomerang.fork_enabled: true -> false
ack: echo fork_enabled=false >> /Volumes/fleet-scratch/tmp/pl-1020044514/bv2.ack
--- ack cmd fork_enabled=false (exit 0)
bv2-ack: /Volumes/fleet-scratch/tmp/pl-1020044514/home/config.yaml: delegation.boomerang.fork_enabled = false
--- board_create (exit 0)
Board 'bv2-smoke-pl' already exists.
  Display name: BV2 smoke (scratch)
  DB path:      /Volumes/fleet-scratch/tmp/pl-1020044514/home/kanban/boards/bv2-smoke-pl/kanban.db
  Use `hermes kanban boards switch bv2-smoke-pl` to make it current.
--- create (exit 0)
{
  "id": "t_f79d72f9",
  "title": "bv2 smoke s2: fork_enabled=false -> summary",
  "body": "origin: cli · session 20261020_044727_01aab3eb · 2026-10-20",
  "assignee": "human:apollo",
  "status": "ready",
  "priority": 0,
  "tenant": null,
  "workspace_kind": "scratch",
  "workspace_path": null,
  "branch_name": null,
  "project_id": null,
  "created_by": "user",
  "created_at": 1792497242,
  "started_at": null,
  "completed_at": null,
  "result": null,
  "skills": [],
  "max_runtime_seconds": null,
  "max_retries": null,
  "model_override": null,
  "provider_override": null,
  "session_id": "20261020_044727_01aab3eb",
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
Subscribed discord:1562069703856496752 to t_f79d72f9
--- set_auto_handle (exit 0)
t_f79d72f9: auto-handle: true
note: no session identity — home-session guard skipped
--- complete (exit 0)
Completed t_f79d72f9
note: no session identity — home-session guard skipped
--- log match
2026-10-20 04:54:42,271 INFO tools.delegate_tool: boomerang.fork_ineligible reason=fork_enabled parent_session=20261020_044727_01aab3eb
--- ack request
config.yaml: delegation.boomerang.auto_handle_enabled: true -> false
ack: echo auto_handle_enabled=false >> /Volumes/fleet-scratch/tmp/pl-1020044514/bv2.ack
--- ack cmd auto_handle_enabled=false (exit 0)
bv2-ack: /Volumes/fleet-scratch/tmp/pl-1020044514/home/config.yaml: delegation.boomerang.auto_handle_enabled = false
--- create (exit 0)
{
  "id": "t_c4b945db",
  "title": "bv2 smoke s2: auto_handle_enabled=false -> surface",
  "body": "origin: cli · session 20261020_044727_01aab3eb · 2026-10-20",
  "assignee": "human:apollo",
  "status": "ready",
  "priority": 0,
  "tenant": null,
  "workspace_kind": "scratch",
  "workspace_path": null,
  "branch_name": null,
  "project_id": null,
  "created_by": "user",
  "created_at": 1792497366,
  "started_at": null,
  "completed_at": null,
  "result": null,
  "skills": [],
  "max_runtime_seconds": null,
  "max_retries": null,
  "model_override": null,
  "provider_override": null,
  "session_id": "20261020_044727_01aab3eb",
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
Subscribed discord:1562069703856496752 to t_c4b945db
--- set_auto_handle (exit 0)
t_c4b945db: auto-handle: true
note: no session identity — home-session guard skipped
--- complete (exit 0)
Completed t_c4b945db
note: no session identity — home-session guard skipped
--- ack request
config.yaml: restore delegation.boomerang.fork_enabled: false -> true and delegation.boomerang.auto_handle_enabled: false -> true
ack: echo restore >> /Volumes/fleet-scratch/tmp/pl-1020044514/bv2.ack
--- ack cmd restore (exit 0)
bv2-ack: /Volumes/fleet-scratch/tmp/pl-1020044514/home/config.yaml: delegation.boomerang.fork_enabled = true
bv2-ack: /Volumes/fleet-scratch/tmp/pl-1020044514/home/config.yaml: delegation.boomerang.auto_handle_enabled = true
```

### Checks

| # | check | result | detail |
|---|---|---|---|
| 1 | precondition: auto_handle_enabled=true and fork_enabled=true | PASS | auto_handle_enabled=True fork_enabled=True |
| 2 | ack `fork_enabled=false` received | PASS | /Volumes/fleet-scratch/tmp/pl-1020044514/bv2.ack |
| 3 | config reads delegation.boomerang.fork_enabled=false after ack | PASS | read false (stage 1) |
| 4 | gateway log: `boomerang.fork_ineligible reason=fork_enabled` after the flip | PASS | 2026-10-20 04:54:42,271 INFO tools.delegate_tool: boomerang.fork_ineligible reason=fork_enabled parent_session=20261020_044727_01aab3eb |
| 5 | origin chat: `[auto-handled] t_f79d72f9` still posts (summary-mode child) | PASS | [auto-handled] t_f79d72f9 — Smoke fixture t_f79d72f9 completed as designed (forced completed event, fork_enabled=false surfaced as summary); nothing to fix, chain clear. |
| 6 | ack `auto_handle_enabled=false` received | PASS | /Volumes/fleet-scratch/tmp/pl-1020044514/bv2.ack |
| 7 | config reads delegation.boomerang.auto_handle_enabled=false after ack | PASS | read false (stage 2) |
| 8 | card t_c4b945db: not auto-handled within one tick of the flip | PASS | no auto-handle (waited 20s) |
| 9 | ack `restore` received | PASS | /Volumes/fleet-scratch/tmp/pl-1020044514/bv2.ack |
| 10 | config restored: both switches true | PASS | stage 3 |

<!-- bv2-smoke:step2:end -->

<!-- bv2-smoke:step3:begin -->
## Step 3: no x-hermes-* header upstream on a fork turn (AC-9, INV-8)

- mode: `live`  started: 2026-10-20T08:19:52-07:00  finished: 2026-10-20T08:42:32-07:00
- verdict: **PASS**
- wire capture root `/Users/alexgierczyk/.claude-wirelog-fleet` (relay-wire-forensics apx/bpx JSONL schema)
- card `s3_fork` = `t_8e129811`
- served by `sub-vps-9`: its capture is on that box; run `wirelog-fleet.py sync` and point BV2_WIRELOG_DIR at ~/.claude-wirelog-fleet before trusting step 3

### Commands

```text
$ python3 ~/.hermes/scripts/wirelog-coverage-check.py   # confirm the serving lane is capturing before trusting a clean result
$ POST http://127.0.0.1:18810/v1/messages  x-hermes-lane-src: platform=bv2canary423abda2130f;delegate_depth=0;aux_task=-  x-hermes-qa-canary: bv2canary423abda2130f  model=claude-haiku-4-5 max_tokens=1   # HTTP 200 served_by=sub-vps-18
$ python3 /Users/alexgierczyk/.hermes/scripts/wirelog-fleet.py sync --sub 18   # canary seat, exit 0
$ /Volumes/fleet-scratch/tmp/bv2-lane.QBLas4/scripts/bv2-kanban.sh boards create bv2-smoke-pl --name 'BV2 smoke (scratch)' --description 'Boomerang v2 smoke harness scratch board (t_989cde1b); safe to archive'   # exit 0
$ /Volumes/fleet-scratch/tmp/bv2-lane.QBLas4/scripts/bv2-kanban.sh --board bv2-smoke-pl create 'bv2 smoke s3: one fork turn for wire capture' --session 20261020_081913_15ca4c41 --assignee human:apollo --json   # exit 0
$ /Volumes/fleet-scratch/tmp/bv2-lane.QBLas4/scripts/bv2-kanban.sh --board bv2-smoke-pl notify-subscribe t_8e129811 --platform discord --chat-id 1562123016979218467 --chat-type channel --delivery-mode notify+wake   # exit 0
$ /Volumes/fleet-scratch/tmp/bv2-lane.QBLas4/scripts/bv2-kanban.sh --board bv2-smoke-pl edit t_8e129811 --auto-handle true   # exit 0
$ /Volumes/fleet-scratch/tmp/bv2-lane.QBLas4/scripts/bv2-kanban.sh --board bv2-smoke-pl complete t_8e129811 --summary 'bv2 smoke: forced completed event'   # exit 0
$ python3 /Users/alexgierczyk/.hermes/scripts/wirelog-fleet.py sync --sub 9   # fork seat, exit 0
```

### Raw output

```text
--- canary
HTTP 200 served_by=sub-vps-18 ok
--- wirelog_sync canary sub-18 (exit 0)
[31msub-18  +2366 new record(s) (66927 total) — BACKLOG remains, resumes next run[0m
[2msynced 2366 new record(s) -> /Users/alexgierczyk/.claude-wirelog-fleet[0m
[1m[31m🔴 1 box(es) unreadable this run (sub-18) — their local captures were PRESERVED, not deleted.[0m
--- board_create (exit 0)
Board 'bv2-smoke-pl' created.
  Display name: BV2 smoke (scratch)
  DB path:      /Volumes/fleet-scratch/tmp/pl-1020081834/home/kanban/boards/bv2-smoke-pl/kanban.db
  Use `hermes kanban boards switch bv2-smoke-pl` to make it current.
--- create (exit 0)
{
  "id": "t_8e129811",
  "title": "bv2 smoke s3: one fork turn for wire capture",
  "body": "origin: cli · session 20261020_081913_15ca4c41 · 2026-10-20",
  "assignee": "human:apollo",
  "status": "ready",
  "priority": 0,
  "tenant": null,
  "workspace_kind": "scratch",
  "workspace_path": null,
  "branch_name": null,
  "project_id": null,
  "created_by": "user",
  "created_at": 1792510002,
  "started_at": null,
  "completed_at": null,
  "result": null,
  "skills": [],
  "max_runtime_seconds": null,
  "max_retries": null,
  "model_override": null,
  "provider_override": null,
  "session_id": "20261020_081913_15ca4c41",
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
Subscribed discord:1562123016979218467 to t_8e129811
--- set_auto_handle (exit 0)
t_8e129811: auto-handle: true
note: no session identity — home-session guard skipped
--- complete (exit 0)
Completed t_8e129811
note: no session identity — home-session guard skipped
--- log match
2026-10-20 08:26:55,991 INFO [20261020_082651_88896c] tools.delegate_tool: boomerang.fork served_by=sub-vps-9 read_share=0.9277 prefix_tokens=10236 child_session=20261020_082651_88896c
--- wirelog_sync fork sub-9 (exit 0)
[32msub-9   +46 new record(s) (133939 total)[0m
[2msynced 46 new record(s) -> /Users/alexgierczyk/.claude-wirelog-fleet[0m
--- upstream header scan
375492-10872 lane=apx sub=9 headers=42
375492-10873 lane=apx sub=9 headers=42
375492-10874 lane=apx sub=9 headers=42
375492-10875 lane=apx sub=9 headers=42
375492-10876 lane=apx sub=9 headers=42
375492-10877 lane=apx sub=9 headers=42
375492-10878 lane=apx sub=9 headers=42
375492-10879 lane=apx sub=9 headers=42
375492-10880 lane=apx sub=9 headers=42
375492-10882 lane=apx sub=9 headers=42
375492-10881 lane=apx sub=9 headers=42
```

### Checks

| # | check | result | detail |
|---|---|---|---|
| 1 | gateway log: `boomerang.fork served_by=<seat>` (a fork turn happened) | PASS | 2026-10-20 08:26:55,991 INFO [20261020_082651_88896c] tools.delegate_tool: boomerang.fork served_by=sub-vps-9 read_share=0.9277 prefix_tokens=10236 child_session=20261020_082651_88896c |
| 2 | positive control: canary `bv2canary423abda2130f` recorded (fleet header reached the capture) | PASS | 517352-4538 lane=apx sub=18 lane_src.platform=bv2canary423abda2130f |
| 3 | wire records captured in the fork-turn window | PASS | 11 record(s), seat `sub-vps-9` |
| 4 | no `x-hermes-*` header in any upstream request | PASS | 0/11 records carry one |

<!-- bv2-smoke:step3:end -->

<!-- bv2-smoke:step4:begin -->
## Step 4: restart drill: a cut child is redispatched once; a spent retry re-surfaces (AC-11, 5.7)

- mode: `live`  started: 2026-10-24T16:15:28-07:00  finished: 2026-10-24T16:21:21-07:00
- verdict: **PASS**
- the harness never restarts anything; gateway pid file `/Volumes/fleet-scratch/tmp/pl-1024161447/home/gateway.pid` boot_id at start: `47216:1792883688.502225`; wake state `/Volumes/fleet-scratch/tmp/pl-1024161447/home/gateway/kanban_boomerang_wake.json`
- arm 1 (redispatch): retry unspent -> quiet redispatch, second child finishes the card
- arm 2 (fallback): retries[card]=1 written before the restart -> lost (retry spent) + re-surface
- card `s4_restart` = `t_9053192c`
- card `s4_fallback` = `t_fff7c556`

### Commands

```text
$ /Volumes/fleet-scratch/tmp/bv2-lane.VnGQJO/scripts/bv2-kanban.sh boards create bv2-smoke-pl --name 'BV2 smoke (scratch)' --description 'Boomerang v2 smoke harness scratch board (t_989cde1b); safe to archive'   # exit 0
$ /Volumes/fleet-scratch/tmp/bv2-lane.VnGQJO/scripts/bv2-kanban.sh --board bv2-smoke-pl create 'bv2 smoke s4: child in flight across a gateway restart (redispatch)' --session 20261024_161516_fcf7f5c3 --assignee human:apollo --json   # exit 0
$ /Volumes/fleet-scratch/tmp/bv2-lane.VnGQJO/scripts/bv2-kanban.sh --board bv2-smoke-pl notify-subscribe t_9053192c --platform discord --chat-id 1563692373173731358 --chat-type channel --delivery-mode notify+wake   # exit 0
$ /Volumes/fleet-scratch/tmp/bv2-lane.VnGQJO/scripts/bv2-kanban.sh --board bv2-smoke-pl edit t_9053192c --boomerang-wake   # exit 0
$ /Volumes/fleet-scratch/tmp/bv2-lane.VnGQJO/scripts/bv2-kanban.sh --board bv2-smoke-pl edit t_9053192c --body 'bv2 smoke s4 restart-drill fixture (board bv2-smoke-pl, puppet lane). Accepting this forced completion requires these checks, each run with the terminal tool and quoted in your evidence: (1) `cat "$HERMES_HOME/config.yaml"` and list every top-level key; (2) `ls -la "$HERMES_HOME/logs"`; (3) `tail -n 5 "$HERMES_HOME/logs/gateway.log"`; (4) `shasum -a 256 "$HERMES_HOME/logs/gateway.log"`; (5) `python3 -c '"'"'import json,sys; print(len(json.load(open(sys.argv[1]))))'"'"' "$HERMES_HOME/sessions/sessions.json"`; (6) wait 60 seconds (`sleep 60`) and re-run (4): report whether the digest changed. If all six ran, accept the completion.'   # exit 0
$ /Volumes/fleet-scratch/tmp/bv2-lane.VnGQJO/scripts/bv2-kanban.sh --board bv2-smoke-pl complete t_9053192c --summary 'bv2 smoke: forced completed event'   # exit 0
# APOLLO (manual): restart the gateway NOW via safe-gateway-restart (single-use approval), while the boomerang-wake child for t_9053192c is still running   ack: restart
$ /Volumes/fleet-scratch/tmp/bv2-lane.VnGQJO/scripts/bv2-ack.sh restart   # scripted ack, exit 0
$ /Volumes/fleet-scratch/tmp/bv2-lane.VnGQJO/scripts/bv2-kanban.sh --board bv2-smoke-pl create 'bv2 smoke s4: child in flight across a gateway restart (retry spent)' --session 20261024_161516_fcf7f5c3 --assignee human:apollo --json   # exit 0
$ /Volumes/fleet-scratch/tmp/bv2-lane.VnGQJO/scripts/bv2-kanban.sh --board bv2-smoke-pl notify-subscribe t_fff7c556 --platform discord --chat-id 1563692373173731358 --chat-type channel --delivery-mode notify+wake   # exit 0
$ /Volumes/fleet-scratch/tmp/bv2-lane.VnGQJO/scripts/bv2-kanban.sh --board bv2-smoke-pl edit t_fff7c556 --boomerang-wake   # exit 0
$ /Volumes/fleet-scratch/tmp/bv2-lane.VnGQJO/scripts/bv2-kanban.sh --board bv2-smoke-pl edit t_fff7c556 --body 'bv2 smoke s4 restart-drill fixture (board bv2-smoke-pl, puppet lane). Accepting this forced completion requires these checks, each run with the terminal tool and quoted in your evidence: (1) `cat "$HERMES_HOME/config.yaml"` and list every top-level key; (2) `ls -la "$HERMES_HOME/logs"`; (3) `tail -n 5 "$HERMES_HOME/logs/gateway.log"`; (4) `shasum -a 256 "$HERMES_HOME/logs/gateway.log"`; (5) `python3 -c '"'"'import json,sys; print(len(json.load(open(sys.argv[1]))))'"'"' "$HERMES_HOME/sessions/sessions.json"`; (6) wait 60 seconds (`sleep 60`) and re-run (4): report whether the digest changed. If all six ran, accept the completion.'   # exit 0
$ /Volumes/fleet-scratch/tmp/bv2-lane.VnGQJO/scripts/bv2-kanban.sh --board bv2-smoke-pl complete t_fff7c556 --summary 'bv2 smoke: forced completed event'   # exit 0
# write retries[t_fff7c556]=1 into /Volumes/fleet-scratch/tmp/pl-1024161447/home/gateway/kanban_boomerang_wake.json   # readback retries[t_fff7c556]=1
# APOLLO (manual): restart the gateway NOW via safe-gateway-restart (single-use approval), while the boomerang-wake child for t_fff7c556 is still running   ack: restart
$ /Volumes/fleet-scratch/tmp/bv2-lane.VnGQJO/scripts/bv2-ack.sh restart   # scripted ack, exit 0
```

### Raw output

```text
--- board_create (exit 0)
Board 'bv2-smoke-pl' created.
  Display name: BV2 smoke (scratch)
  DB path:      /Volumes/fleet-scratch/tmp/pl-1024161447/home/kanban/boards/bv2-smoke-pl/kanban.db
  Use `hermes kanban boards switch bv2-smoke-pl` to make it current.
--- create (exit 0)
{
  "id": "t_9053192c",
  "title": "bv2 smoke s4: child in flight across a gateway restart (redispatch)",
  "body": "origin: cli · session 20261024_161516_fcf7f5c3 · 2026-10-24",
  "assignee": "human:apollo",
  "status": "ready",
  "priority": 0,
  "tenant": null,
  "workspace_kind": "scratch",
  "workspace_path": null,
  "branch_name": null,
  "project_id": null,
  "created_by": "user",
  "created_at": 1792883738,
  "started_at": null,
  "completed_at": null,
  "result": null,
  "skills": [],
  "max_runtime_seconds": null,
  "max_retries": null,
  "model_override": null,
  "provider_override": null,
  "session_id": "20261024_161516_fcf7f5c3",
  "workflow_template_id": null,
  "current_step_key": null,
  "completion_contract": "local-only",
  "last_failure_error": null,
  "reasoning_effort": null,
  "brain": null,
  "pin_sub_reason": null,
  "pin_sub_fallback": false,
  "unhomed": false,
  "no_worker": false,
  "home": "20261024_161516_fcf7f5c3"
}
--- notify_subscribe (exit 0)
Subscribed discord:1563692373173731358 to t_9053192c
--- set_boomerang_wake (exit 0)
t_9053192c: level: boomerang wake
note: no session identity — home-session guard skipped
--- s4_hold (exit 0)
Edited t_9053192c
note: no session identity — home-session guard skipped
--- complete (exit 0)
Completed t_9053192c
note: no session identity — home-session guard skipped
--- log match (redispatch arm)
2026-10-24 16:15:47,962 INFO gateway.kanban_boomerang_wake: kanban notifier: boomerang-wake t_9053192c disposition=auto row=completed path=notifier delegation=del-20261024-161547-08cd
--- ack request
restart the gateway NOW via safe-gateway-restart (single-use approval), while the boomerang-wake child for t_9053192c is still running
ack: echo restart >> /Volumes/fleet-scratch/tmp/pl-1024161447/bv2.ack
--- ack cmd restart (exit 0)
bv2-ack: restart: scratch gateway boot_id before=47216:1792883688.502225 pid=47216
gw-restart: pid 47216 SIGKILL after 45s
gw-restart: pid 69420 connected boot_id=69420:1792883794.829501
--- log match (redispatch arm)
2026-10-24 16:16:52,152 INFO gateway.run: Gateway running with 1 platform(s)
--- log match (redispatch arm)
2026-10-24 16:16:58,269 INFO gateway.kanban_boomerang_wake: kanban boomerang-wake: t_9053192c redispatched after gateway restart (retry 1/1) delegation=del-20261024-161658-5f2a
--- log match (redispatch arm)
2026-10-24 16:19:15,828 INFO gateway.kanban_boomerang_wake: kanban boomerang-wake: t_9053192c handled action=complete delegation=del-20261024-161658-5f2a
--- chat match (redispatch arm)
[boomerang-wake] t_9053192c — All six fixture checks ran and passed; forced completion of restart-drill card t_9053192c accepted, gateway.log digest stable across 60s.
--- create (exit 0)
{
  "id": "t_fff7c556",
  "title": "bv2 smoke s4: child in flight across a gateway restart (retry spent)",
  "body": "origin: cli · session 20261024_161516_fcf7f5c3 · 2026-10-24",
  "assignee": "human:apollo",
  "status": "ready",
  "priority": 0,
  "tenant": null,
  "workspace_kind": "scratch",
  "workspace_path": null,
  "branch_name": null,
  "project_id": null,
  "created_by": "user",
  "created_at": 1792883979,
  "started_at": null,
  "completed_at": null,
  "result": null,
  "skills": [],
  "max_runtime_seconds": null,
  "max_retries": null,
  "model_override": null,
  "provider_override": null,
  "session_id": "20261024_161516_fcf7f5c3",
  "workflow_template_id": null,
  "current_step_key": null,
  "completion_contract": "local-only",
  "last_failure_error": null,
  "reasoning_effort": null,
  "brain": null,
  "pin_sub_reason": null,
  "pin_sub_fallback": false,
  "unhomed": false,
  "no_worker": false,
  "home": "20261024_161516_fcf7f5c3"
}
--- notify_subscribe (exit 0)
Subscribed discord:1563692373173731358 to t_fff7c556
--- set_boomerang_wake (exit 0)
t_fff7c556: level: boomerang wake
note: no session identity — home-session guard skipped
--- s4_hold (exit 0)
Edited t_fff7c556
note: no session identity — home-session guard skipped
--- complete (exit 0)
Completed t_fff7c556
note: no session identity — home-session guard skipped
--- log match (fallback arm)
2026-10-24 16:19:46,975 INFO gateway.kanban_boomerang_wake: kanban notifier: boomerang-wake t_fff7c556 disposition=auto row=completed path=notifier delegation=del-20261024-161946-7b62
--- ack request
restart the gateway NOW via safe-gateway-restart (single-use approval), while the boomerang-wake child for t_fff7c556 is still running
ack: echo restart >> /Volumes/fleet-scratch/tmp/pl-1024161447/bv2.ack
--- ack cmd restart (exit 0)
bv2-ack: restart: scratch gateway boot_id before=69420:1792883794.829501 pid=69420
gw-restart: pid 69420 SIGKILL after 45s
gw-restart: pid 38081 connected boot_id=38081:1792884032.82546
--- log match (fallback arm)
2026-10-24 16:20:53,604 INFO gateway.run: Gateway running with 1 platform(s)
--- log match (fallback arm)
2026-10-24 16:20:59,734 INFO gateway.kanban_boomerang_wake: kanban boomerang-wake: t_fff7c556 lost reason=gateway restart, retry spent resurfaced delivered=True delegation=del-20261024-161946-7b62
```

### Checks

| # | check | result | detail |
|---|---|---|---|
| 1 | redispatch arm: boomerang-wake child dispatched for t_9053192c (disposition=auto) | PASS | 2026-10-24 16:15:47,962 INFO gateway.kanban_boomerang_wake: kanban notifier: boomerang-wake t_9053192c disposition=auto row=completed path=notifier delegation=del-20261024-161547-08cd |
| 2 | ack `restart` received | PASS | /Volumes/fleet-scratch/tmp/pl-1024161447/bv2.ack |
| 3 | redispatch arm: gateway restart observed in the log | PASS | 2026-10-24 16:16:52,152 INFO gateway.run: Gateway running with 1 platform(s) |
| 4 | redispatch arm: gateway boot_id changed | PASS | 47216:1792883688.502225 -> 69420:1792883794.829501 |
| 5 | redispatch arm: gateway.log INFO `kanban boomerang-wake: t_9053192c redispatched after gateway restart` | PASS | 2026-10-24 16:16:58,269 INFO gateway.kanban_boomerang_wake: kanban boomerang-wake: t_9053192c redispatched after gateway restart (retry 1/1) delegation=del-20261024-161658-5f2a |
| 6 | redispatch arm: redispatch on the first notifier tick after the restart | PASS | 6.1s after the boot line (limit 180s = wiring + 5 s initial delay + one full notifier tick, --first-tick-seconds) |
| 7 | redispatch arm: the redispatch is a NEW child | PASS | first delegation del-20261024-161547-08cd -> redispatched del-20261024-161658-5f2a |
| 8 | redispatch arm: the second child handled the card (delegation del-20261024-161658-5f2a) | PASS | 2026-10-24 16:19:15,828 INFO gateway.kanban_boomerang_wake: kanban boomerang-wake: t_9053192c handled action=complete delegation=del-20261024-161658-5f2a |
| 9 | redispatch arm: origin chat `[boomerang-wake] t_9053192c - <Outcome>` after the restart | PASS | [boomerang-wake] t_9053192c — All six fixture checks ran and passed; forced completion of restart-drill card t_9053192c accepted, gateway.log digest stable across 60s. |
| 10 | redispatch arm: ZERO `warning boomerang-wake lost t_9053192c` lines in the origin chat | PASS | none (waited 20s) |
| 11 | fallback arm: boomerang-wake child dispatched for t_fff7c556 (disposition=auto) | PASS | 2026-10-24 16:19:46,975 INFO gateway.kanban_boomerang_wake: kanban notifier: boomerang-wake t_fff7c556 disposition=auto row=completed path=notifier delegation=del-20261024-161946-7b62 |
| 12 | claim for t_fff7c556 is `running` before the retry is pre-spent | PASS | delegation del-20261024-161946-7b62 |
| 13 | retries[t_fff7c556] pre-spent | PASS | readback 1 |
| 14 | ack `restart` received | PASS | /Volumes/fleet-scratch/tmp/pl-1024161447/bv2.ack |
| 15 | fallback arm: gateway restart observed in the log | PASS | 2026-10-24 16:20:53,604 INFO gateway.run: Gateway running with 1 platform(s) |
| 16 | fallback arm: gateway boot_id changed | PASS | 69420:1792883794.829501 -> 38081:1792884032.82546 |
| 17 | fallback arm: reconciler re-surfaced the wake for t_fff7c556 (retry spent) | PASS | 2026-10-24 16:20:59,734 INFO gateway.kanban_boomerang_wake: kanban boomerang-wake: t_fff7c556 lost reason=gateway restart, retry spent resurfaced delivered=True delegation=del-20261024-161946-7b62 |
| 18 | fallback arm: re-surface on the first notifier tick after the restart | PASS | 6.1s after the boot line (limit 180s = wiring + 5 s initial delay + one full notifier tick, --first-tick-seconds) |
| 19 | fallback arm: resurfaced delivered=True | PASS | delivered=True |
| 20 | fallback arm: origin chat `warning boomerang-wake lost t_fff7c556 (reason: gateway restart, retry spent)` | PASS | ⚠️ boomerang-wake lost t_fff7c556 (reason: gateway restart, retry spent) |
| 21 | fallback arm: no redispatch for t_fff7c556 | PASS | none |
| 22 | fallback arm: no late `[boomerang-wake] t_fff7c556` within waited 20s after the re-surface | PASS | none |

<!-- bv2-smoke:step4:end -->


## t_facf0dba step 4 runs (2026-10-24)

The step-4 section above is run 2. Two lane runs against the #2271 head, steps 4 only (`bv2-smoke-lane.sh <wt> --steps 4`):

- **run 1, scratch code `4217925545` (#2271 head + harness), lane exit 1.** Redispatch arm: the INFO line
  `kanban boomerang-wake: t_9b9b8b56 redispatched after gateway restart (retry 1/1) delegation=del-20261024-154551-3f1e`
  landed 6.2 s after the boot line, and no `lost` line was posted. But the second child never finished the card:
  after a boot there is no live parent, so `resolve_parent` falls back to `_db_parent` (`fork_ineligible reason=no_live_parent`),
  which built the AIAgent with `model=""`. Its first call got HTTP 400 `the request carries no usable model value`
  (scratch agent.log 15:45:51.702). Checks: `the second child handled the card | FAIL`, `[boomerang-wake] ... after the restart | FAIL`.
  The fallback arm PASSed on that run (t_36777497).
### Run 1 lane facts

- scratch gateway code: `421792554567925bd01f1ae07451aff05426ceb0` from `/Users/alexgierczyk/.hermes/hermes-agent/.worktrees/t_facf0dba`; scratch home `/Volumes/fleet-scratch/tmp/pl-1024154312/home`
- origin thread `1563684599572201542` in #daedalus, home session `20261024_154423_03ce097a` (owned by the scratch gateway)
- launchd gateway boot_ids before: .hermes 34786:1792874306.006716;aegis 92061:1792778715.678917;argus 93629:1792778722.792216;athena 94130:1792778724.722454;cadmus 95056:1792778728.530041;chiron 96430:1792778734.539368;daedalus-opus 97046:1792778737.505067;daedalus 97990:1792778740.431808;momus 98937:1792778744.600903;
- launchd gateway boot_ids after:  .hermes 34786:1792874306.006716;aegis 92061:1792778715.678917;argus 93629:1792778722.792216;athena 94130:1792778724.722454;cadmus 95056:1792778728.530041;chiron 96430:1792778734.539368;daedalus-opus 97046:1792778737.505067;daedalus 97990:1792778740.431808;momus 98937:1792778744.600903;  -> UNCHANGED

- **fix `f8939c5c90`:** `_db_parent` passes the fallback route's model or `_resolve_gateway_model()`.
- **run 2, scratch code `f8939c5c90`, lane exit 0:** both arms PASS (the section above). launchd boot_ids unchanged, zero-spill PASS.

## Lane facts

- scratch gateway code: `f8939c5c902ced56cb861313d92c371c604c09e8` from `/Users/alexgierczyk/.hermes/hermes-agent/.worktrees/t_facf0dba`; scratch home `/Volumes/fleet-scratch/tmp/pl-1024161447/home`
- origin thread `1563692373173731358` in #daedalus, home session `20261024_161516_fcf7f5c3` (owned by the scratch gateway)
- launchd gateway boot_ids before: .hermes 34786:1792874306.006716;aegis 92061:1792778715.678917;argus 93629:1792778722.792216;athena 94130:1792778724.722454;cadmus 95056:1792778728.530041;chiron 96430:1792778734.539368;daedalus-opus 97046:1792778737.505067;daedalus 97990:1792778740.431808;momus 98937:1792778744.600903;
- launchd gateway boot_ids after:  .hermes 34786:1792874306.006716;aegis 92061:1792778715.678917;argus 93629:1792778722.792216;athena 94130:1792778724.722454;cadmus 95056:1792778728.530041;chiron 96430:1792778734.539368;daedalus-opus 97046:1792778737.505067;daedalus 97990:1792778740.431808;momus 98937:1792778744.600903;  -> UNCHANGED
