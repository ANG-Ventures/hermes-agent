# Parent routing build checkpoint

Task: t_99abe991. Historical receipts below are checkpoint evidence, not approval for landing or activation. The correction-round section supersedes earlier acceptance claims.

## Executed evidence

- Base route matrix: 3 failed / 5 passed; inherited summary/fork/branch incorrectly selected worker-model. Explicit child and ordinary controls passed.
- Cold-parent base probe attempted global credential resolution instead of its supplied session resolver and failed with no configured global provider.
- Routing/fork matrix checkpoint: 55 passed in 43.88s.
- Existing persisted-session/channel suites: 15 passed in 10.66s.
- Existing ordinary fallback matrix: 29 passed in 9.13s.
- Latest HTTP checkpoint: 10 passed in 31.59s. Synthetic primary and same-provider rung returned quota 429; cross-vendor SSE returned the unique expected nonce. Includes actual run_entry wrapper with executor/delivery mocked, canonical SessionStore cold resolution, active fallback, empty-policy primary-only failure, in-process detached JSON reconstruction and two boundary mutants.
- Actual credential-sync writer plus route matrix/eligible-fork subset: 20 passed in 11.23s.
- Final targeted mixed/explicit per-task replay, actual credential-sync and bounded snapshot wait: 5 passed, 18 deselected in 3.86s. Import-at-collection corrected a local bootstrap/home-I/O guard fixture error before this receipt.

Runnable narrow checks (sanctioned test-gate; local fallback only when remote admission is unavailable):

    ~/.hermes/scripts/test-gate python3 -m pytest -q tests/tools/test_boomerang_parent_routing.py tests/gateway/test_boomerang_parent_routing.py
    ~/.hermes/scripts/test-gate python3 -m pytest -q -s tests/integration/test_boomerang_parent_routing_http.py

HTTP cases enforce 30-second alarms; bound the execution process to 180 seconds externally, excluding test-gate placement queue time.

## Independent review disposition

1. Explicit recovery credentials bypass: accepted, bypass now checks persisted route_source; selected per-task route also recorded.
2. Mixed batch child-0 route contamination: accepted, selected route and effective fallback recorded on each child/task, including ordinary and explicit children. Both-order and explicit replay checks passed in the final targeted receipt.
3. Credential refresh snapshot race: accepted, refresh/adoption writers synchronized per agent; actual sync-writer regression passes. Snapshot acquisition bounded to two seconds. Token lookup/client operations still occur under the per-agent lock; no gateway-wide lock is used.
4. Eligible-fork nested override aliasing: accepted, deep copy from captured route snapshot; eligible fork/summary test added.
5. Postconstruction downgrade re-snapshot: accepted, captured route snapshot threaded through recursive rebuild. Dedicated parent-switch downgrade/pin-refused proof remains outstanding.

## Continuation checkpoint (same draft PR #116)

- Endpoint-loss reproduced directly on cc36bb1: `persist_route` omitted selected URL. Selected safe primary endpoint now persists in both actual child metadata and helper records; recovery prefers it over provider default and preserves selected protocol. Credential-bearing URLs fail closed. Eleven persistence cases passed through remote sanctioned gate (ci), 3.17s; direct pre-fix assertion was `selected endpoint lost before recovery`.
- Hermetic HTTP fixture now suppresses unrelated plugin/relay boot and context probes, keeps SQLite registry checks for private new DBs, and shortens the actual retry sleep at both definition and imported call-site seams. Cold first summary alone passed 1 in 8.97s. No deadline increased.
- Real notifier dispatch -> persisted claim -> busy/cold canonical parent -> actual delegate/conversation/fallback -> nonce verdict -> real executor/receipt -> adapter delivery: 3 passed in 62.95s before DB-fixture cleanup. Combined HTTP + auto-dispatch rerun: 13 passed in 73.19s. ALR-shaped primary and same-vendor `/anthropic/v1/messages` each fail; CPA-shaped OpenAI chat/SSE succeeds on `/v1/chat/completions`, with distinct synthetic credentials. This is protocol integration, not vendor availability.
- Actual two subprocess/profile homes use production `_child_main`, profile `.env` credential resolution, persisted task reconstruction and real local HTTP: 1 passed in 28.20s. Each returned its own nonce on its own selected URL/model/key despite changed worker/provider defaults. Plugins/MCP startup and metadata probes are explicitly suppressed by child harness; actual platform launch/supervisor admission is not claimed.
- Historical snapshot-timeout arm held the real lock and sent zero requests, but incorrectly accepted `resurfaced=true`, `resurface_delivered=false` without a pending handback. That was wake loss, not acceptance; corrected below.
- Newly discovered: documented pin-refused summary reconstruction did not exist in this tree (no `pin_refused` code). Implementing narrow explicit relay-503/header refusal propagation and one summary rebuild using captured route; fingerprint/pin-refused constructor tests pending.
- Existing Linux wake hang reproduced: default SessionDB open traverses host `/proc/*/fd` under home-I/O guard. Passing `HERMES_STATE_DELETED_WAL_CHECK=registry` in outer shell is scrubbed by test isolation; set it in the scratch-DB fixture. Prior multi-file run stopped bounded after no result, not called green. Base comparison remains pending.

