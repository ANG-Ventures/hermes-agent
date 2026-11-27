# Configuration and rollout handoff

Example only; parent must confirm each binding is still served before applying.

```yaml
auxiliary:
  goal_judge:
    provider: claude-alr
    model: claude-opus-5-5
    timeout: 30
    total_timeout: 120
    max_tokens: 4096
    fallback_chain:
      - {provider: cpa, model: gpt-6.1-sol, timeout: 30, reasoning_effort: medium}
      - {provider: xai-oauth, model: grok-4.7, timeout: 30}
      - {provider: cpa, model: kimi-k3.1-pro, timeout: 30}
```

A nonempty chain opts verdict judging into bounded explicit routing. No implicit
main-model fallback or same-provider transport retry precedes the next route.
Each route's deadline includes Python startup, plugin discovery and credential
resolution. total_timeout bounds the chain; timeout is also the upper bound for
any individual rung, including the foreground cleanup wait. Timeout/cancel starts
owned background termination and bounded reaping; cancellation propagates without
waiting for that worker. If termination is stuck or refused, `cleanup_pending`
fails closed and inhibits all new judge requests for that home in this process
until the retained process handle is reaped. This is not a claim of immediate
termination when the OS refuses it. Other homes remain independent. Startup,
pipe, malformed IPC and nonzero worker exits are sanitized `worker_error`
availability failures and may advance only after ownership is released.
Each failed chain is one GoalManager failure,
not four failures. Parse/transport exhaustion never marks a goal done. The existing
failure thresholds still pause repeatedly unjudgeable goals, preserving the goal.

Providers/models must be explicit, with finite positive timeouts and at most seven
fallback routes. Unknown/unavailable routes are reported by category. Do not embed
credentials in a rollout document; use the existing provider credential setup or
route key_env. Endpoint/key/extra_body are not inherited across vendor routes.
Empty/absent fallback_chain retains the ordinary auxiliary path. Contract drafting
retains ordinary auxiliary recovery rather than validating a draft as a verdict.

Rollback configuration: remove fallback_chain (or set []); remove total_timeout if
not desired. Parent must back up live config, validate, apply and deploy through the
normal lane. No gateway restart or live configuration change is performed here.

Parent-owned model-rollout skill patch (replace its goal-judge seat section):

    Goal judge is an ordered explicit chain: latest Opus, latest GPT Sol,
    latest Grok, latest Kimi. Advance primary and every fallback_chain entry
    in root and every profile's auxiliary.goal_judge block on the respective
    family rollout. Keep unrelated primaries/delegation/certified judges intact.
    Discover concrete provider/model bindings from current registry/lane catalogs;
    a declared model is not served evidence. Check every provider's reachability.
    Before cutover, run a fresh-client forced-primary-failure judge through the
    real judge/GoalManager seam. Require a schema-valid verdict, actual served
    model, ordered attempted-route receipt, and no leftover process after timeout
    or cancellation (or explicit cleanup_pending ownership and retry inhibition
    when termination fails). Test each rung and all-unavailable; a valid blocked/wait/
    continue verdict ends the chain and must not be shopped for a done answer.
    Keep total_timeout and per-route timeout finite. Report merged source and
    live loaded behavior separately; deployment/restart and config apply are
    operator-owned gates.

Fleet scope expansion: apply the same shared runtime to Apollo/default, Aegis,
Agora (ACE-AI default home), and every active profile. Profiles are independent
islands: they inherit built-in defaults, NOT the root profile's live chain.
studio-manifest.json and ace-ai-manifest.json enumerate effective overrides and
per-profile proposed migrations from each home's own aliases. Preserve declared
exceptions explicitly; do not assume shared plugin code means shared credentials.
The operator must migrate each listed goal_judge block or record its exception.
An absent override (ACE-AI Clanker currently auto) is not an inherited fleet chain.
Rollout guidance must inventory both fleet policy intent and per-profile effective
configuration, and re-prove forced-primary-failure under each host/identity.
