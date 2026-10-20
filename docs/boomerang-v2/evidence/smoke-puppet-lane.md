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
## Step 4: restart drill observer: reconciler re-surfaces within one tick (AC-11, 5.7)

- mode: `live`  started: 2026-10-20T05:51:30-07:00  finished: 2026-10-20T05:59:32-07:00
- verdict: **PASS**
- the harness never restarts anything; gateway pid file `/Volumes/fleet-scratch/tmp/pl-1020044514/home/gateway.pid` boot_id before: `6910:1792496722.396544`
- card `s4_restart` = `t_db9c6f92`

### Commands

```text
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh boards create bv2-smoke-pl --name 'BV2 smoke (scratch)' --description 'Boomerang v2 smoke harness scratch board (t_989cde1b); safe to archive'   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl create 'bv2 smoke s4: child in flight across a gateway restart' --session 20261020_044727_01aab3eb --assignee human:apollo --json   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl notify-subscribe t_db9c6f92 --platform discord --chat-id 1562069703856496752 --chat-type channel --delivery-mode notify+wake   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl edit t_db9c6f92 --auto-handle true   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl edit t_db9c6f92 --body 'bv2 smoke s4 restart-drill fixture (board bv2-smoke-pl, puppet lane). Accepting this forced completion requires these checks, each run with the terminal tool and quoted in your evidence: (1) `cat "$HERMES_HOME/config.yaml"` and list every top-level key; (2) `ls -la "$HERMES_HOME/logs"`; (3) `tail -n 5 "$HERMES_HOME/logs/gateway.log"`; (4) `shasum -a 256 "$HERMES_HOME/logs/gateway.log"`; (5) `python3 -c '"'"'import json,sys; print(len(json.load(open(sys.argv[1]))))'"'"' "$HERMES_HOME/sessions/sessions.json"`; (6) wait 60 seconds (`sleep 60`) and re-run (4): report whether the digest changed. If all six ran, accept the completion.'   # exit 0
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-kanban.sh --board bv2-smoke-pl complete t_db9c6f92 --summary 'bv2 smoke: forced completed event'   # exit 0
# APOLLO (manual): restart the gateway NOW via safe-gateway-restart (single-use approval), while the auto-handle child for t_db9c6f92 is still running   ack: restart
$ /Users/alexgierczyk/.hermes/.worktrees/t_b901c171/skills-shared/hermes-harness/puppet-lane/scripts/bv2-ack.sh restart   # scripted ack, exit 0
```

### Raw output

```text
--- board_create (exit 0)
Board 'bv2-smoke-pl' already exists.
  Display name: BV2 smoke (scratch)
  DB path:      /Volumes/fleet-scratch/tmp/pl-1020044514/home/kanban/boards/bv2-smoke-pl/kanban.db
  Use `hermes kanban boards switch bv2-smoke-pl` to make it current.
--- create (exit 0)
{
  "id": "t_db9c6f92",
  "title": "bv2 smoke s4: child in flight across a gateway restart",
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
  "created_at": 1792500778,
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
Subscribed discord:1562069703856496752 to t_db9c6f92
--- set_auto_handle (exit 0)
t_db9c6f92: auto-handle: true
note: no session identity — home-session guard skipped
--- s4_hold (exit 0)
Edited t_db9c6f92
note: no session identity — home-session guard skipped
--- complete (exit 0)
Completed t_db9c6f92
note: no session identity — home-session guard skipped
--- log match
2026-10-20 05:55:41,754 INFO gateway.kanban_auto_handle: kanban notifier: auto-handled t_db9c6f92 disposition=auto row=completed path=notifier delegation=del-20261020-055541-0ffc
--- ack request
restart the gateway NOW via safe-gateway-restart (single-use approval), while the auto-handle child for t_db9c6f92 is still running
ack: echo restart >> /Volumes/fleet-scratch/tmp/pl-1020044514/bv2.ack
--- ack cmd restart (exit 0)
bv2-ack: restart: scratch gateway boot_id before=6910:1792496722.396544 pid=6910
gw-restart: pid 6910 SIGKILL after 45s
gw-restart: pid 7895 connected boot_id=7895:1792501007.955348
--- log match
2026-10-20 05:59:00,612 INFO gateway.run: Gateway running with 1 platform(s)
--- log match
2026-10-20 05:59:08,327 INFO gateway.kanban_auto_handle: kanban auto-handle: t_db9c6f92 lost reason=gateway restart resurfaced delivered=True delegation=del-20261020-055541-0ffc
```

### Checks

| # | check | result | detail |
|---|---|---|---|
| 1 | auto-handle child dispatched for t_db9c6f92 (disposition=auto) | PASS | 2026-10-20 05:55:41,754 INFO gateway.kanban_auto_handle: kanban notifier: auto-handled t_db9c6f92 disposition=auto row=completed path=notifier delegation=del-20261020-055541-0ffc |
| 2 | ack `restart` received | PASS | /Volumes/fleet-scratch/tmp/pl-1020044514/bv2.ack |
| 3 | gateway restart observed in the log | PASS | 2026-10-20 05:59:00,612 INFO gateway.run: Gateway running with 1 platform(s) |
| 4 | gateway boot_id changed | PASS | 6910:1792496722.396544 -> 7895:1792501007.955348 |
| 5 | reconciler re-surfaced the wake for t_db9c6f92 | PASS | 2026-10-20 05:59:08,327 INFO gateway.kanban_auto_handle: kanban auto-handle: t_db9c6f92 lost reason=gateway restart resurfaced delivered=True delegation=del-20261020-055541-0ffc |
| 6 | re-surface on the first notifier tick after the restart | PASS | 7.7s after `2026-10-20 05:59:00` (limit 180s = wiring + 5 s initial delay + one full notifier tick, --first-tick-seconds) |
| 7 | origin chat: `warning auto-handle lost t_db9c6f92 (reason: gateway restart)` | PASS | ⚠️ auto-handle lost t_db9c6f92 (reason: gateway restart) |
| 8 | no late `[auto-handled] t_db9c6f92` within waited 20s after the re-surface | PASS | none |

<!-- bv2-smoke:step4:end -->

