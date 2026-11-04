# Relay fallback banner attribution

Fallback banner wording is derived from the failing response, not a later relay health read. The route-change notice and the compact “replying on … (fallback: …)” notice share the recorded head label.

For pool-pressure refusals, the response's `x-pool-seats-total`, `x-pool-seats-eligible`, `x-pool-seats-busy`, and `x-pool-ineligible-reasons` headers distinguish:

- Positive eligible count with every eligible seat measured busy: relay busy.
- Zero eligible seats with every pool seat excluded by quota: relay quota exhausted.
- Zero eligible seats with every pool seat excluded by transport: relay transport unavailable.
- Missing, partial, mixed, or otherwise inconclusive eligibility evidence: relay unavailable. Eligibility alone is not evidence of occupied slots.

An explicit “pool at capacity” refusal without counts remains capacity evidence. Deployment drain, connection failures, named quota windows, and the existing Fable allowance wording retain their more specific attribution. A bare “no eligible sub” without a quota class or named quota evidence is displayed neutrally, even when the historical text classifier assigned it a quota class.

These are presentation rules only. The error classifier, retry policy, cooldowns, drain behavior, and route selection are unchanged.