Raw receipts are `/Users/alexgierczyk/.hermes/cache/scratch/boomerang-{persistence-green,http-cold-first,http-final,auto-wire-r2,detached-profiles}.log`.

## Final continuation receipt

All commands use `TEST_GATE_NO_CACHE=1 ~/.hermes/scripts/test-gate /opt/homebrew/bin/python3 -m pytest -o addopts= -q` followed by the named repo-relative files (HTTP also `-s`). Remote venue was ci unless noted. Each tranche is at most three files; no suite guard bypass, production calls, board writes, config changes, merge, deploy or restart.

| Targets | Result | Raw scratch receipt (`boomerang-` prefix) |
|---|---|---|
| `tests/integration/test_boomerang_auto_routing.py`, `test_boomerang_detached_profiles.py`, `test_boomerang_parent_routing_http.py` | 15 passed, 78.22s | `integration-final-r2.log` |
| `tests/tools/test_boomerang_fingerprint_routing.py`, `test_boomerang_route_persistence.py`, `test_boomerang_parent_routing.py` | 45 passed, 26.21s | `final-contract-r2.log` |
| `tests/gateway/test_boomerang_parent_routing.py` with earlier fingerprint/persistence subset | 22 passed, 34.46s | `final-contract.log` |
| `tests/tools/test_delegate_tool_fork_mode.py`, `test_delegate_tool_fork_entrypoint.py`, `test_delegate_tool_fork_pin.py` | 49 passed, 77.41s | `fork-regression.log` |
| `tests/tools/test_delegate_fallback_matrix.py`, `test_delegate_request_overrides.py`, `test_boomerang_inherit_context.py` | 61 passed, 17.54s | `ordinary-regression-r2.log` |
| `tests/tools/test_delegate.py`, `test_delegate_detached.py` | 187 passed + 3 subtests, 138.16s | `delegate-regression-r3.log` |
| `tests/tools/test_delegate_detached_safety.py`, `test_delegate_restart_recovery.py` | 98 passed, 1 skipped, 194.89s | `detached-regression-r2.log` |
| `tests/gateway/test_kanban_boomerang_wake_dispatch.py` | 62 passed, 252.14s | `wake-regression-r2.log` |
| `tests/gateway/test_session_model_override_persistence.py`, `test_session_model_override_routing.py`, `test_session_route_concurrency.py` | 16 passed, 64.00s | `session-regression.log` |

The persistence red control materialized cc36bb1's routing helper in a separate base archive: 12 failed in 2.44s, including named endpoint-loss assertion, malformed policy before lookup and credential URL refusal. This is not the queued file initially named `persistence-red.log`, which actually ran after the fix; only the direct pre-fix assertion and `persistence-control-red.log` are red evidence.

Regression failures were adjudicated, not waived:
- Existing detached launch tests failed with the same `None` handle / missing env assertions on immutable base 59e169b (2 failed, 1 passed in 17.88s, `base-comparison.log`). Host systemd scope admission returns degraded. Their tests now explicitly inject direct-launch admission while retaining actual Popen, PID-file failure and env assertions. Whole files pass as above.
- Wake setup stalls were captured with a 15s faulthandler: `/proc` scan, then SQLite `_init_schema.executescript` under remote load. Base exact owner-wake node passes remotely and locally. Scratch fixture now retains registry WAL checks without foreign-host fd enumeration. Whole head file passes, no deadline increase.
- Expanded route metadata exposed MagicMock-generated non-configuration objects (deepcopy reached a thread lock). Existing mock parent factories now declare concrete route configuration, including nested orchestration. No production copying guard was weakened; complete ordinary/delegate files pass.
- Both boundary mutants now accept only named routing-contract assertion failures. Fingerprint mismatch and explicit relay `503 + x-pool-pin-refused` rebuild tests switch the live parent mid-construction/request, then require the original model/provider/URL/key/chain and summary context. Refusal controls reject 429, missing header, summary mode and an unpinned request. Pin-refusal transport is injected at the real request method; this is not a relay vendor test.

Sanitized per-case traces are committed at `evidence/continuation-wire-traces.json`. Actual synthetic auth equality is asserted by the tests; credential values are omitted from that published artifact.

## Remaining limitations — retain draft pending parent certification

