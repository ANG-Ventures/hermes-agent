# Boomerang parent-routing inheritance

Version: 1.0
Author: Apollo
Status: authored; independent review and repository publication pending. Implementation authorized by Ace, not yet started.
Spec card: t_5a8c93ce. Build card: t_99abe991.
Target repository: ANG-Ventures/hermes-agent.
Target canonical path after publication: docs/boomerang-v2/parent-routing-inheritance-spec.md.

## 1. Outcome

A boomerang is a continuation of its parent, not an ordinary worker with the fleet's default worker model. Copy the parent's effective route and fallback policy whether context travels as a full fork or a summary. A pinned parent remains pinned in the same sense as the parent: its selected primary and its permitted fallback behavior are preserved together.

This is a routing fix, not a new fallback system. Reuse the gateway's session resolver, existing provider credential resolution, retry/fallback engine and delegation persistence.

## 2. Measured failure and remaining uncertainty

The November 7 failure for t_dcd349d5 was requested as fork but recorded as summary. Child session 20261107_194504_f124e7 started on claude-alr/claude-opus-5-5 and failed after three attempts with HTTP 503, no eligible sub. Immediately before its construction, agent.log recorded: `delegation fallback_providers is 'auto' but the parent has no fallback chain; the child runs without fallback`. The visible main conversation successfully fell through ALR, DTLR and then CPA/Sol.

Source inspection establishes:

- gateway/kanban_boomerang_wake.py::_db_parent constructs an AIAgent from global runtime kwargs and a default-model resolver. It does not pass the normal gateway fallback constructor argument or explicitly resolve the home session's override.
- resolve_parent selects an idle live parent, otherwise waits and constructs the database-backed summary parent.
- tools/delegate_tool.py child construction forces parent routing for an eligible fork. A failed eligibility check changes mode to summary while retaining the previously resolved general-delegation runtime.
- tools/delegate_tool_config.py::_resolve_child_fallback_chain reads the parent's _fallback_chain. `auto` cannot restore a missing chain.
- gateway/run_turn.py::_resolve_session_agent_runtime already defines session /model, channel override, then global default precedence. Unreadable persisted identities and unresolved credentials fail closed rather than silently selecting a different provider.
- Normal gateway construction explicitly passes fallback_model=self._refresh_fallback_model(), including in gateway/run_turn_runner.py. The synthetic-parent constructor omits this wiring.

The historical claim's home_sid differs from the child log's parent_session. A resumed owner may legitimately explain this. Tests must resolve the current owner through existing owner resolution; this spec does not claim the mismatch proves cross-session leakage.

The missing-chain log proves the incident had no fallback. The exact path that produced that parent is not independently reconstructed from the historical process. Both inspected construction gaps are within scope; neither should be described as an exhaustive explanation of every past boomerang failure.

## 3. Decisions and non-goals

D1. Requested boomerang semantics, not successful cache eligibility, determine routing inheritance. Both explicit summary and automatic summary downgrade inherit.

D2. A deliberate per-call child model/provider/endpoint override remains an ordinary explicit delegation route. It retains current scoped-fallback behavior. A global delegation default is not a deliberate child override.

D3. An eligible live parent supplies its current active route, including an already-active fallback, plus its effective permitted fallback policy. Constructing the child must not first restore the parent's configured primary or mutate the parent. Further child retries use the existing fallback engine.

D4. With no live parent, use the same session-aware route and fallback resolution as a normal gateway turn. Do not invent persistence of a transient active fallback that the gateway itself does not persist. The cold child gets the same recoverability as a cold parent, not a fabricated remembered route.

D5. Preserve an explicitly disabled fallback policy. An empty effective chain may be legitimate; never infer corruption merely from length zero. Construction provenance distinguishes unresolved policy from a deliberately resolved empty policy.

D6. No new user-facing setting, background service, quota monitor, provider registry, or independent route database. No silent paid-provider escape route. No change to ordinary delegation, cron routing, card disposition, board permissions, wake delivery, stale-card handling or cache eligibility.

D7. Ace asked Apollo to author this spec personally. Implementation remains separately assigned after review. A production gateway restart needs fresh approval for that instance.

## 4. Invariants and proof

- Context-mode changes cannot alter an inherited route or fallback list. Prove with identical inputs across eligible fork, rejected fork and explicit summary.
- Route fields travel as one coherent bundle: model, provider identity, endpoint, protocol, request overrides, credential reference and effective fallback policy. Never pair one provider's endpoint with another's credential. Prove with two distinguishable local endpoints and credentials.
- Child mutations cannot modify parent or sibling chains, overrides or runtime state. Deep-copy configuration structures; do not copy or share mutable transport clients. Prove mutation isolation.
- Persist non-secret route identity and existing credential references only. Re-resolve secrets through the correct profile scope at execution. Prove serialized recovery records contain no sentinel credential values.
- Unavailable or malformed persisted session identity follows the existing fail-closed path with no outbound request. It never becomes global-default routing.
- Existing explicit child overrides and ordinary delegation preserve their existing precedence and fallback behavior. Keep their regression suites and add side-by-side controls.
- Successful routing means an actual request reaches the expected provider and produces a final response. Constructor dictionaries alone are insufficient proof.

