# pre_tool_call callback contention — findings

Date: 2026-09-21 PT
Task: t_59c0886e

## Root cause

The reported message did not identify a callback timeout. It conflated two branches in `PluginManager.invoke_hook()`:

- actual callback timeout; and
- any callback already present in the process-global `_hook_running_callbacks` map.

The map key was `(hook_name, id(callback))`. The default gateway multiplexes many sessions through one `PluginManager`, so a normal callback invocation in session A made the same callback look “still running” to session B. `pre_tool_call` is globally fail-closed, so session B received `pre_tool_call plugin callback timed out or is still running` even when session A's callback was healthy and milliseconds old.

Evidence from `/Users/alexgierczyk/.hermes/logs/gateway.error.log`:

| Signal | Count |
|---|---:|
| `pre_tool_call` callback timeout lines | 0 |
| `pre_tool_call` “timeout or while still running” skips | 1,917 |
| `_pre_tool_call` skips | 1,212 |
| shell-hook skips (sleep TTL + vision dedup) | 625 |

The dominant producer was therefore ordinary overlap in Python callback `_pre_tool_call`, not the shell hook. The shell hook was a secondary producer because it shared the same process-global running key.

### The defect predates the incident by three weeks

Re-counted over the full `gateway.error.log` (2,055 `pre_tool_call` skips):

| Date | Skips | | Date | Skips |
|---|---:|---|---|---:|
| 2026-08-31 | 57 | | 2026-09-15 | 26 |
| 2026-09-01 | 4 | | 2026-09-16 | 16 |
| 2026-09-04 | 14 | | 2026-09-17 | 24 |
| 2026-09-07 | 4 | | 2026-09-18 | 85 |
| 2026-09-08 | 138 | | 2026-09-19 | 243 |
| 2026-09-10 | 285 | | 2026-09-20 | 399 |
| 2026-09-12 | 31 | | 2026-09-21 | 664 |

First skip `2026-08-31 02:15:09`, last `2026-09-21 04:53:56`. The bug is **not load-specific** — heavy multi-worker load only raised the overlap *rate*. Any two sessions sharing one `PluginManager` could trigger it from day one, so this fix is broader than an incident patch.

Source path before fix:

- `hermes_cli/plugins.py:450`: `pre_tool_call` belongs to `_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS`.
- `hermes_cli/plugins.py:458`: 60-second post-timeout suppression.
- `hermes_cli/plugins.py:3685`: 30-second default callback budget.
- `hermes_cli/plugins.py:5628-5652`: process-global `(hook_name, id(cb))` running key; normal overlap entered the same branch as suppression.
- `hermes_cli/plugins.py:5685-5700`: actual timeout path abandons the daemon thread, arms suppression, and (for `pre_tool_call`) appends a block directive.

## Standalone hook timings

Canned payload, `/usr/bin/python3`, 10 sequential runs each under current host load:

| Hook | n | p50 | p95 | max | exits |
|---|---:|---:|---:|---:|---:|
| `sleep-ttl-guard.py` | 10 | 35.6 ms | 44.4 ms | 65.5 ms | 10/10 rc=0 |
| `vision-dedup-guard.py` | 10 | 53.0 ms | 67.6 ms | 69.4 ms | 10/10 rc=0 |

These are 460–840x below the outer 30-second callback budget. The “Python spawn exceeded 30 seconds” hypothesis is not supported by timing or logs.

`py-spy dump --pid 99910` was attempted against the live default gateway. macOS refused attach with `This program requires root on OSX`; no unattended elevation was performed.

## Host load

A 2-second live sample found 29 `pyright-langserver` processes totaling 136.0% CPU and 18,766.5 MiB RSS. Two processes accounted for 76.2% and 52.9% CPU. This is meaningful host pressure, but it is not the mechanism behind this error: the configured shell hooks still completed in <=69.4 ms, and there were zero `pre_tool_call` timeout records.

Recommendation: set `lsp.enabled: false` on dedicated Kanban worker profiles (`daedalus`, `daedalus-fable`, `daedalus-opus`) or implement dispatcher-scoped LSP disablement. Current profile configs explicitly enable LSP. This is an operations follow-up, not part of the callback correctness fix.

## Fix

