# Absorbed-feature delta candidates — 2026-07-25 sweep

Output of the D2 absorption sweep (card `t_d82545b8`). These are fork deltas sitting on
top of code upstream has **absorbed**, so per doctrine D2 rule 3 they are now bug fixes
to *upstream's* code and are upstream-bound.

Ground truth for every row below was re-derived against the merge base
`a7a696ba59e0838a81351859abb39fb8484d4973` (`git merge-base fork/main origin/main`) and
re-checked against **current** `origin/main`, per D7a ("a shortlist is not a submit
order; re-verify against current main"). Raw `origin/main..fork/main` diffs were NOT
trusted: the fork is 405 commits behind upstream, so a raw diff mixes our deltas with
upstream-newer code and manufactures phantom candidates.

Status: **candidates only — no PR opened by this card.** Drafting/submission is a
separate unit of work.

---

## The absorption this sweep found

`tui_gateway/{compute_host,host_supervisor,synthetic_turn}.py` + `scripts/iso-certify.py`
were fork-authored (2026-07-05 / 07-12) and upstream **re-authored and merged** them as
PR #65895, commit `7d27a31ce` (2026-07-16, author `brooklyn!`), carrying
`Co-authored-by: Kyzcreig <9063726+Kyzcreig@users.noreply.github.com>`. Our own PR #63096
was closed as superseded the same day.

🔴 **Detection note worth carrying forward.** This absorption is invisible to
`gh pr list --repo NousResearch/hermes-agent --author Kyzcreig --state merged` — that
returns `[]`. **Zero** of our 19+ upstream PRs are merged as *our* PRs. Upstream absorbs
fork work by re-authoring it under a maintainer PR with a `Co-authored-by` trailer. The
reliable probe is therefore:

```bash
git log origin/main --format="%h|%ad|%an|%s" --date=short --grep="Kyzcreig" -i
```

(3 hits total, of which `7d27a31ce` is the substantive one.) A sweep that only asks the
PR API "did our PR merge?" will report "no absorptions" forever and silently accumulate
zombie-fork-copy debt.

---

## CANDIDATE 1 — `synthetic_turn.py`: stale pre-burn clock gates the delta cadence

- **File:** `tui_gateway/synthetic_turn.py` (upstream lines ~169–186 on current `origin/main`)
- **Verdict:** GENUINE UPSTREAM BUG → upstream-bound
- **Status on current `origin/main`:** still present; zero upstream commits have touched
  this file since the merge base.

Upstream samples `now = time.monotonic()` **before** the GIL-holding CPU burn and the
`time.sleep(sleep_s)` stall, then reuses that stale `now` both for the cadence gate
(`if now - last_delta >= interval`) and for the reset (`last_delta = now`). With a
per-chunk stall at or above the delta interval, the gate is evaluated against a
timestamp taken one full chunk in the past.

Fork fix (3 insertions, 2 deletions) re-samples after the burn:

```python
tick = time.monotonic()
if tick - last_delta >= interval:
    ...
    last_delta = tick
```

Corrupts **both** observable outputs (first-delta latency and total delta count) from one
shared cause. Per the doctrine's worked reference, the deterministic proof is a
fake-clock test (`sleep_s=0.20` ≥ `interval=0.05` → stale-`now` yields first delta at
0.40s / 4 deltas; the fix yields 0.20s / 5 deltas) — no real sleeping, no flake.

Patch applies clean to current `origin/main` (`git apply --check` exit 0).

---

## CANDIDATE 2 — `scripts/iso-certify.py`: broken probe client reads as FAIL, not INCONCLUSIVE

- **File:** `scripts/iso-certify.py` (upstream verdict block, ~lines 546–566)
- **Verdict:** GENUINE UPSTREAM BUG → upstream-bound
- **Status on current `origin/main`:** still present.
- ⚠️ **Not named in the task body** — found by this sweep.

Upstream folds `probe_thread_samples_ok(ws_samples, rest_samples)` into the `serving_ok`
boolean. So when the probe client fails to sample, `serving_ok` goes False and the harness
emits **`FAIL`** — i.e. it reports "isolation broke serving" when what actually happened is
"our measuring instrument broke." A certify harness that converts its own instrumentation
failure into a product verdict is exactly the acceptance-gate failure mode worth fixing.

Upstream **already accepts this principle** one branch above: `load_valid` gets its own
`INCONCLUSIVE` leg with the comment *"Never a PASS; report INCONCLUSIVE, not FAIL, so it
is not read as 'isolation broke serving'."* The fork change lifts probe-adequacy to a
sibling `INCONCLUSIVE` leg with the same rationale — so the PR argument is
consistency-with-their-own-design, not a new opinion.

Patch applies clean to current `origin/main` (`git apply --check` exit 0).

---

## CANDIDATE 3 — `compute_host.py` `source=` kwarg: **NO-PR-NEEDED** (fork-side drift)

- **File:** `tui_gateway/compute_host.py`, `_make_agent(...)` call site
- **Verdict:** NOT a bug upstream. **Do not PR.**

The fork calls `server._make_agent(..., source=frame.get("source"))` where upstream calls
`platform_override=frame.get("source")`. The task body carried this as a delta to extract.
Ground-truthing the **callee signature** (doctrine D2 rule 2 — "verify, don't assume the
fork side is right") shows the fork side is the drift:

```
origin/main  _make_agent params: [... 'service_tier_override', 'platform_override']
             'source' in params: False
fork/main    _make_agent params: [... 'service_tier_override', 'source', 'platform_override']
             'source' in params: True
```

The fork kwarg binds **only because the fork widened `_make_agent`'s own signature**.
Porting `source=` to upstream's narrower signature would raise `TypeError`. Upstream's
`platform_override=` form is already correct.

Follow-up owed on the **fork** side (not upstream): either converge on
`platform_override=` or register the extra `source` parameter as a fork-permanent seam.
Recorded here so the next sweep doesn't re-propose this as a candidate.

An honest NO-PR verdict is a first-class deliverable; PR-count is not the success metric.

---

## Not candidates (checked, deliberately excluded)

- **`host_supervisor.py` / `compute_host.py` `encoding="utf-8", errors="replace"`** — a raw
  `origin/main` vs `fork/main` diff shows these as fork "deletions." They are not ours.
  Against the merge base, `base->fork` is **empty** for `host_supervisor.py`; the lines are
  upstream-**newer** code (`base->origin` = 9 insertions) that the fork simply hasn't merged
  yet. Reporting these as fork deltas would have been a pure artifact of the 405-commit lag.
- **`iso-certify.py` `started = False` reordering** — cosmetic statement reordering with no
  behavioral difference. Not worth a hunk in an upstream PR.

---

## Registry effect (`docs/sync/fork-features.json`)

- **Added** entry `compute-host turn isolation (...)` → `lifecycle: absorbed`,
  `upstream_ref: .../pull/65895`, `absorbed_date: 2026-07-16`, with the four absorbed paths
  and three canary test files. The family was previously **unregistered entirely** — an
  absorbed feature carrying live fork deltas and no manifest coverage.
- **Corrected** the telegram entry's provenance. It was recorded `absorbed` against
  PR #52844, but #52844 is authored by `teknium1` fixing third-party issue #46621 (reporter
  `otopba`) — **not** a fork submission. That is D3 parallel invention, not D2 absorption of
  our work. `lifecycle` stays `absorbed` (upstream's implementation is canonical, ours is
  retired) but the `why` no longer implies we authored it.
- The six `upstream-intended` entries keep `upstream_ref: null`. Verified by exact-path
  match that **no** open Kyzcreig PR touches any of their `fork_ext` paths, so they remain
  genuine backlog. (A substring search suggests false hits such as #71444/#39587 — those
  match on unrelated filenames, not the registry paths.)