## 5. Minimal design

### 5.1 One route decision before context construction

At delegation preparation, distinguish explicit call-level routing from defaults. Resolve one of:

1. Explicit child override: existing delegation policy.
2. Boomerang inheritance without explicit override: parent route and effective fallback policy.
3. Ordinary delegation: existing delegation policy.

Thread the selected policy through child construction and recovery using existing internal execution metadata. Use an internal enum/marker only if provenance cannot already be represented without ambiguity; no model-visible switch is required. Per-task inherit modes must be honored before batch-wide credentials accidentally flatten their semantics.

The fork eligibility check may select prompt/context construction and first-request seat affinity. It must not recalculate the child's model route. Post-construction downgrade and pin-refused reconstruction must reuse the same inherited routing decision.

Audit the real producers: delegate_task with inherit_context, fork/branch and summary modes; slash boomerang; boomerang-wake notifier and owner-wake paths; recovery relaunch; detached worker reconstruction. Use the same policy at their shared child-construction boundary, not independent fixes per producer.

### 5.2 Live parent, including busy parent

Capture one coherent route snapshot without changing or running the parent. Reuse an existing runtime snapshot/lock if present. If current mutation sites cannot provide a consistent snapshot, add a narrowly scoped snapshot method synchronized with route activation, rather than reading independent attributes while failover changes them.

A busy parent's transcript may still be folded from the database after the existing wait. That does not justify replacing its routing with a synthetic global-default agent. Snapshot routing separately from transcript retrieval. The child owns its clients and its copies of policy data.

### 5.3 Cold/database parent

Replace the raw global route assembly in _db_parent with the normal session-aware resolver for the resolved owner's session key and source. Preserve channel override context as well as /model overrides. Pass the same effective fallback policy supplied by normal gateway construction.

Reuse canonical resolver logic, but account for its side effects: _resolve_session_agent_runtime currently clears a runner-level pre-agent notice field and reconciles session overrides. Background invocation must not consume another foreground turn's notice. Prefer a small shared resolution helper returning route and notice locally over invoking a stateful foreground wrapper concurrently. Do not hold a gateway-wide lock over network credential resolution.

If a persisted identity is unreadable or credentials cannot be resolved, retain the normal explicit error and owner-wake handling. Never silently retry with global credentials.

### 5.4 Recovery and detached execution

Persist the selected routing provenance and non-secret route/fallback policy through the existing delegation execution record. Restore it without reapplying today's general delegation defaults. Resolve credential references in the original profile scope. Add backward-compatible handling for old records: recover through the canonical owner/session resolver when sufficient identity exists; otherwise report an attributable routing-unavailable failure instead of pretending a zero-chain global parent is correct.

Do not add duplicate boomerang work to fix routing. Existing ownership, retry budget and single-delivery logic stay intact.

### 5.5 Observability

One structured construction log includes delegation ID, resolved owner session, child session when available, requested/effective context mode, route source (live-parent/session-rehydrated/explicit-child/delegation-default), effective provider/model and fallback count. Log an explicit fallback-disabled source when appropriate. Use allowlisted fields; exclude keys, headers, credential URLs and prompts. Preserve existing log markers used by consumers.

## 6. Implementation and verification

### Phase A: red-first contract and canonical route selection

Add tests in tests/tools/test_boomerang_parent_routing.py and tests/gateway/test_boomerang_parent_routing.py. Extend existing boomerang/delegation suites where they already own an assertion. Implement the narrow route decision and session reconstruction.

Unit matrix:

- Eligible fork, rejected fork and explicit summary inherit a parent route deliberately different from global delegation defaults.
- Busy live parent retains its active fallback while its transcript is read from storage.
- Cold parent restores a persisted model pin and channel override precedence.
- Explicit [] remains disabled; missing reconstruction wiring does not erase the configured chain.
- Parent chain includes same-vendor and cross-vendor routes; order and models remain intact.
- Explicit child override and non-boomerang delegation retain current semantics.
- Mutating child chain/overrides does not mutate parent or sibling.
- Corrupt persisted identity and unavailable credentials produce zero provider requests.
- A concurrent route switch cannot yield mixed provider/endpoint/protocol credentials.

