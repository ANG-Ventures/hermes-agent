# staging/lcm-profile — isolated hermes-lcm adoption smoke (PRD #2 v2, Phase 3)

This directory is an **isolated staging profile** used to evaluate the
`stephenschoettler/hermes-lcm` context engine without touching any live Hermes
profile. It exists so the LCM adoption smoke (PRD #2 v2, Phase 3) can run against
the real engine in-process while honoring the task's hard constraint:

> **NO writes to live `~/.hermes/plugins` or `~/.hermes/profiles/*`.**

## Layout

```
staging/lcm-profile/
├── README.md                     # this file
├── VENDORED_FROM.txt             # upstream commit + license + audit verdict
└── plugins/
    └── hermes-lcm/               # vendored copy of the audited plugin (no .git)
```

The plugin is **vendored** (a plain copy of the audited clone, `.git` removed),
not symlinked, and not installed via the upstream `scripts/install.sh` — that
installer writes a symlink into `~/.hermes/plugins/hermes-lcm` or
`~/.hermes/profiles/<p>/plugins/hermes-lcm`, which is exactly the live-profile
write this task forbids. We therefore load the engine directly from this path.

## How the smoke runs

`scripts/probe_hermes_lcm_isolated.py` (repo root) registers
`plugins/hermes-lcm/` as the in-process `hermes_lcm` package (the same loader the
plugin's own `tests/conftest.py` uses), instantiates `LCMEngine` against a
throwaway SQLite DB under a temp dir, and exercises six smoke dimensions:

1. **load + identity** — `LCMEngine` is a `ContextEngine` subclass; `name == "lcm"`.
2. **normal chat/tool** — ingest messages; `lcm_status` / `lcm_describe` respond.
3. **threshold compaction** — `should_compress()` honors the threshold; `compress()`
   builds a DAG summary node and shrinks the active context.
4. **lcm_grep / describe / expand recall** — a fact compacted out of the active
   context is found by `lcm_grep` and recovered **byte-identically** by `lcm_expand`.
5. **reset semantics** — `on_session_reset()` zeroes per-session counters while the
   immutable lossless store still answers `lcm_grep`.
6. **failure behavior (fail-open)** — with the summarizer LLM unavailable,
   escalation degrades to L3 deterministic truncation (no crash, no message lost).

Summarization is **stubbed deterministically** so the smoke is offline and
reproducible — the same `summarize_with_escalation` / `_invoke_summary_llm_chain`
seam the plugin's upstream tests patch. No real provider call is made.

## Run it

From the worktree root, using the shared hermes venv:

```bash
python scripts/probe_hermes_lcm_isolated.py \
    --profile-dir staging/lcm-profile \
    --out docs/reports/hermes-lcm-adoption-smoke.md
pytest tests/context_engine/test_lcm_adoption_smoke.py -q
```

The probe exits 0 only when all checks pass and refuses to run if the plugin dir
resolves under a live `~/.hermes/plugins` or `~/.hermes/profiles` path.

## Provenance, security, and license

- **Upstream:** `github.com/stephenschoettler/hermes-lcm` @ `03b74f8` (main,
  2026-06-13), plugin manifest `v0.16.2`. See `VENDORED_FROM.txt`.
- **Ingest audit:** PASS for fleet-internal use. The `external-code-ingest-audit`
  gate flagged 22 HIGH, **all** placeholder secrets inside `tests/` and
  `benchmarking/` fixtures that exercise the engine's own redaction
  (`sk-def...cdef`, `[REDACTED PRIVATE KEY]`, etc.); `scary-non-test == 0`.
- **License:** the upstream repo ships **no LICENSE file**. Internal fleet
  run/fork is acceptable; public redistribution/vendoring is blocked until a
  license grant (PRD §0.1, §1, §3). This vendored copy is for isolated evaluation
  only and must not be redistributed.

## What this is NOT

- Not a live activation. Turning LCM on still requires `plugins.enabled:
  [hermes-lcm]` + `context.engine: lcm` in a real profile config plus a Hermes
  restart — deferred to a first low-blast-radius profile (Daedalus/Athena) per
  PRD §9.5, and gated on PRD #3's eval battery.
- Not the PRD #3 real-session recovery gate. This smoke proves the mechanics
  (load, compact, recall byte-exact, reset, fail-open) in-process; it does **not**
  prove a live model spontaneously calls `lcm_expand` without being told to.