- Production activation/smoke is intentionally pending; no restart or real provider availability is claimed.
- Independent final-head review/certification belongs to Apollo. Full-tree CI was not run locally; these are named narrow subsystem receipts.
- Detached subprocess proof uses two actual OS processes and the real `_child_main`/record reconstruction/credential resolver, but suppresses plugin/MCP startup and does not certify OS supervisor spawn admission or restart/relaunch exactly-once integration. Two profiles use distinct endpoints/keys/models with explicit disabled chains; cross-vendor traversal is proven separately through automatic wakes.
- Cold session/channel precedence and corruption refusal are separately tested; not every precedence/corrupt-on-disk case has its own full HTTP dispatch row. Actual credential-sync writer concurrency is proven, but decorator-removal mutation coverage for every route writer is not exhaustive.
- Operator commit f4738af4fda2 adds cold `session-rehydrated` provenance. Its independent integration receipt was 15 passed in 31.67s. The prior approval was withdrawn at 8450a69a; it does not apply to the correction head. Production activation remains pending.
- HTTP alarms remain 30s per case; combined integration receipt is below 180s, but test-gate placement time is outside that bound and no independent 180s process-group supervisor is shipped.

No production configuration, runtime tree, tracking board or gateway process was modified.

## Operator-requested correction round (reviewed base 8450a69a)

The six-key scope is corrective, not a new architecture or a production acceptance waiver.

- `1f01758726e8`: endpoint/key mismatch now fails closed before copying or constructing a recovered runtime. Saved endpoints may only use credentials resolved for that endpoint. Named `custom:<name>` references use the existing resolver. The previous provider-default-key plus saved-foreign-URL test was unsafe and replaced. Red assertion: DID NOT RAISE; matching credentials remain accepted and mismatched lookup issues no HTTP request or credential-bearing error.
- `55306c3b0721`: removed automatic pin-refusal summary replacement. A refused first request returns the existing `error` result and normal subtree/registry cleanup; no second child exists outside the dispatcher's cancellation ownership. This deliberately withdraws the earlier automatic-rebuild capability. Constructor-time fingerprint downgrade remains supported.
- `664f668ffaf9`: `_fail` atomically persists the original turn and warning via the existing pending-handback mechanism before terminalizing. Actual snapshot-lock timeout now proves first refusal, pending turn, reconciliation admission exactly once, and cleared obligation. No second delivery subsystem added.
- `b289dfdf1cf2`: owner lookup and profile context entry are inside the dispatch failure boundary. Both injected exceptions now terminalize with the original wake durably pending, rather than escaping the task.
- `beea9b15e31c`: HTTP alarm is a context manager entered before endpoint/session/agent setup; all setup exceptions restore handler/timer. Parent starts as None. The cold-first SDK import occurs at collection to avoid metadata enumeration against guarded home during a test.
- `90de20f8c829`: virtual endpoints require their owning provider: MoA's local facade or a registered external-process ACP profile. Ordinary background ACP/MoA routes traverse durable-spec construction and completion/interrupt tests. HTTP providers cannot impersonate these virtual transports; credential-bearing routes remain refused.

Observed receipts (same repository interpreter; sanctioned `test-gate`, cache disabled):

- Red route/fingerprint/auto tranche: 5 failed, 24 passed in 28.24s. Owner/scope red: 2 failed. These are assertion failures, not setup failures.
- Final HTTP + automatic dispatch + owner/scope tranche: 20 passed in 50.69s.
- Final persistence + fingerprint tranche: 28 passed in 9.54s.
- Entire background-interrupt file plus parent routing matrix: 31 passed in 17.57s.
- Cold summary and setup-exception alone: 2 passed, 9 deselected in 11.38s.
- Immutable old HTTP harness setup-failure control: 1 failed in 3.07s with `setup exception leaked SIGALRM handler`; cleanup in the control restores the leaked alarm even on assertion failure. The first control attempt failed earlier on SDK metadata/home isolation and is not the alarm reproduction.
- Two real detached profile subprocesses with endpoint-bound named custom references: 1 passed in 11.35s. A generic OpenAI default key is intentionally insufficient for the saved foreign endpoint; the old detached fixture failed closed after the security fix and was corrected to use a real symbolic reference.
- Entire wake-dispatch/default-handback files: 86 passed in 88.00s after their fake runners supplied the required owner/scope seam. Initial 7 failures included one late-bootstrap home-I/O violation and six incomplete runner doubles; no production guard was relaxed.
- Fresh automatic busy/snapshot wire run: 2 passed, 2 deselected in 9.19s. Sanitized request/nonce receipt: `evidence/correction-wire-traces.json`; synthetic key equality remains asserted in executable tests. Snapshot's printed claim precedes reconciliation; the test additionally requires pending turn, one later admission and cleared row.