Verify with the repository's sanctioned test-gate runner wrapping `python -m pytest -q tests/tools/test_boomerang_parent_routing.py tests/gateway/test_boomerang_parent_routing.py`. Expected: all named cases pass. Before implementation, inheritance and cold-parent tests must fail on the base revision for their intended reason, not import errors.

### Phase B: recovery parity and real fallback execution

Add tests/integration/test_boomerang_parent_routing_http.py using local ephemeral HTTP endpoints and a temporary Hermes home/session store. Drive resolve_parent, run_child and real delegate construction into the actual conversation retry/fallback engine. Mock only external platform delivery and credential lookup as needed; do not replace the routing decision, chain construction or fallback loop with a fake result.

A controlled primary returns 429 or quota-shaped 503, a second permitted lane fails, and the cross-vendor endpoint returns a unique per-case sentinel in the correct wire protocol. Endpoint logs independently record model, route and request ordinal. Assert the child's final result contains that sentinel and requests actually reached the expected fallback. Label endpoints as synthetic providers; this proves harness integration, not vendor availability.

Exercise explicit summary, forced fork downgrade, busy parent, cold parent with pin, and detached serialization/reconstruction. Fork-specific cache tests may inject affinity/eligibility inputs, but must run the real child constructor and keep that test limitation explicit.

Verify with the sanctioned test runner wrapping `python -m pytest -q tests/integration/test_boomerang_parent_routing_http.py`. Each scenario has a 30-second deadline with injected short retry waits; whole run hard timeout 180 seconds. Failure aborts and cleans up child processes, listeners and temporary homes. No production endpoints, cards or messages are touched. Write per-case request traces and verdicts to the build evidence directory.

Negative controls: remove inherited fallback at the construction boundary and restore delegation defaults on summary downgrade, one mutant at a time. Each must fail the relevant route/sentinel assertion. A helper-only test that stays green under either mutant is not acceptance evidence.

Recovery tests also scan serialized artifacts for sentinel secrets, verify explicit-disabled policy survives, and reconstruct two sessions with different provider identities without contamination.

Evals: not applicable. The property is deterministic routing correctness, not model answer quality.

### Phase C: review, publication and activation

Review the final diff against every invariant and run the existing delegation, boomerang-wake and persisted-session routing suites through test-gate. Record exact commands, revision, test counts and integration traces. Update docs/boomerang-v2/README.md with the contract and recovery semantics.

Land through the normal reviewed PR path. Deploy through the standard runtime deploy mechanism, never edit the imported live tree. After an approved gateway restart, use an isolated scratch owner/card and a harmless nonce task to capture resolved parent/child route, fallback policy count and completion. The isolated forced-error HTTP suite remains the proof of failure traversal; production smoke proves the deployed entry point uses the new policy. Delete/archive only the smoke's own artifacts.

A blocked production restart leaves status explicitly merged/deployed, activation pending. It does not turn into a claim of live acceptance.

## 7. Acceptance checklist

- [ ] Context-mode matrix preserves parent routing independently of global delegation defaults.
- [ ] Cold session reconstruction restores /model and channel precedence with the normal effective fallback policy.
- [ ] Busy/active-fallback parent snapshot is coherent and does not reset the parent.
- [ ] Forced provider failures reach the cross-vendor endpoint and return its sentinel through the real child loop.
- [ ] Explicit-disabled fallback and explicit child overrides retain their contracts.
- [ ] Detached/recovery records preserve routing provenance without secrets.
- [ ] Two-session isolation and corrupt-identity negative tests pass.
- [ ] Both regression mutants are detected.
- [ ] Existing relevant suites pass; docs and reviewed PR published.
- [ ] Deployed smoke proves the loaded gateway path, or activation is plainly marked pending.

## 8. Risks, rollback and simplicity gate

The main implementation risks are concurrency while copying a live route, mistaking a general default for an explicit override, and calling the foreground resolver from a background thread with shared notice state. These have named tests above. A missing historical transient fallback after cold boot is not recoverable by guessing; reuse normal cold-turn policy.

Rollback is a scoped revert of the implementation PR and normal deploy/reload procedure, with explicit gateway restart approval. Existing boomerang-wake disable controls remain available to stop automatic handling during an incident. No config migration or data deletion is introduced.

This plan needs one route-selection change, canonical reconstruction wiring, recovery threading, tests and documentation. It does not need a new routing framework, durable service, monitor or configuration knob. A small shared helper is justified only where it removes duplicated routing policy or isolates the foreground resolver's existing side effects.

## 9. Review status and next action

Apollo authored this document after reading the relevant runtime construction and session-resolution seams. Independent review has not yet run. No implementation or live routing change is claimed. Publish this source to the target repository, review it, fold defects, then release t_99abe991 against the resulting immutable spec revision. There are no user-facing design questions outstanding.
