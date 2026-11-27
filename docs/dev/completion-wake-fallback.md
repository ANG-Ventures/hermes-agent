# Completion wake admission (t_4f28d7a7)

## Cause and scope

`_deliver_control_wake` calls `_wake_downgrade_reason` before `deliver_wake`.
`WakeGate._default_lane_probe` previously created a profile-default task and
called only `capped_provider`. A positive primary cap returned `lane … capped`
without checking the configured fallback chain, preventing the agent's existing
failover machinery from ever running. The notifier uses the same WakeGate.

The Athena follow-up log records successful report delivery followed by repeated
`lane claude-alr capped` responses for the entire 1800-second defer ceiling.
The same log records that the advisor itself reached the configured terminal
fallback earlier. Notification delivery was not turn execution.

## Repair and boundaries

When the primary is capped, reuse `available_profile_fallback` with the same
health probes, cache and eligibility threshold. Admit only if that selector
finds a configured rung with headroom; otherwise preserve the downgrade and
state that no configured fallback has headroom. The shared resolver expands
configured/derived chains in the owning profile's home and secret scope, then
restores both caller ContextVars even if source expansion raises. This matters
for control-socket service threads that have no turn scope or carry another
profile's scope; changing the home alone does not authorize derived sources.
The launch home reuses `launch_secret_scope` so frozen env-only credentials
survive multi-profile activation; secondary homes receive only their own files
and external sources, never the launch environment.

This is admission, NOT forced route installation. Do not stamp the selected
health-check rung as a session override: the regular agent resolver still owns
credentials, endpoint hints, API mode, model context changes, quota policy,
explicit pins, budgets and runtime failover. Health is advisory and can change
between admission and execution; acceptance never guarantees synthesis success.
No new credential probing, resource reservation, fallback policy, or dedup
ledger is introduced. Unknown health retains the existing fail-open semantics.
Session-specific pins are not changed or converted into configured fallback
permissions by this change.

Sibling callers covered by the shared gate: control-socket wake and kanban
notifier downgrade checks. Async delegation/completion admission uses its own
receipt path and is not changed. The dispatch helper's transport/key-ledger
state machine is unchanged. The companion hermes-home change removes the
premature `Apollo picking it up` claim from the pre-wake result notification.

## Verification

The regression drives the real `_deliver_control_wake`, actual `WakeGate`,
profile config + shared health/fallback selector and `deliver_wake`; its adapter
runs a real `AIAgent.run_conversation` with fake provider I/O. Assertions require
one admitted event, primary quota error, then exactly one configured fallback
API attempt and a completed synthesis. Before the repair it returns
`delivered=False, downgraded='lane zai capped'` and no turn executes.

Other cases cover all-capped/empty chains, repeated deferrals with no execution,
and true CPU/process saturation even with a healthy fallback. Existing tests
cover identity/profile authorization, real socket transport and context pins.
The hermes-home dispatch suite covers transport retries/key dedup and truthful
notification wording; no live provider/Discord traffic is used.

## Deployment / rollback (operator only)

1. Merge reviewed fleet PRs and rerun the relevant suites on the merged tree.
2. Land home scripts with the fleet home deployment gate. Already-running
   dispatch shells retain loaded functions and must not be represented as fixed.
3. Deploy the reviewed agent revision with `~/.hermes/fleet/deploy.sh` using the
   established instance-specific rollout procedure. Gateway code is imported
   from the deploy-only runtime tree; source edits alone do not activate it.
4. Only with instance-specific approval, perform the normal guarded gateway
   reload/restart boundary. No restart/deploy was performed by this patch.
5. Stage a fresh authorized completion in the origin thread. Record the
   dispatch id, report message id, admission log naming the fallback, actual
   served provider/model and resulting parent synthesis turn. A delivered
   notification or socket admission receipt alone is not acceptance.
6. Roll back by reverting these PR commits through the same reviewed home/runtime
   deployment paths, then cross the approved gateway load boundary again.
   There are no config changes, schemas or new durable state to migrate.
