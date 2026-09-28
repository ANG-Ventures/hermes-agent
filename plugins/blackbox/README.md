# Blackbox

Per-turn telemetry for Hermes: cost, tokens, tools and API calls per turn,
written to a per-profile SQLite store, with alert cards and the `/cost`
command. Hooks and config are in `plugin.yaml` and `__init__.py`.

## Blackbox is the attribution layer, not the meter

Blackbox records every call the harness makes, and it is the only record of
*who and why*: session, turn, agent, aux task, delegate depth, `sub_harness`
(MoA) and `meta_harness` (FleetReview). Keep `turn_api_calls` on for every
profile for that reason.

Blackbox does not decide *how many* tokens were spent. The boundary recorders
count: the per-box apx/bpx wirelog, the CLI transcript for cpx, the
CLIProxyAPI usage queue, and the gemini-bridge call log.
`subs_data.ledger.LANE_PRIMARY` names the one primary recorder per lane, and
only primary rows add to a total. A Blackbox row that matches a wire row by
correlation id (`route_id`, `x-hermes-call-id`, request-id) is that row's
attribution. It is never a second count.

A Blackbox total on its own is a **lower bound**. The harness does not see:

- Claude Code traffic sent through `claude-apr-cli`;
- side calls made by a bpx child process.

Do not sum Blackbox and wire numbers, and do not pick the bigger one.
Reconcile them. Unmatched harness rows (`unjoined_harness`) and unmatched wire
rows are named classes in the S7 §7 reconciliation
(`plans/subs-ace/S7-token-ledger-spec.md`, I2/D1). When a subs.ace or
tokens.ace figure disagrees with a Blackbox sum, check which recorder is
primary for that lane before treating it as a Blackbox bug.

References:

- Obsidian: `AI/Infrastructure/Token Tracking — System Overview`, §4a.
- Skill: `blackbox-turn-telemetry`, section "Blackbox is the ATTRIBUTION layer
  of token tracking, not the METER".