Background CI attribution: job 128679699643 at old head contains three actual 60-second harness failures and three `a detached unit must not run in-process` assertions. The immutable 8450a69a control reproduces that six-node set. The fake child lacked concrete route metadata and MagicMock fabricated values that durable-spec serialization rejected; the fixture now declares the actual route contract. This does not weaken production persistence checks. Other CI dependency-download failures are not counted as passes, and no blind rerun was launched.

`scripts/run_tests.sh` was attempted through the gate and refused by the worker suite policy. The executed commands use `test-gate <repo>/venv/bin/python -m pytest -q -o addopts= <one to three named files>`. Full-tree CI, merge, deployment, restart and real-provider production smoke remain operator-owned and unverified. Production deviations: none.

## Terminal scoped correction (base 59f081a6e0bc)

- `c-7be8c40d5e15aa52`: reproduced through `delegate_task` recovery for ordinary and explicit children. A foreign provider-default endpoint refused valid direct credentials; a matching default endpoint selected the wrong provider key. Each persisted task now names its credential source independently. Direct routes resolve through the existing delegation credential resolver only when the current delegation endpoint matches the saved endpoint and supplies a key. Missing keys or changed endpoints fail closed; no arbitrary provider/parent-key fallback was added. Saved model, resolved fallback chain, request overrides and routing filters are replayed per task. Legacy records may use the execution-level credential reference for non-parent tasks; mixed new records carry per-task references.
- `c-f7ab7139beabb5e9`: `persist_route` now takes the constructed child's resolved `_fallback_chain`, not its constructor-input metadata. The injected symbolic-input/constructed-list discrepancy reproduced a persistence failure on base. Literal `auto` reaching a real constructor was NOT reproduced: the current child resolver already resolves delegation `auto` before construction. The change enforces the durable effective-chain invariant; tests include real `auto` policy entry cases and an injected normalization discrepancy.
- `c-494d6b4cbc8bb84e`: not a production defect: `_call_args = dict(locals())` includes both signature flags. Existing downgrade proof strengthened with an actual concurrent locked parent-route writer. Recursive summary downgrade retains the original snapshot/model/endpoint/fallbacks/overrides.
- `c-b7b90d9004782bf9`: each successful subprocess launch immediately registers an ExitStack teardown callback, covering exceptions during subsequent launches as well as normal result collection. Injected second-launch failure proves the first real subprocess is reaped.

Receipts use the repository interpreter through the sanctioned gate with `TEST_GATE_NO_CACHE=1`. Remote placement fell back with `editable_outside_repo`; these are admitted local results, not remote receipts. `scripts/run_tests.sh` was attempted and refused by the suite guard.

- Base focused regression: `tests/tools/test_boomerang_parent_routing.py -k 'direct_recovery or constructed_fallback'`: **5 failed, 23 deselected in 6.22s** (named assertions, not setup errors).
- Parent routing, persistence and fingerprint tranche: **56 passed in 16.93s**.
- Updated routing, concurrent fingerprint and real detached-process tranche: **40 passed in 35.33s**.
- Entire restart-recovery, fallback-matrix and background-turn-interrupt files: **88 passed in 21.86s**.
- Detached provider/direct/mixed-both-orders plus entire HTTP fallback file: **16 passed in 59.16s**. Two distinct temporary homes and real production `_child_main` processes; keys differ between named-provider and direct references even at the same URL. Every HTTP observation matches the selected model and endpoint-bound key, each profile returns its own sentinel, and no key appears in persisted specs/results/child logs.
- Explicit direct recovery alone: **1 passed, 5 deselected in 7.51s**, real process IDs 88480/88482 and sentinels DETACHED-a/DETACHED-b.
- Final persistence/restart/expanded-auto contract tranche: **108 passed in 11.13s**. The first expanded-auto test attempt incorrectly expected unknown-vendor models to be remodeled and failed five assertions; the canonical resolver intentionally retains unknown/cross-vendor rungs. Corrected the test expectation, not production behavior.
- Operator-requested mixed source/fallback proof: entire parent-routing file **40 passed in 7.86s**. Both task orders retain distinct selected-child/inherited-child models, selected-backup/inherited-backup chains and direct-config/provider credential sources, including identical URLs. Changed config endpoint and removed config key both refuse before child construction with an explicit zero-network assertion.

Raw logs are attached to the task as scoped correction evidence. The HTTP auto-wake receipt traversed parent-pin → second-model → cross-model and returned `BOOMERANG-WIRE-3d7d1ea075b04e75bae177f387c6bca0`; this is isolated synthetic protocol proof, not production availability. Prior immutable-base CI classifications remain intact. No broad Prism request, merge, deployment, live configuration mutation or restart was performed; current-head CI and production acceptance remain open for Apollo.
