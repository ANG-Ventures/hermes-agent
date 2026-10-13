# Stop holds automatic work

## Goal and measured failure
On 2026-10-13, five explicit stops in Apollo's Discord home channel were followed by internal notification turns 9–18 milliseconds later. This blocked model switching. The stop tail deliberately preserved an internal wake in the runnable adapter slot. The adapter immediately drained that slot. The existing test even asserted this restart.

`display.stop_releases_queue` is false in the affected deployment. That flag controls human queue release, not the internal wake-preservation path; it cannot solve the failure.

## Contract and design
Explicit stop latches automatic execution off before awaiting cleanup. Pending notifications join the existing held queue, not a new queue service. Internal arrivals pass a shared queue-management gate before the busy dispatcher and again at turn session resolution (covering recursive follow-ups). The process-local tri-state hydrates from the existing durable user-stopped marker after restart.

Read-only slash commands and model selection do not resume. A human turn clears the durable stop and releases only held system notifications behind the human turn; human /queue items remain held. Explicit /queue run releases both classes without merging system messages into human prompts. A newer stop generation wins over an awaiting resume. User resets keep existing boundary policy. Other sessions are unaffected.

The previous stop_releases_queue opt-in does not override an explicit automatic-work pause. No new config, dependency, database or slash command.

## Invariants
- Preserve accepted notifications; park rather than discard them.
- Stop must not put an internal wake back into the run-next slot.
- A model command must not start an LLM turn or release held work.
- Human queue and system notifications stay distinguishable; merged release only merges human items.
- Existing generation fencing, ownership and busy guards remain in force.
- Graceful restart uses the existing held-queue spool. This does not introduce crash-durable storage for queues that were previously in memory.

## Verification
Run through the fleet test gate, at most three explicit modules per invocation:
- tests/gateway/test_stop_holds_internal_wakes.py: original real stop/adapter-drain RED on base, late arrivals, real inbound interception, controls, explicit release, restart-marker hydration, cross-session isolation and failed resume writes.
- tests/gateway/test_gateway_queue_mgmt.py and test_queue_release_real_adapter.py: existing queue contracts.
- tests/gateway/test_interrupt_keeps_parked_internal_wake.py, test_user_stopped_never_resumes.py, test_queue_stale_drain_real_turn.py: stop/reset preservation and stale-generation behavior.
- tests/gateway/test_stop_clears_pending_clarify.py, test_stop_ends_background_delegations.py, test_stop_zombie_turn_lease.py: cancellation siblings.

Acceptance still requires real model-switch-path verification and production reload proof, not merely these unit tests.

## Ops / rollback
Land only on reviewed exact-head regression/security evidence. Deploy via fleet deployment tooling and reload through the safe gateway restart front door. Verify stop followed by model selection under an internal notification backlog. Rollback is revert of the focused PR plus the same deployment/reload path. No production-state mutation is needed.

## Non-goals
Global pause, notification producer redesign, arbitrary queue crash durability, force-changing a live agent's model, or gateway restart in the middle of an operator turn.
