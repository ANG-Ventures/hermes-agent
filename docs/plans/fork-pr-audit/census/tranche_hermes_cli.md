# Tranche: hermes_cli — 177 rows

absorb = substantive added src lines found verbatim on upstream/main (hit/total); sym = added def/class names found. Signal only — read README §0.

| key | subsystem | kind | loc | files | conflict-files (syncs) | absorb src | sym | date | subject |
|---|---|---|---|---|---|---|---|---|---|
| #829 | hermes_cli | code | 4729 | 15 | 3(2) | 50/937 | 8/58 | 2026-09-24 | feat(kanban): batch set-model selectors + time-boxed lane-model override (#829) |
| #785 | hermes_cli | code | 2473 | 10 | 2(2) | 17/264 | 0/8 | 2026-09-21 | fix(kanban): containment guards must reject path == root before rmtree (#785) |
| #924 | hermes_cli | code | 1941 | 6 | 1(2) | 5/274 | 4/16 | 2026-09-23 | fix(kanban): re-port canonical survivor binding onto current main (#924) |
| #842 | hermes_cli | code | 1718 | 4 | 2(2) | 2/191 | 0/9 | 2026-09-22 | kanban: reclamation consults the RECORDED survivor instead of re-deriving from stale bases (#842) |
| #953 | hermes_cli | code | 1649 | 5 | 2(2) | 8/246 | 4/21 | 2026-09-24 | kanban dispatcher: pool-health default probe + escalating rate-limit backoff + tick circuit (t_32f44 |
| #808 | hermes_cli | code | 1645 | 4 | 2(2) | 20/314 | 9/32 | 2026-09-21 | kanban: auto-resolve blocks whose gate PR has already merged (#808) |
| #987 | hermes_cli | code | 1617 | 15 | 5(3) | 15/228 | 0/16 | 2026-09-24 | feat(kanban): post-#951 — worker cards inherit parent home; list home-first + --all; pings carry hom |
| #773 | hermes_cli | code | 1520 | 9 | 5(3) | 7/85 | 1/5 | 2026-09-20 | fix(gateway): stop per-message synchronous work on the event loop (config lock, realpath, atomic wri |
| #797 | other:cli.py | code | 1510 | 18 | 7(3) | 4/234 | 3/8 | 2026-09-23 | fix(usage): carry UNKNOWN through the persisted schema, cumulative totals and the Langfuse export (# |
| #568 | hermes_cli | code | 1396 | 7 | 3(2) | 6/265 | 4/9 | 2026-08-10 | fix(kanban): repair alt-key routing identity without guessing (#568) |
| #545 | hermes_cli | code | 1373 | 9 | 3(3) | 11/149 | 0/8 | 2026-08-09 | feat(kanban): attribute comments to their run and session (#545) |
| #900 | hermes_cli | code | 1271 | 9 | 6(3) | 21/250 | 7/27 | 2026-09-23 | feat(kanban): fan-out brakes — worker-created cards park in triage + per-board 24h USD ceiling (#900 |
| #951 | hermes_cli | code | 1267 | 5 | 4(3) | 7/216 | 5/19 | 2026-09-24 | feat(kanban): home-session card ownership — session stamping, --home, foreign-session mutation guard |
| #921 | hermes_cli | code | 1246 | 11 | 4(2) | 20/157 | 1/6 | 2026-09-23 | fix(kanban): fail closed on unprovable worker liveness (#921) |
| #848 | hermes_cli | code | 1170 | 8 | 2(2) | 3/198 | 4/7 | 2026-09-22 | fix(kanban): bind an explicit survivor claim to the card that names it (#848) |
| #655 | hermes_cli | code | 1165 | 15 | 6(2) | 24/205 | 4/17 | 2026-09-09 | fix(kanban): preserve quota exits and defer exhausted providers (#655) |
| #907 | hermes_cli | code | 1149 | 3 | 2(2) | 4/168 | 0/8 | 2026-09-23 | fix(kanban): refuse destructive writes when pin contradicts requested board (t_e1c2dae5) (#907) |
| #886 | hermes_cli | code | 1097 | 6 | 2(2) | 0/161 | 4/6 | 2026-09-23 | fix(kanban): close the 4 P1 FleetReview recorded on #848's head 0757be86 (#886) |
| #831 | hermes_cli | code | 1054 | 8 | 4(3) | 10/156 | 0/12 | 2026-09-22 | fix(kanban): refuse a non-spawnable review assignee at request-review (#831) |
| #897 | hermes_cli | code | 1020 | 11 | 6(3) | 10/228 | 1/10 | 2026-09-23 | kanban: 'superseded' terminal outcome for an already-satisfied premise (t_90178503) (#897) |
| #486 | hermes_cli | code | 1018 | 12 | 7(3) | 39/111 | 2/2 | 2026-08-07 | feat(kanban): per-task reasoning effort (DB + dispatcher + CLI + tool) (#486) |
| #874 | hermes_cli | code | 1007 | 5 | 2(3) | 3/175 | 1/7 | 2026-09-23 | fix(guards): deny the root HERMES_HOME actually resolves to, not <home>/.hermes (t_5bfcbf14) (#874) |
| #783 | hermes_cli | code | 1007 | 6 | 3(2) | 8/210 | 9/18 | 2026-09-20 | fix(kanban): preserve implementation survivors before completion and cleanup (#783) |
| #804 | hermes_cli | code | 1002 | 6 | 4(2) | 10/210 | 5/17 | 2026-09-23 | fix(kanban): fail closed when scratch mount is unavailable (#804) |
| #816 | hermes_cli | code | 992 | 5 | 2(3) | 10/93 | 15/26 | 2026-09-21 | fix(models): make CANONICAL_PROVIDERS plugin auto-extend LAZY (fixes circular import for plugins imp |
| #182 | hermes_cli | code | 936 | 2 | 2(3) | 33/84 | 4/10 | 2026-07-02 | fix(dashboard): offload session read endpoints (#182) |
| #950 | hermes_cli | code | 913 | 4 | 2(2) | 6/93 | 4/12 | 2026-09-24 | fix(kanban): pin agree-checks + managed-scratch containment spelling-blind (t_ee808d83 r3, #948 foll |
| #854 | hermes_cli | code | 913 | 5 | 1(2) | 18/181 | 7/15 | 2026-09-22 | feat(kanban): append-only mutation journal + replay, outside the kanban home (#854) |
| #947 | hermes_cli | code | 873 | 5 | 2(2) | 4/183 | 9/19 | 2026-09-24 | feat(kanban): hermes kanban clone - reference clones against shared fleet mirrors (t_dad1edd7) (#947 |
| #555 | hermes_cli | code | 871 | 5 | 2(2) | 13/118 | 1/4 | 2026-08-10 | fix(kanban): stop identity-less notify subs minting a phantom 2nd session (#555) |
| #513 | hermes_cli | code | 861 | 4 | 3(2) | 15/172 | 0/5 | 2026-08-08 | fix(kanban): give `triage` a supported exit and make downstream stranding loud (#513) |
| #952 | hermes_cli | code | 860 | 8 | 5(2) | 11/151 | 2/11 | 2026-09-24 | fix(kanban): resume dependency-wait PR and page stranded ready cards (#952) |
| #707 | hermes_cli | code | 819 | 6 | 5(3) | 10/139 | 0/6 | 2026-09-17 | fix(kanban): HERMES_KANBAN_DB pin vs explicit board arg — scoped, per-call-site contradiction guard  |
| #833 | hermes_cli | code | 817 | 3 | 1(2) | 3/110 | 1/6 | 2026-09-21 | fix(kanban): keep ALL subprocess I/O out of the PR-gate dispatch lock (#833) |
| #811 | hermes_cli | code | 817 | 5 | 1(2) | 2/50 | 1/3 | 2026-09-22 | post-merge FleetReview on #785: fix 6 findings, dispute 1, partial 1 (kanban deletion gates) (#811) |
| #590 | hermes_cli | code | 789 | 4 | 3(2) | 8/188 | 0/11 | 2026-08-18 | [verified] warn on kanban dispatch file collisions (#590) |
| #795 | hermes_cli | code | 763 | 6 | 2(2) | 2/130 | 10/12 | 2026-09-21 | fix(kanban): accept a VERIFIED external survivor when the implementation is not in the workspace (#7 |
| #514 | hermes_cli | code | 763 | 5 | 3(3) | 5/135 | 1/5 | 2026-08-08 | feat(kanban): a DEPLOY must never be gated on a DISCOVERY — add a non-blocking link kind (#514) |
| #962 | hermes_cli | code | 762 | 5 | 2(2) | 2/185 | 6/9 | 2026-09-24 | WIP: verify merged survivor SHAs and record no-ref closeouts (#962) |
| #1059 | hermes_cli | code | 743 | 8 | 4(3) | 6/142 | 0/8 | 2026-09-25 | fix(kanban): refuse mid-turn provider failover for a card-pinned worker (#1059) |
| #888 | hermes_cli | code | 727 | 3 | 0(0) | 1/56 | 2/4 | 2026-09-23 | fix(kanban): survivor capture cost is per-REMOTE-REF, starving the kanban_complete tool at 420s (#88 |
| #819 | hermes_cli | code | 725 | 6 | 1(1) | 3/62 | 0/3 | 2026-09-21 | fix(plugins): isolate concurrent hook callbacks (#819) |
| #308 | hermes_cli | code | 723 | 2 | 2(3) | 57/122 | 1/1 | 2026-07-11 | fix(dashboard): offload 10 blocking SessionDB calls off the event loop (#308) |
| #901 | hermes_cli | code | 718 | 4 | 0(0) | 2/96 | 3/6 | 2026-09-23 | fix(kanban): bound and prune the survivor walk; ask git before walking a home (#901) |
| #802 | hermes_cli | code | 718 | 3 | 0(0) | 5/63 | 1/6 | 2026-09-21 | feat(fallback): let a chain entry derive its rungs from a registered source (#802) |
| #946 | hermes_cli | code | 691 | 5 | 1(2) | 4/82 | 1/4 | 2026-09-24 | fix(kanban): claim guard sees open prior runs and every spawn of a run (#946) |
| #943 | hermes_cli | code | 654 | 11 | 7(3) | 12/142 | 1/9 | 2026-09-23 | Detect heartbeat-only Kanban stalls and route capped pools (#943) |
| #1002 | hermes_cli | code | 650 | 5 | 4(2) | 5/147 | 6/12 | 2026-09-24 | feat(kanban): reviewer-swarm gates — per-profile cap mapping, load-gated spawns, review round cap, m |
| #720 | hermes_cli | code | 634 | 5 | 4(3) | 6/74 | 0/2 | 2026-09-19 | fix(model): resolve `model.aliases` on every model-string entry point, not just /model (#720) |
| #823 | hermes_cli | code | 632 | 8 | 4(2) | 18/90 | 0/6 | 2026-09-21 | fix(kanban): gate flagship worker models (#823) |
| #586 | hermes_cli | code | 624 | 6 | 4(3) | 6/109 | 1/4 | 2026-08-12 | fix(kanban): make worker authority non-transitive across process boundaries (#586) |
| #566 | hermes_cli | code | 599 | 7 | 6(3) | 7/49 | 0/0 | 2026-08-10 | fix(kanban): carry the wake's full key identity as data so alt-id and Slack chats can't phantom (#56 |
| #636 | hermes_cli | code | 580 | 9 | 4(3) | 3/69 | 0/3 | 2026-08-31 | fix(kanban): close the ambient-env reads the OWNER_PID anchor left open (#636) |
| #276 | hermes_cli | code | 577 | 5 | 2(3) | 8/35 | 0/0 | 2026-07-10 | fix(kanban): decompose inherits board default_workdir + sibling worktree isolation (#276) |
| #384 | hermes_cli | code | 564 | 2 | 2(2) | 2/110 | 0/7 | 2026-07-18 | fix(kanban): prevent PR-state budget starvation (#384) |
| #39 | hermes_cli | code | 528 | 5 | 4(3) | 4/51 | 0/1 | 2026-06-14 | feat(kanban): per-task model override write-path (#39) |
| #872 | hermes_cli | code | 518 | 2 | 0(0) | 1/87 | 2/7 | 2026-09-22 | fix(kanban): a broken object store names the repo, the lender and a remedy (#872) |
| #1057 | hermes_cli | code | 512 | 2 | 0(0) | 5/99 | 0/9 | 2026-09-25 | fix(kanban pr-gate): MERGED is not DEPLOYED — hold deploy-premised cards until the merge is in the l |
| #313 | hermes_cli | code | 512 | 3 | 1(1) | 1/23 | 0/0 | 2026-07-12 | fix(plugins): isolate background-review tool whitelist per-task (threading.local -> ContextVar) (#31 |
| #158 | hermes_cli | code | 507 | 10 | 1(3) | 6/201 | 0/0 | 2026-07-01 | feat(desktop): backend runs from runtime deploy tree by default (+ config override) (#158) |
| #579 | hermes_cli | code | 493 | 3 | 2(2) | 6/73 | 2/6 | 2026-08-11 | fix(kanban): zero-byte board .db files must not exist, and must not read as empty boards (#579) |
| #948 | hermes_cli | code | 489 | 3 | 2(2) | 11/106 | 1/9 | 2026-09-24 | fix(kanban): gc uses one cached machine-wide cwd scan, not per-path lsof +D (t_ee808d83) (#948) |
| #359 | other:locales | code | 478 | 50 | 14(3) | 1/68 | 0/0 | 2026-07-15 | fix(reasoning): align non-desktop max parity (#359) |
| #441 | hermes_cli | code | 474 | 6 | 5(3) | 10/100 | 4/15 | 2026-07-26 | feat(tui_gateway): bound heavy session reads (#441) |
| #215 | hermes_cli | code | 473 | 6 | 5(3) | 10/100 | 4/15 | 2026-07-06 | feat(tui_gateway): bound heavy session reads (#215) |
| #682 | hermes_cli | code | 460 | 4 | 2(2) | 0/5 | 0/1 | 2026-09-12 | fix(gateway): canonicalize BOTH sides of the routing-lane comparison (#682) |
| #771 | hermes_cli | code | 457 | 5 | 2(2) | 1/53 | 0/4 | 2026-09-20 | fix(kanban): run worker gateways at background CPU priority (#771) |
| #960 | hermes_cli | code | 453 | 5 | 5(3) | 4/57 | 2/3 | 2026-09-24 | fix(kanban): judge goal deliverables before completion; fail open for operator errors (#960) |
| #809 | hermes_cli | code | 451 | 2 | 1(2) | 2/48 | 0/1 | 2026-09-21 | kanban: block on failure #1 when a card's workspace anchor can never resolve (#809) |
| #983 | hermes_cli | code | 450 | 4 | 1(2) | 5/56 | 0/1 | 2026-09-24 | fix(kanban): reclaim pid-less claim when local claimer is dead (#983) |
| #515 | hermes_cli | code | 441 | 5 | 3(2) | 8/107 | 2/6 | 2026-08-09 | fix(kanban): guard delete_task against live workers; add HERMES_KANBAN_SANDBOX (#515) |
| #336 | hermes_cli | code | 441 | 8 | 6(2) | 12/62 | 2/6 | 2026-07-15 | fix(kanban): clear respawn guard for closed PRs (#336) |
| #889 | hermes_cli | code | 419 | 10 | 2(2) | 0/54 | 0/3 | 2026-09-23 | fix(cli): a printed hint must survive a real SHELL, not just shlex.split (#889) |
| #774 | hermes_cli | code | 419 | 3 | 1(2) | 1/42 | 1/6 | 2026-09-21 | fix(config): stop warning on ${env:} refs resolved before .env loads (#774) |
| #1018 | hermes_cli | code | 407 | 2 | 0(0) | 1/81 | 0/3 | 2026-09-24 | fix(kanban survivor): hold ignored orphan bytes on in-place repo replacement and unbound cleanup (t_ |
| #664 | hermes_cli | code | 401 | 3 | 3(2) | 5/65 | 0/3 | 2026-09-09 | fix(kanban): stale last_failure_error no longer poisons the respawn guard; age-bound blocker_auth; n |
| #870 | hermes_cli | code | 395 | 2 | 0(0) | 1/62 | 1/4 | 2026-09-22 | fix(kanban): PR-gate must not resolve a bare #N against an ENCLOSING repo (#870) |
| #877 | hermes_cli | code | 389 | 2 | 0(0) | 2/64 | 0/2 | 2026-09-22 | fix(kanban): PR-gate must not resolve a bare #N against prose or a contradicted repo (#877) |
| #980 | hermes_cli | code | 380 | 4 | 2(3) | 1/34 | 0/1 | 2026-09-24 | fix(model): accept Discord 'name:provider/model' on every surface; refuse unknown provider (#980) |
| #665 | hermes_cli | code | 355 | 5 | 2(2) | 3/47 | 1/9 | 2026-09-10 | fix(kanban): upstream capacity overload (529/503/overloaded) preserves retries like a quota wall (#6 |
| #982 | hermes_cli | code | 342 | 4 | 2(2) | 2/48 | 0/2 | 2026-09-24 | feat(kanban): home = session lineage (home_ids) + kanban.home_guard (default refuse) (#982) |
| #949 | hermes_cli | code | 337 | 4 | 3(2) | 3/70 | 0/2 | 2026-09-24 | fix(kanban): cohort-death guard + signal receipts for externally-ended workers (t_0c1ebbae) (#949) |
| #800 | hermes_cli | code | 332 | 5 | 2(2) | 3/30 | 0/2 | 2026-09-21 | fix(kanban): expose board tick-lock holder on skipped dispatch (#800) |
| #706 | hermes_cli | code | 330 | 2 | 1(2) | 3/55 | 0/4 | 2026-09-17 | fix(kanban): alias retired board slugs; phantom-board guard must read the WAL (#706) |
| #475 | hermes_cli | code | 330 | 4 | 3(3) | 5/76 | 2/2 | 2026-08-06 | fix(kanban): restore fork contracts clobbered by 11cffc4d5 + catalog test-hermeticity gate (#475) |
| #844 | hermes_cli | code | 326 | 3 | 2(2) | 1/43 | 0/0 | 2026-09-22 | kanban: exit 126/127 from a worker is INFRASTRUCTURE, not a task failure (t_263e7303) (#844) |
| #778 | hermes_cli | code | 326 | 2 | 2(2) | 0/31 | 1/2 | 2026-09-21 | fix(backup): size the SQLite safe-copy bound so a 3 GB state.db is not dropped from --quick snapshot |
| #18 | other:locales | code | 325 | 18 | 3(3) | 3/17 | 0/0 | 2026-06-07 | fix(compress): two-line readout — Chat size + Full request size (#18) |
| #940 | hermes_cli | code | 315 | 3 | 2(2) | 7/48 | 1/2 | 2026-09-23 | fix(kanban): home-rooted dir: cards stop owning every scratch workspace; gc reaps old done workspace |
| #1050 | hermes_cli | code | 311 | 2 | 1(2) | 1/42 | 1/3 | 2026-09-25 | fix(kanban): claim-guard owner identity uses a clock-step-immune start token (t_21dfa673) (#1050) |
| #712 | hermes_cli | code | 296 | 4 | 4(3) | 1/21 | 0/1 | 2026-09-19 | fix(model): honour excluded_providers for user-defined and injected rows (#712) |
| #985 | hermes_cli | code | 292 | 4 | 2(2) | 1/42 | 0/4 | 2026-09-24 | fix(kanban): budget per-tick relay spawns against eligible subs (#985) |
| #595 | hermes_cli | code | 288 | 2 | 2(2) | 11/46 | 0/1 | 2026-08-16 | fix(models): honest model-verification warning + transient-probe retry (#595) |
| #1039 | hermes_cli | code | 261 | 2 | 1(2) | 1/35 | 0/4 | 2026-09-25 | fix(kanban): spelling-blind mount-root match in workspace admission (t_800d50d2) (#1039) |
| #588 | hermes_cli | code | 259 | 4 | 2(2) | 1/35 | 0/0 | 2026-08-13 | fix(kanban): stop #568's creator binding from vetoing worker-card wake evidence (#588) |
| #803 | hermes_cli | code | 257 | 5 | 5(2) | 6/57 | 0/4 | 2026-09-21 | fix(kanban): release dependency-backed creation holds (#803) |
| #386 | hermes_cli | code | 256 | 4 | 1(2) | 0/29 | 1/1 | 2026-07-18 | fix(model): hot-reload model.aliases without a gateway restart (#386) |
| #914 | hermes_cli | code | 253 | 2 | 0(0) | 0/35 | 0/4 | 2026-09-23 | perf(kanban): key the budget ledger sum on the window, not the board path (#914) |
| #374 | other:cli.py | code | 251 | 3 | 1(2) | 1/41 | 0/1 | 2026-07-16 | fix(cli): fail loud on -m provider/model when provider does not resolve (no silent strip onto defaul |
| nopr:07d219143b | hermes_cli | code | 250 | 2 | 0(0) | 0/51 | 0/1 | 2026-08-10 | fix(kanban): a COUNT in a card body is a timestamped claim, not current state |
| #837 | hermes_cli | code | 242 | 2 | 0(0) | 0/5 | 0/0 | 2026-09-22 | kanban: make the operator survivor reachable when a recorded repo vanishes (#837) |
| #956 | hermes_cli | code | 238 | 6 | 4(2) | 4/60 | 0/1 | 2026-09-24 | fix(kanban): reject recycled PIDs as prior worker owners (#956) |
| #232 | other:locales | code | 237 | 18 | 2(3) | 0/21 | 0/0 | 2026-07-08 | feat(merge): mark source session as merged + terse accurate merge note (#232) |
| #994 | hermes_cli | code | 233 | 2 | 1(2) | 7/60 | 0/2 | 2026-09-24 | fix(kanban): end runs left 'running' on done/archived cards (#994) |
| nopr:9f9bf61409 | hermes_cli | code | 231 | 2 | 1(2) | 6/56 | 0/3 | 2026-05-13 | fix(model-switch): support inline provider syntax |
| #1034 | hermes_cli | code | 228 | 2 | 0(0) | 1/33 | 0/2 | 2026-09-25 | fix(kanban): malformed recorded landed receipt HOLDs on reclamation instead of escaping _hold (t_ad3 |
| #879 | hermes_cli | code | 225 | 5 | 0(0) | 0/26 | 0/1 | 2026-09-22 | fix(cli): a printed --flag hint must parse as pasted, for any repo key or path (#879) |
| #437 | hermes_cli | code | 219 | 3 | 2(2) | 1/18 | 0/0 | 2026-07-26 | fix(kanban): reclaim must not launder a blocked card into ready (#437) |
| #426 | hermes_cli | code | 217 | 5 | 2(3) | 0/31 | 0/2 | 2026-07-25 | fix(tests): remove two xfail markers left by the 2026-07-23 parity merge (#426) |
| #1011 | hermes_cli | code | 216 | 2 | 1(1) | 1/28 | 0/1 | 2026-09-25 | fix(plugins): keep chained/prefixed diff out of lossy pre_tool_call rewrites (#1011) |
| #653 | hermes_cli | code | 214 | 2 | 2(2) | 4/10 | 0/0 | 2026-09-08 | fix(kanban): honor post-PR requeues in respawn guard (#653) |
| #713 | hermes_cli | code | 213 | 3 | 2(3) | 2/43 | 0/1 | 2026-09-19 | fix(cli): resolve -m <alias> / <provider>/<model> like /model instead of silently falling back (#713 |
| #479 | hermes_cli | code | 208 | 5 | 2(2) | 2/12 | 0/1 | 2026-08-06 | fix(kanban): identify dispatcher chat workers (#479) |
| #235 | other:locales | code | 204 | 18 | 2(3) | 0/14 | 0/0 | 2026-07-08 | feat(merge): reciprocal thread links + footer-consistent message counts (#235) |
| #400 | hermes_cli | code | 197 | 3 | 2(2) | 6/31 | 1/2 | 2026-07-18 | fix(kanban): preserve provider in model overrides (#400) |
| #240 | hermes_cli | code | 191 | 2 | 1(2) | 0/29 | 0/1 | 2026-07-08 | feat(picker): config-driven /model picker hide + order (model.picker) (#240) |
| #989 | hermes_cli | code | 190 | 5 | 4(3) | 0/20 | 1/2 | 2026-09-24 | fix(kanban,launchd): clamp macOS workers to utility QoS; gateway plist ProcessType=Interactive (t_14 |
| #17 | other:locales | code | 183 | 18 | 3(3) | 0/11 | 0/0 | 2026-06-07 | fix(compress): show real measured context alongside estimate; estimate over full transcript (#17) |
| #522 | hermes_cli | code | 178 | 3 | 3(2) | 3/19 | 0/0 | 2026-08-08 | fix(kanban): stop zombie goal loops on ownership loss (#522) |
| #965 | hermes_cli | code | 174 | 2 | 0(0) | 1/16 | 0/1 | 2026-09-24 | fix(kanban): scratch workspace under a dangling gitfile has no survivor (#965) |
| #166 | hermes_cli | code | 173 | 2 | 2(2) | 0/16 | 0/1 | 2026-07-01 | fix(models): dedup namespaced curated vs bare live ids in provider_model_ids merge (#166) |
| #822 | hermes_cli | code | 165 | 7 | 0(0) | 3/16 | 0/0 | 2026-09-21 | fix(git): disable optional locks in bounded probes (#822) |
| #1040 | hermes_cli | code | 164 | 2 | 1(2) | 0/10 | 0/2 | 2026-09-25 | test(kanban): make progress-stall policy tests probe-deterministic (t_5457397a) (#1040) |
| #685 | hermes_cli | code | 162 | 2 | 1(2) | 0/2 | 0/0 | 2026-09-13 | fix(kanban): move the chat_type invariant to the add_notify_sub choke point (#685) |
| #890 | hermes_cli | code | 158 | 2 | 2(2) | 1/28 | 0/1 | 2026-09-23 | fix(kanban): SANDBOX=1 neutralising an explicit path pin is no longer silent (#890) |
| #798 | hermes_cli | code | 156 | 2 | 1(2) | 1/15 | 0/0 | 2026-09-21 | fix(kanban): park a review card — block accepts review, unblock restores it, triage-resolve names th |
| #617 | hermes_cli | code | 153 | 3 | 1(1) | 0/8 | 1/1 | 2026-08-19 | feat(cron): add `hermes cron list --json` for machine-readable job records (#617) |
| #494 | other:cli.py | code | 151 | 2 | 2(2) | 1/9 | 0/1 | 2026-08-07 | fix(kanban): goal-loop block path must pass the ownership guard (#494) |
| #1020 | hermes_cli | code | 135 | 4 | 2(2) | 3/22 | 1/2 | 2026-09-25 | fix(kanban): stale workspaces-root pin no longer HOLDs every completion (#1020) |
| #328 | hermes_cli | code | 130 | 4 | 4(3) | 2/27 | 0/1 | 2026-07-14 | fix(picker): desktop/TUI model picker honors model.picker hide+order + hides failover lanes (#328) |
| #234 | other:locales | code | 129 | 18 | 2(3) | 0/6 | 0/0 | 2026-07-08 | feat(branch/merge): reciprocal parent<->child links in thread branches (#234) |
| #537 | hermes_cli | code | 127 | 2 | 1(2) | 0/4 | 0/0 | 2026-08-09 | fix(config): stop `config set compression.<lcm_knob>` crying wolf (#537) |
| #711 | hermes_cli | code | 124 | 2 | 1(1) | 0/0 | 0/0 | 2026-09-19 | fix(cron): propagate `hermes cron run --wait` failure exit status (#711) |
| #233 | hermes_cli | code | 120 | 2 | 1(2) | 0/3 | 0/0 | 2026-07-08 | feat(picker): hide numbered Claude failover lanes from the /model picker (#233) |
| nopr:ce5f851789 | other:locales | code | 120 | 15 | 0(0) | 0/7 | 0/0 | 2026-07-07 | fix(i18n): add /undo tail-preview keys to all non-English locales |
| #1051 | hermes_cli | code | 117 | 3 | 2(3) | 1/26 | 0/2 | 2026-09-25 | fix(kanban): remap worker assignee='default' to kanban.default_assignee (#1051) |
| nopr:b5d533baa7 | hermes_cli | code | 116 | 12 | 1(3) | 11/21 | 0/0 | 2026-07-24 | fix(sync): CI round-1 fixes — dead import of upstream-deleted module + keychain-guard false anchor |
| #335 | other:locales | code | 115 | 18 | 2(2) | 0/10 | 0/2 | 2026-07-14 | fix(undo): strip injected gateway markers from /undo·/redo tail preview (#335) |
| #858 | hermes_cli | code | 112 | 2 | 1(2) | 0/1 | 0/0 | 2026-09-23 | fix(kanban): gate repair_db(), the second rw door to the live board (t_880f5d17) (#858) |
| #291 | hermes_cli | code | 111 | 4 | 2(3) | 7/29 | 0/2 | 2026-07-10 | perf(dashboard): lightweight profile listing for session-list + cron aggregators (10s -> ~1s) (#291) |
| #654 | hermes_cli | code | 108 | 2 | 2(2) | 0/4 | 0/0 | 2026-09-08 | fix(kanban): respawn guard honors ALL operator requeue verbs, not three of five (#654) |
| #519 | hermes_cli | code | 103 | 3 | 1(2) | 0/7 | 0/0 | 2026-08-08 | fix(kanban): ignore provenance links in triage stranding reports (#519) |
| #391 | other:pyproject.toml | code | 103 | 2 | 1(2) | 0/1 | 0/0 | 2026-07-18 | fix(packaging): declare hermes_state_ext + hermes_undo in py-modules (#391) |
| #760 | hermes_cli | code | 97 | 2 | 2(2) | 0/11 | 0/1 | 2026-09-20 | fix(backup): exclude cache/forensic-*/ and cache/**/*.db from the backup walk (#760) |
| nopr:8f7823887e | hermes_cli | code | 97 | 2 | 1(2) | 0/11 | 0/0 | 2026-05-28 | fix(models): name provider + endpoint in anthropic_messages fallback warning + fix provider_label sh |
| #135 | hermes_cli | code | 94 | 2 | 1(2) | 0/2 | 1/1 | 2026-06-30 | fix(model-picker): don't duplicate the current model when its catalog entry is namespaced (#135) |
| #239 | hermes_cli | code | 92 | 2 | 1(2) | 0/1 | 0/0 | 2026-07-08 | fix(picker): match RENAMED failover-lane slugs (claude-apx-N / claude-bpx-N) (#239) |
| #492 | hermes_cli | code | 91 | 2 | 0(0) | 0/14 | 0/1 | 2026-08-07 | fix(models): dots->hyphens only for Anthropic MODELS, not Anthropic-wire relays (#492) |
| #637 | hermes_cli | code | 87 | 2 | 1(2) | 1/1 | 0/0 | 2026-08-24 | fix(auth): defer hermes_cli.config import until after PROVIDER_REGISTRY exists (#637) |
| #455 | hermes_cli | code | 87 | 2 | 2(2) | 0/14 | 0/1 | 2026-07-27 | fix(backup): skip ephemeral kanban board workspaces in the full-zip walk (#455) |
| #619 | hermes_cli | code | 83 | 2 | 0(0) | 0/1 | 0/0 | 2026-08-20 | feat(doctor): surface the absolute runtime interpreter path (#619) |
| #536 | hermes_cli | code | 83 | 2 | 1(2) | 0/0 | 0/0 | 2026-08-09 | fix(backup): exclude staging DISK IMAGES from the full-tier walk (#536) |
| #471 | hermes_cli | code | 82 | 2 | 0(0) | 0/5 | 0/0 | 2026-08-06 | fix(oneshot): identify kanban worker turns (#471) |
| #200 | hermes_cli | code | 82 | 2 | 2(2) | 0/4 | 0/0 | 2026-07-05 | fix(backup): exclude self-nested offsite tars + managed browser profile from full backup (#200) |
| #466 | hermes_cli | code | 81 | 2 | 1(3) | 0/1 | 0/0 | 2026-08-03 | fix(gateway): mark /curator cli_only — advertised with no gateway handler (#466) |
| nopr:a184062e7b | hermes_cli | code | 77 | 2 | 2(2) | 4/13 | 1/1 | 2026-06-05 | fix(banner): include local HEAD in update-check cache key |
| nopr:d68a4b0fcc | hermes_cli | code | 72 | 1 | 1(2) | 2/29 | 0/1 | 2026-05-28 | fix(model-switch): resolve provider-module plugin providers in switch path |
| nopr:7cb39d1319 | hermes_cli | code | 70 | 1 | 1(2) | 3/27 | 1/1 | 2026-06-21 | fix(backup): exclude browser-automation profiles + bulk media from full backup |
| nopr:8a8b81638c | hermes_cli | code | 69 | 2 | 0(0) | 0/1 | 0/0 | 2026-06-02 | fix(recap): backtick-wrap Files touched paths to stop gateway auto-attach |
| #856 | hermes_cli | code | 67 | 2 | 0(0) | 0/3 | 0/0 | 2026-09-22 | fix(kanban): log git's returncode and stderr when survivor capture fails (#856) |
| #781 | hermes_cli | code | 65 | 2 | 1(2) | 0/4 | 0/0 | 2026-09-21 | fix(kanban): require an open parent for dependency auto-resume (#781) |
| #620 | hermes_cli | code | 58 | 2 | 2(2) | 0/2 | 0/0 | 2026-08-20 | fix(models): strip relay routing prefix before model-listing lookup (#620) |
| #490 | hermes_cli | code | 54 | 2 | 0(0) | 0/1 | 0/0 | 2026-08-07 | fix(kanban-diagnostics): a clean block breaks the crash streak (recovered card stops paging CRITICAL |
| #35 | hermes_cli | code | 53 | 2 | 2(2) | 1/2 | 0/0 | 2026-06-12 | fix(kanban): make --initial-status blocked sticky against recompute_ready (#35) |
| #794 | hermes_cli | code | 52 | 2 | 0(0) | 2/3 | 0/0 | 2026-09-21 | fix(kanban): survivor baseline skips unreadable dirs instead of killing the dispatch tick (#794) |
| nopr:aa0f725eb8 | hermes_cli | code | 48 | 2 | 0(0) | 0/2 | 0/0 | 2026-06-14 | fix(browser): suppress macOS "Keychain Not Found" modal on debug-Chrome auto-launch |
| #326 | hermes_cli | code | 44 | 2 | 1(2) | 0/1 | 0/0 | 2026-07-14 | fix(auth): clear stale xai-oauth last_auth_error on successful token save (#326) |
| #115 | hermes_cli | code | 40 | 2 | 2(2) | 0/5 | 0/0 | 2026-06-29 | fix(backup): exclude swiftui-docs (Apple IP) from backup zip (#115) |
| #491 | hermes_cli | code | 39 | 2 | 2(2) | 0/1 | 0/0 | 2026-08-07 | fix(kanban): respawn guard fail-closed forever on a JSON-quoted PR URL (#491) |
| #593 | hermes_cli | code | 30 | 1 | 1(1) | 0/3 | 0/0 | 2026-08-16 | fix(cli): hide numeric failover lanes in `hermes model`, the third picker surface (#593) |
| nopr:36134d8944 | hermes_cli | code | 29 | 2 | 1(2) | 1/2 | 0/0 | 2026-06-11 | fix(model): route bare gpt-5.5 to Codex OAuth |
| nopr:9c6214b9c5 | hermes_cli | code | 27 | 2 | 2(2) | 0/2 | 0/0 | 2026-05-13 | fix: parse openai-codex slash model switches |
| nopr:ca61765850 | hermes_cli | code | 27 | 1 | 1(2) | 1/10 | 0/0 | 2026-06-21 | fix(backup): bound _safe_copy_db so a live 1GB+ DB can't deadlock the full backup |
| #33 | hermes_cli | code | 25 | 2 | 0(0) | 0/3 | 0/0 | 2026-06-11 | fix(browser): add --remote-allow-origins to CDP debug launch for Chrome 111+ (#33) |
| #941 | hermes_cli | code | 24 | 2 | 0(0) | 0/2 | 0/0 | 2026-09-23 | fix(kanban): include git operation and exit code in survivor holds (#941) |
| #638 | hermes_cli | code | 18 | 3 | 1(1) | 0/1 | 0/0 | 2026-08-24 | fix(ci): make 3 tests container-hermetic + stop doctor skipping backend sections in containers (#638 |
| #631 | other:pyproject.toml | code | 5 | 2 | 1(2) | 0/1 | 0/0 | 2026-08-21 | test: fail on empty parameter batteries (#631) |
