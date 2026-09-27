# Tranche: cron+tools — 111 rows

absorb = substantive added src lines found verbatim on upstream/main (hit/total); sym = added def/class names found. Signal only — read README §0.

| key | subsystem | kind | loc | files | conflict-files (syncs) | absorb src | sym | date | subject |
|---|---|---|---|---|---|---|---|---|---|
| #285 | tools | code | 3499 | 19 | 9(3) | 82/761 | 18/50 | 2026-07-11 | feat(delegation): background delegate_task restart survival (persist + auto-redispatch) (#285) |
| #693 | tools | code | 3041 | 7 | 3(3) | 7/285 | 4/17 | 2026-09-14 | fix(delegation): preserve recovered results and isolate replay scope (#693) |
| #211 | tools | code | 1123 | 6 | 1(1) | 18/195 | 9/22 | 2026-07-06 | feat(video_analyze): SSRF-filtering egress proxy for yt-dlp (default OFF) (#211) |
| #791 | cron | code | 1082 | 5 | 2(3) | 25/243 | 8/21 | 2026-09-21 | fix(cron): by-id field merge under lock + vanished-job guard (#791) |
| #767 | cron | code | 966 | 5 | 1(2) | 7/105 | 0/7 | 2026-09-20 | fix(lifecycle_guard): allow bootstrap of an EXISTING non-gateway plist; surface guard blocks in exec |
| #690 | tools | code | 944 | 2 | 1(3) | 9/103 | 2/7 | 2026-09-13 | fix(delegation): recover terminal mirrors from canonical outbox evidence (#690) |
| #83 | tools | code | 814 | 5 | 3(3) | 0/77 | 1/7 | 2026-06-22 | fix(send_message): route subagent bare sends to parent origin, not home (v2) (#83) |
| #470 | tools | code | 813 | 5 | 5(3) | 35/111 | 3/5 | 2026-08-06 | feat(kanban): CLI auto-subscribe knob + dispatch unwatched warning (fork-adapted from #80564) (#470) |
| #740 | cron | code | 788 | 3 | 1(2) | 4/122 | 0/10 | 2026-09-20 | fix(terminal): let a supervised gateway manage SIBLING gateway launchd/systemd jobs (self-aware life |
| #88 | tools | code | 768 | 8 | 7(3) | 0/55 | 1/6 | 2026-06-22 | fix(send_message): HERMES_CRON_SESSION ContextVar isolation (stop in-process cron flag latch leaking |
| nopr:f504b1c928 | tools | code | 720 | 9 | 2(3) | 14/241 | 0/6 | 2026-06-29 | fix(toolsets): restore fork-custom messaging + moa toolsets dropped by upstream parity merge |
| #122 | tools | code | 636 | 6 | 3(2) | 10/130 | 4/15 | 2026-06-30 | feat(skills): author new skills into skills-shared/<group>/ by default (#122) |
| #321 | tools | code | 586 | 3 | 1(3) | 6/79 | 0/4 | 2026-07-13 | feat(delegation): Phase-0 cancel-attribution forensics (WHO/WHY/WHEN on every cancel) (#321) |
| #815 | cron | code | 566 | 6 | 1(2) | 4/71 | 1/2 | 2026-09-21 | fix(cron): close 7 P1 races and false-greens in the vanished-job guard (#791 forward-fix) (#815) |
| #388 | tools | code | 556 | 8 | 5(3) | 4/67 | 0/4 | 2026-07-18 | feat(cron): model='auto' pins a new LLM cron to the creating agent's own model (#388) |
| #203 | tools | code | 550 | 7 | 5(3) | 12/101 | 0/2 | 2026-07-05 | feat: /boomerang command + delegate_task inherit_context (isolated subagent that inherits session co |
| #556 | tools | code | 510 | 5 | 3(3) | 0/83 | 1/3 | 2026-08-10 | fix(kanban): stamp session provenance on comments written from execute_code (#556) |
| #130 | cron | code | 501 | 4 | 1(3) | 0/51 | 0/1 | 2026-06-30 | feat(cron): per-job cross-provider fallback opt-in + LOUD alert when fallback fires (#130) |
| #71 | tools | code | 497 | 2 | 1(1) | 10/68 | 0/4 | 2026-06-21 | fix(send_message): route bare in-turn target to current channel, not home (#71) |
| nopr:f578c82019 | tools | code | 497 | 2 | 1(1) | 10/68 | 0/4 | 2026-06-21 | fix(send_message): route bare in-turn target to current channel, not home |
| #922 | tools | code | 412 | 9 | 6(3) | 6/50 | 0/0 | 2026-09-23 | fix: guard delegate and cron flagship routes with landed policy (#922) |
| #462 | cron | code | 404 | 4 | 3(3) | 7/47 | 2/4 | 2026-07-27 | fix(gateway): let the shutdown drain actually cancel in-flight cron scripts (#462) |
| #323 | cron | code | 350 | 4 | 2(3) | 3/47 | 0/3 | 2026-07-13 | fix(cron): swallow transient provider-capacity 503s on recurring jobs; name the condition (#323) |
| #920 | cron | code | 326 | 3 | 1(2) | 4/105 | 1/4 | 2026-09-23 | fix(lifecycle-guard): scan Python heredocs without shell-reference walks (#920) |
| #1026 | cron | code | 322 | 2 | 1(3) | 2/64 | 0/5 | 2026-09-25 | fix(cron): honour the fleet host-down gate in cron failure delivery (#1026) |
| #553 | tools | code | 304 | 3 | 1(1) | 2/31 | 0/0 | 2026-08-09 | fix(terminal): stop the silent output blackout when stdout fd >= FD_SETSIZE (#553) |
| #1027 | cron | code | 294 | 5 | 1(3) | 8/45 | 0/3 | 2026-09-25 | fix(cron): per-job script ceiling = schedule interval (or timeout_s), clamped to global; log PHASE=c |
| #614 | cron | code | 289 | 2 | 2(2) | 1/46 | 0/2 | 2026-08-19 | fix(lifecycle-guard): treat heredoc bodies as data, not commands (#614) |
| #397 | tools | code | 282 | 2 | 2(2) | 5/51 | 0/3 | 2026-07-18 | fix(cron): block deliver=origin on sub-hourly jobs at create/update time (#397) |
| #733 | tools | code | 279 | 3 | 1(3) | 0/24 | 0/1 | 2026-09-19 | fix(delegation): boot-scope the durable cancel so a one-shot exit can't kill the gateway's delegatio |
| nopr:d5b2992f2d | cron | code | 276 | 4 | 3(3) | 10/55 | 0/3 | 2026-06-05 | feat(cron): hermes cron run <id> --wait — synchronous single-job execution |
| #411 | tools | code | 275 | 2 | 2(2) | 2/43 | 0/2 | 2026-07-21 | fix(cronjob): accept flat model-string arg + warn on silently-dropped model spec (#411) |
| #209 | tools | code | 263 | 1 | 1(1) | 4/84 | 2/10 | 2026-07-05 | fix(video_analyze): video-capable provider routing + yt-dlp page-URL ingestion (#209) |
| #641 | tools | code | 256 | 2 | 2(2) | 3/45 | 0/3 | 2026-08-29 | fix(cron): refuse cross-vendor model/provider pairs at create/update time (#641) |
| #487 | tools | code | 253 | 3 | 0(0) | 0/5 | 0/1 | 2026-08-08 | fix(delegation): tombstones must say whether the dead attempt ever started (#487) |
| #408 | tools | code | 243 | 6 | 3(3) | 2/17 | 0/0 | 2026-07-20 | fix: park delivery-exhausted orphan async-delegations + throttle webhook invalid-sig warning (#408) |
| #1017 | cron | code | 236 | 2 | 1(2) | 7/76 | 0/2 | 2026-09-24 | fix(lifecycle-guard): gate the read-only Python path mask on a whole-body allowlist (#1017) |
| #603 | tools | code | 231 | 5 | 0(0) | 2/29 | 0/2 | 2026-08-18 | fix(lazy-deps): an importable SDK is available even when installs are refused (#603) |
| #577 | tools | code | 229 | 3 | 0(0) | 1/29 | 0/2 | 2026-08-11 | fix(terminal): the advertised foreground cap must match the enforced one (#577) |
| nopr:bb4e64499c | cron | code | 227 | 2 | 2(3) | 0/27 | 0/1 | 2026-06-05 | fix(cron): pin fallback chain to job's provider — codex job can't silently become opus |
| #839 | tools | code | 222 | 2 | 1(3) | 1/24 | 0/0 | 2026-09-22 | kanban: make the survivor escape hatch reachable from the kanban_complete TOOL (t_52abef50) (#839) |
| #345 | tools | code | 221 | 10 | 5(3) | 0/11 | 0/0 | 2026-07-14 | fix: make the full suite reliable on macOS (#345) |
| #415 | tools | code | 218 | 2 | 0(0) | 0/9 | 0/0 | 2026-07-23 | fix(process): bound the orphaned-pipe drain in _reconcile_local_exit (#415) |
| #632 | cron | code | 217 | 3 | 2(2) | 1/51 | 3/5 | 2026-08-21 | feat(cron): humanize cron expressions in the schedule display field (greenhouse t_ef14a609) (#632) |
| #278 | cron | code | 216 | 3 | 2(3) | 0/17 | 0/1 | 2026-07-10 | fix(cron): bind authoritative delivery target into cron worker turns (origin misroute) (#278) |
| #1024 | tools | code | 216 | 4 | 3(3) | 7/46 | 0/1 | 2026-09-25 | fix(kanban): kanban_attach reads a host path byte-exact instead of making the model re-type base64 ( |
| #875 | tools | code | 212 | 2 | 1(3) | 4/26 | 0/0 | 2026-09-23 | kanban: the complete TOOL must accept the qualified survivor claim its refusals name (#875) |
| #1032 | cron | code | 209 | 2 | 1(2) | 1/46 | 0/2 | 2026-09-25 | fix(cron): never persist the 'auto' model sentinel in the job store (t_2f1ca8d4) (#1032) |
| #521 | cron | code | 205 | 2 | 0(0) | 1/46 | 0/1 | 2026-08-08 | fix(cron): reconcile jobs.json when a run's owner dies mid-execution (#521) |
| #933 | cron | code | 194 | 3 | 1(2) | 0/61 | 0/1 | 2026-09-23 | fix: preserve heredoc command ownership in lifecycle guard (#933) |
| #387 | tools | code | 186 | 3 | 0(0) | 0/21 | 2/2 | 2026-07-18 | feat(discord): add react/unreact actions to the discord tool (#387) |
| #818 | cron | code | 184 | 2 | 1(2) | 2/20 | 0/1 | 2026-09-21 | fix(lifecycle-guard): stop fail-closing on an oversized non-executable data file at command position |
| #615 | tools | code | 182 | 2 | 0(0) | 1/20 | 0/2 | 2026-08-19 | fix(search_files): auto-select rg's PCRE2 engine for look-around patterns (#615) |
| #575 | tools | code | 173 | 4 | 2(3) | 2/21 | 0/2 | 2026-08-11 | fix(terminal): make the foreground-timeout and disk-warning caps config keys (#575) |
| #532 | cron | code | 170 | 4 | 2(3) | 1/12 | 0/0 | 2026-08-08 | fix(cron): an INTERRUPTED run records last_status=unknown, not error (#532) |
| #126 | tools | code | 170 | 4 | 3(3) | 2/19 | 0/0 | 2026-06-30 | feat(cron): per-job reasoning_effort override (#126) |
| #477 | cron | code | 167 | 2 | 1(3) | 2/21 | 0/1 | 2026-08-06 | fix(cron): resolve dict-form job model instead of reporting "no model configured" (#477) |
| #484 | tools | code | 166 | 2 | 2(2) | 0/24 | 0/0 | 2026-08-07 | fix(cron): refuse a bare-platform deliver that would silently hit the home channel (#484) |
| nopr:4d626ac813 | tools | code | 159 | 3 | 2(1) | 0/5 | 0/0 | 2026-07-10 | fix(terminal): exclude session-identity vars from the shared env snapshot |
| #618 | tools | code | 158 | 3 | 2(2) | 0/2 | 0/0 | 2026-08-19 | fix(security): `read_secrets` must not fire on words ending in "cat" (#618) |
| #583 | tools | code | 153 | 2 | 2(3) | 1/8 | 0/0 | 2026-08-11 | fix(delegation): strip blocked explicit child toolsets (#583) |
| #927 | tools | code | 152 | 6 | 4(3) | 1/19 | 0/0 | 2026-09-23 | fix(kanban): verify inline attachment digest and preserve raw diff (#927) |
| #351 | cron | code | 151 | 2 | 1(2) | 1/29 | 0/1 | 2026-07-15 | fix(cron): fail loud when a pytest context writes a NON-TEMP cron store (#351) |
| #788 | tools | code | 149 | 2 | 0(0) | 2/23 | 0/0 | 2026-09-21 | fix(process): wait for real child exit after stdout closes (#788) |
| #830 | cron | code | 145 | 2 | 1(2) | 5/31 | 0/1 | 2026-09-21 | fix(cron): reap orphaned .fire-*.lock files (>60s, zero-byte, unheld, hash not owned by a current jo |
| #389 | tools | code | 143 | 2 | 2(2) | 1/26 | 0/1 | 2026-07-18 | fix(cron): surface unsafe creation shapes immediately (#389) |
| #786 | tools | code | 139 | 4 | 2(2) | 0/1 | 0/0 | 2026-09-20 | fix(approvals): honor mode=off on the shared SSH-config gate (#786) |
| #482 | cron | code | 135 | 2 | 1(3) | 2/15 | 0/0 | 2026-08-07 | fix(cron): dict-form job model + ticker execution-ledger never closed (#482) |
| #684 | tools | code | 134 | 3 | 2(3) | 0/5 | 0/0 | 2026-09-13 | fix(kanban): canonicalize chat_type on the WRITE path too (#684) |
| #662 | tools | code | 128 | 3 | 1(3) | 0/21 | 0/0 | 2026-09-09 | registry: reject model-supplied tool arguments the schema does not declare (#662) |
| nopr:6ae6cbe84c | cron | code | 127 | 2 | 1(3) | 0/14 | 0/1 | 2026-06-05 | feat(cron): per-job fallback chain (same-provider resilience) |
| #319 | cron | code | 126 | 2 | 2(2) | 0/34 | 0/2 | 2026-07-12 | fix(guard): stop lifecycle guard false-positiving on skill paths + quoted data (#319) |
| #162 | tools | code | 126 | 2 | 1(2) | 0/17 | 0/0 | 2026-07-01 | fix(skills): resolve skills-shared from the top-level root, not a profile HERMES_HOME (#162) |
| #5 | tools | code | 125 | 2 | 0(0) | 3/12 | 0/0 | 2026-06-04 | fix(write_file): add post-write read-back verification (#5) |
| nopr:74ed8a85c9 | tools | code | 123 | 2 | 1(2) | 4/18 | 0/1 | 2026-06-04 | feat(approval): env-gated reboot/shutdown downgrade (HERMES_ALLOW_REBOOT) |
| nopr:ae6ebc87c4 | tools | code | 123 | 3 | 2(2) | 0/0 | 0/0 | 2026-07-10 | fix(security+tests): more slice reconciliations — cron approval regression + test drift |
| #348 | cron | code | 122 | 5 | 2(2) | 0/10 | 0/0 | 2026-07-15 | fix(cron): resolve HERMES_HOME live in the cron store, not at import time (#348) |
| #543 | tools | code | 122 | 2 | 1(1) | 0/3 | 0/0 | 2026-08-09 | fix(terminal): stop per-execution identity leaking via the shell snapshot (#543) |
| #717 | tools | code | 119 | 3 | 2(2) | 2/20 | 0/1 | 2026-09-19 | fix(skills): create-time dedupe must see UNSUBSCRIBED shared groups (#717) |
| #624 | cron | code | 115 | 3 | 2(2) | 0/1 | 0/0 | 2026-08-20 | fix(lifecycle-guard): referenced DIRECTORY path is not an unsafe script (#624) |
| nopr:e77a87d63a | cron | code | 114 | 4 | 2(3) | 4/20 | 1/2 | 2026-07-16 | refactor(cron): extract scheduler fork helpers |
| nopr:05d38f1d0d | cron | code | 109 | 2 | 1(3) | 1/10 | 0/0 | 2026-06-04 | feat(cron): per-job api_max_retries override |
| #688 | tools | code | 109 | 3 | 1(3) | 0/9 | 0/0 | 2026-09-13 | fix(delegation): cap JSON outbox replay age at 48 hours (#688) |
| #507 | cron | code | 107 | 2 | 1(3) | 2/14 | 0/0 | 2026-08-08 | fix(cron): `cron run <name> --wait` accepts a job NAME, not just an ID (#507) |
| #360 | tools | code | 107 | 6 | 1(1) | 0/8 | 0/0 | 2026-07-15 | test: port macOS host-environment test-hygiene fixes (+ ripgrep-version robustness) (#360) |
| nopr:124a8c0706 | tools | code | 95 | 4 | 2(1) | 0/4 | 0/0 | 2026-07-10 | fix(terminal): pin snapshot temp name in parent shell; fold review fixes |
| #591 | cron | code | 94 | 2 | 2(2) | 0/17 | 0/0 | 2026-08-15 | fix(cron): lifecycle guard resolves relative scripts through cd chains (#591) |
| #399 | tools | code | 92 | 2 | 0(0) | 0/5 | 0/0 | 2026-07-18 | fix(mcp): normalize wrapped schemas in refresh re-injection so lcm_* tools survive (#399) |
| nopr:116493ff97 | cron | code | 91 | 2 | 2(2) | 0/17 | 0/1 | 2026-07-10 | fix(guard): exempt ssh-wrapped remote gateway restarts from lifecycle block |
| #204 | tools | code | 87 | 2 | 1(3) | 0/4 | 0/0 | 2026-07-05 | fix(delegate): boomerang inherit reads _session_messages (gateway live-transcript source) (#204) |
| #98 | tools | code | 81 | 3 | 2(3) | 0/15 | 0/0 | 2026-06-22 | fix(kanban): fall through to os.environ when session_id contextvar is empty (ACP regression from #97 |
| #16 | cron | code | 80 | 3 | 2(3) | 0/9 | 1/1 | 2026-06-07 | cron: cleaner status-aware delivery framing (no double-wrap on failure) (#16) |
| #689 | tools | code | 77 | 4 | 1(3) | 0/0 | 0/0 | 2026-09-13 | refactor(delegation): retire unclaimed SQLite acknowledgement helper (#689) |
| #252 | tools | code | 71 | 2 | 0(0) | 1/15 | 0/0 | 2026-07-10 | fix(browser): is_camofox_mode honors config.yaml browser.cdp_url (#252) |
| nopr:9a1db22e58 | tools | code | 70 | 2 | 0(0) | 0/6 | 0/0 | 2026-06-05 | fix(patch): error instead of silent no-op success on header-less V4A patch |
| #148 | tools | code | 67 | 2 | 2(2) | 0/8 | 0/0 | 2026-07-01 | fix(cron): reject inline script bodies and filename+args in `script=` at create time (#148) |
| #314 | tools | code | 59 | 2 | 2(1) | 0/2 | 0/0 | 2026-07-12 | fix(vision_analyze): accept path/image_path/file_path aliases for image_url (#314) |
| #594 | cron | code | 54 | 1 | 1(2) | 0/13 | 0/0 | 2026-08-16 | fix(guard): a commit message that documents a lifecycle command is data, not an invocation (#594) |
| #565 | cron | code | 53 | 2 | 1(3) | 0/3 | 0/1 | 2026-08-10 | fix(cron): make manual one-shot runs execute (#565) |
| #199 | cron | code | 53 | 2 | 2(3) | 0/14 | 0/1 | 2026-07-05 | fix(cron): treat a code-formatted [SILENT] sentinel as silence (#199) |
| nopr:8d20d24ed0 | tools | code | 53 | 4 | 1(3) | 2/5 | 0/0 | 2026-07-24 | fix(sync): CI round-3 — durable-route wake target + nousbot contributor mapping |
| #473 | cron | code | 47 | 2 | 2(3) | 0/6 | 0/0 | 2026-08-06 | fix(cron): attach job identity to turn telemetry (#473) |
| #394 | cron | code | 43 | 2 | 1(3) | 0/1 | 0/0 | 2026-07-18 | fix(cron): provider-only fallback entries count as DECLARED (Greptile #385) (#394) |
| #134 | cron | code | 43 | 3 | 1(3) | 0/4 | 1/1 | 2026-06-30 | feat(cron): deliver the fallback alert UNWRAPPED (drop the Cronjob-Failed envelope) (#134) |
| nopr:091b3f915d | tools | code | 33 | 1 | 0(0) | 2/7 | 0/0 | 2026-06-29 | fix(moa): empty-content-as-success, ignored aggregator_model, OpenRouter gpt- temperature guard |
| nopr:30a8a063e9 | tools | code | 29 | 2 | 2(3) | 0/9 | 0/0 | 2026-05-14 | fix(delegation): allow explicit code_execution + honor explicit toolsets |
| nopr:c559189dd2 | cron | code | 10 | 2 | 2(2) | 0/2 | 0/0 | 2026-07-10 | fix(guard): close IPv4-mapped-IPv6 loopback + env-var-assignment bypasses (Greptile P1s) |
| nopr:7e505547a7 | tools | code | 9 | 1 | 0(0) | 0/1 | 0/0 | 2026-06-29 | fix(moa): aggregator empty-content raises instead of returning empty success |
| #139 | cron | code | 8 | 2 | 2(3) | 0/0 | 0/0 | 2026-06-30 | style(cron): capitalize the cron-response 'Job ID' label (#139) |
| #28 | cron | code | 8 | 2 | 2(3) | 0/0 | 0/0 | 2026-06-09 | style(cron): use 🪪 job_id: prefix instead of parenthesized (job_id: …) (#28) |
| nopr:10a1424335 | cron | code | 4 | 2 | 2(2) | 0/1 | 0/0 | 2026-07-10 | fix(guard): block bracketed IPv6 loopback (ssh [::1]) — Greptile P1 |