1. Ordinary concurrent callback invocations are no longer treated as timed out. Suppression starts only after an invocation actually exceeds its budget.
2. Shell-hook callbacks now expose their individual `fail_closed` setting to the outer plugin timeout boundary. Advisory shell hooks remain fail-open on outer timeout/suppression; explicitly enforcing hooks remain fail-closed.
3. Timeout logging now emits callback name, measured elapsed duration, and configured budget. Suppression logs are no longer mislabeled “or while still running.”
4. **The user-facing block message is split in two and no longer lies.** The single `pre_tool_call plugin callback timed out or is still running` string described a state this fix deleted, and was the exact wording that sent the original investigation down a phantom concurrency-snowball hypothesis. There are two distinct refusal paths and they now report themselves accurately:

   | Path | `plugins.py` | Message |
   |---|---|---|
   | genuine timeout | :5736 | `pre_tool_call plugin callback <name> timed out after 0.101s (budget 0.1s)` |
   | suppression window | :5683 | `pre_tool_call plugin callback <name> is suppressed after an earlier timeout (retry in 60s)` |

   Collapsing these back into one string would re-break the diagnosis: the suppression refusal is **not** a timeout of the call being refused — an *earlier* call timed out and the 60 s cooldown is still open. Saying "timed out" there would be false in the opposite direction. Both now name the callback, matching the WARNING lines.

## Measurement trap: ad-hoc `python` in a worktree executes the LIVE tree

Worth recording, because it silently invalidated two round-1 probes and is very hard to see.

The shared venv (`~/.hermes/hermes-agent/venv`, which `scripts/run_tests.sh` falls back to for worktrees) installs an **editable-install finder** that hard-maps every top-level package to the live checkout:

```
site-packages/__editable___hermes_agent_0_20_6_finder.py
  → /Users/alexgierczyk/.hermes/hermes-agent/hermes_cli     (LIVE, not the worktree)
```

The deception: `module.__file__`, `module.__cached__`, and `inspect.getsource()` all report the **worktree** path, while the code object that actually runs is the **live tree's**. A probe can print worktree source and execute live-tree bytecode in the same breath.

- ✅ **pytest is safe** — it inserts rootdir at `sys.path[0]`, which beats the finder. Verified: `sys.path.insert(0,'.')` resolves to the worktree copy. The 128/128 and mutation runs are therefore sound.
- ❌ **bare `python probe.py` from the worktree is NOT safe** — it silently runs the live tree.

Reliable oracle: the **logging record's** `pathname`/`lineno` (`record.pathname`), or `func.__code__.co_filename`. Not `__file__`, not `inspect.getsource`.

Correct invocation for any probe against a worktree:

```bash
PYTHONPATH=/Users/alexgierczyk/.hermes/hermes-agent/.worktrees/t_59c0886e python probe.py
```

Better still, assert the tree inside the probe so it cannot silently measure the wrong code:

```python
assert "/.worktrees/t_59c0886e/" in pm.PluginManager.invoke_hook.__code__.co_filename
```

## Verification

- RED before fix: two new regressions failed (normal concurrent overlap synthesized a block; advisory timeout synthesized a block).
- GREEN after fix: `scripts/run_tests.sh tests/hermes_cli/test_plugins.py tests/agent/test_shell_hooks.py -q` -> 128 passed, 0 failed.
- Mutation: reverted only production changes while retaining tests -> exactly 2 failed; restored production patch -> 128 passed.
- 50-way in-process overlap through the real `PluginManager.invoke_hook` path -> `entered=50 completed=50/50 blocks=0`.
- `git diff --check` passed.

Round 2 (message split), all probes import-asserted against the worktree:

- Two refusal paths are distinct and accurate: `identical: False`, timeout path reports `timed out after 0.101s (budget 0.1s)`, suppression path reports `is suppressed after an earlier timeout (retry in 60s)`.
- Mutation (re-conflate the suppression path back to the old single string, tests kept) -> `test_pre_tool_call_timeout_fail_closed` FAILED on `assert 'hung_policy' in msg2`; restored -> 128 passed, 0 failed.
- Thread accumulation re-verified on real PR code (round 1 cleared this against the live tree): 40 invocations against a permanently hung hook -> `callback_actually_spawned=1`, `thread delta=1`, exactly 2 distinct messages.
- Fail-closed policy unchanged: advisory (`fail_closed=False`) -> allow on timeout AND suppression; enforcing (default) -> block on both.
- `ruff check` -> All checks passed.

A literal post-deploy worker-session 50-call check remains deployment-gated: this task is forbidden to restart the gateway, and the live gateway continues running the old runtime until Apollo deploys/restarts it.
