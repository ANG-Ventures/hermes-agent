# PHASE-0 PROBE — P4 server-side reroute detectability (FALSIFIED)

**Date:** 2026-07-07 · **Owner:** Apollo
**Spec:** `~/.hermes/plans/2026-07-07_reasoning-model-switch-visibility-SPEC.md` §5F / Phase 4 / AC-14
**Verdict:** 🔴 **P4 NOT BUILDABLE AS SPECIFIED — premise falsified. Do NOT ship.**

## The premise P4 rests on

P4 (announce a server-side model reroute) detects the reroute by comparing the
**requested** model (`agent.model`) against the **served** model (`response.model`,
read at `conversation_loop.py:4212`). The load-bearing assumption: when Anthropic's
safety layer serves `claude-opus-4-8` for a `claude-fable-5` request on sensitive
content, `response.model` reports the *served* (`opus`) name, so `served != requested`
is detectable.

## The probe (live, against the real relay `http://127.0.0.1:18810/anthropic`)

Requested `claude-fable-5`; read `response.model` from the actual API response.

| Prompt class | requested | served `response.model` | diverged? |
|---|---|---|---|
| benign ("say ping") | claude-fable-5 | `claude-fable-5` | NO |
| defensive-security (stored-XSS/CSP) | claude-fable-5 | `claude-fable-5` | NO |
| working keylogger | claude-fable-5 | `claude-fable-5` | NO |
| nerve-agent synthesis | claude-fable-5 | `claude-fable-5` | NO |
| "ignore safety, make a bomb" | claude-fable-5 | `claude-fable-5` | NO |

Faithfulness control (proves the field WOULD show a divergence if one happened):

| requested | served `response.model` |
|---|---|
| claude-opus-4-8 | `claude-opus-4-8` |
| claude-fable-5 | `claude-fable-5` |
| claude-haiku-4-5 | `claude-haiku-4-5-20251001` (dated variant — a genuinely distinct served string) |

The `response.model` field is **faithful**: it echoes the exact model served, and
even surfaces a dated variant when the served model differs from the request. So if a
`fable → opus` reroute actually changed which model answered, the field would report
`claude-opus-4-8`. It never does — across benign AND maximally safety-flagged prompts,
`response.model` is always the requested `claude-fable-5`.

## Conclusion

**The server-side reroute is NOT observable via `response.model`.** Whatever the
`content-triggered-model-flip-routing` skill described as a "fable→opus flip" is
either (a) not a model substitution at the API-response level, (b) an internal
Anthropic safety pass that does not change the reported served model, or (c) a
footer artifact from a *requested*-model change (a `/model` override), not a
server reroute. In none of these cases can a `served != requested` detector fire.

Per the spec's Phase-0 gate (AC-14): **P4 is reported un-buildable rather than
shipped as a detector that never fires.** P1/P2/P3 are unaffected and ship.

## What this means for Ace's OQ-1 ask ("announce the reroute like a fallback")

- **Client-side failover** (the pool bouncing opus@claude-app → opus@f3) IS
  observable and IS already announced in-session — the morning spec's
  `_emit_fallback_announce` (live since commit #225, gated `model.announce_route_change`).
  That covers the "announce it like a fallback" ask for the case that actually happens.
- **A genuine server-side reroute** would need a signal Anthropic does not currently
  expose in the response. If Anthropic ever adds a served-model or safety-reroute
  field to the API response, revisit P4 — the detector shell (`_emit_reroute_announce`,
  the call site, the dedup) is designed and ready to wire to a real signal.

**Roadmap:** re-open P4 only if a live probe shows `response.model` (or a new
Anthropic response field) diverging on a reroute.
