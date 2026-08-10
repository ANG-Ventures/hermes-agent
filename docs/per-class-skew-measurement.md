# Per-content-class token-estimate skew — measurement

This is the evidence that motivated per-content-class skew calibration
(`agent/content_class.py` + the class arm in `agent/context_engine.py`). It is
recorded here so the decision can be re-audited or falsified rather than taken
on trust.

## Question

The token-estimate skew was a single global ratio. Was the estimator's error
actually uniform across content, or does it differ enough by content class that
one blended ratio is wrong for every turn?

A negative result was an acceptable outcome: if the class distributions
overlapped, the correct recommendation was to keep the single global ratio and
not ship speculative machinery.

## Method

Replay, not synthesis. For 30 real production sessions drawn from the local
session store (`~/.hermes/state.db`, sessions with >60 stored messages):

1. Bucket every stored message's content into a class:
   * `text` — `user` + `assistant` natural-language content
   * `tool` — `tool`-role results plus the JSON of assistant `tool_calls`
2. Concatenate each bucket into ~80 KB chunks.
3. For each chunk, ask the **production estimator**
   (`agent.model_metadata.estimate_messages_tokens_rough`) what it thinks the
   chunk costs.
4. Ask the provider for ground truth: a real `POST /v1/messages` with
   `max_tokens=1` against `claude-opus-4-5`, reading `usage.input_tokens`.
5. Subtract a measured empty-request baseline from both sides so the fixed
   request wrapper does not contaminate the ratio.

`ratio = real / rough`. A ratio above 1.0 means the local estimator reads LOW
(under-counts), which makes threshold compaction fire LATE — toward provider
overflow.

15.4 M characters of real transcript were measured across 176 chunks.

## Result

| class | n  | median | mean  | sd     | min    | max    |
|-------|----|--------|-------|--------|--------|--------|
| text  | 89 | 1.0048 | 1.0128| 0.0536 | 0.9335 | 1.1456 |
| tool  | 87 | 1.1629 | 1.1757| 0.0871 | 1.0008 | 1.4499 |
| global| 176| 1.0842 | —     | —      | —      | —      |

Separation statistics:

* Welch t = **14.89** (df ≈ 142)
* Mann-Whitney U z = **−10.48**
* Cohen's d = **2.26** (a very large effect; d ≥ 0.8 is conventionally "large")
* text p90 = 1.0877 vs tool p10 = 1.0842 — the distributions barely touch

Cost of the single global ratio, applied to each class:

* on text-dominated content it over-corrects by **7.9%**
* on tool-dominated content it under-corrects by **6.8%**

That is the "wrong for both" failure the global ratio produces, quantified.

## Interpretation

The split is real and large. Structured tool output (JSON, escaped strings,
long unbroken identifiers, base64-ish fragments, paths) tokenizes denser than
prose, so the character-rate estimator systematically reads low on it and
roughly correct on prose. This is a **rate** difference, which is exactly what a
per-class ratio corrects and what a single blended ratio cannot.

## Explicitly NOT done

The measured body divisor was **not** hardcoded. The adaptive loop is supposed
to converge on the right ratio per class on its own, and it now can: each class
accumulates its own last-k readings and takes its own median. Baking a
measured constant in would freeze one workload's number into a fleet-wide
default and defeat the convergence the loop exists to provide.

## Reproducing

The sweep is a replay over local data plus live provider calls; it is not
checked in as a test because it costs real API calls and depends on a populated
session store. The procedure above is sufficient to re-run it: bucket
`~/.hermes/state.db` messages by role, chunk, compare
`estimate_messages_tokens_rough` against `usage.input_tokens` from a
`max_tokens=1` request, net of an empty-request baseline.

## What the tests assert

`tests/agent/test_per_class_skew_calibration.py` asserts the **behavior
contract**, never these measured constants:

* a tool-heavy turn receives the tool-class ratio, not a blended one
* a class below `compression.skew_class_min_samples` falls back to global
* an under-count is still correctable upward, bounded by `_SKEW_SCALE_UP_MAX`
  (the PR #506 / #529 regression band)
* WIRING: the classifier is actually consulted on the production estimate path
