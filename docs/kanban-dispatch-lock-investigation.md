# Dispatcher lock incident: evidence does not identify a tick-lock leak

Task: t_6b68232f. Investigation only; no production changes and no claim that the original incident is fixed.

## Two distinct locks

- `gateway/kanban_watchers.py:227–261,1765–1810`: `<kanban_home>/kanban/.dispatcher.lock` is the gateway leadership lock. The owning gateway intentionally holds it between ticks, for the watcher's lifetime. No thread executing a dispatch tick is required for this lock to remain held. A stale alarm based only on this lock's acquisition age and ready cards would misclassify healthy leaders (including leaders whose workers hit capacity or quota limits).
- `hermes_cli/kanban_db.py:2294–2375,12365–12391`: `<board-db>.dispatch.lock` guards individual `dispatch_once` ticks. CLI `skipped_locked=True` comes from contention on this second file, not the global leadership lock. A locked first file does not prove a locked second file.

The task's observed fd refers to the first file, whereas the proposed release repair targets the second. Releasing the leadership lock after every tick would change intentional single-leader semantics, not repair a proven leak.

## Baseline implementation and probes

The current tick context manager already has `finally` around its yield, catches unlock errors, and closes the handle in a nested finally. Normal Python file opening already creates non-inheritable descriptors. Seven new characterization probes run against unchanged production code:

1. Hold global leadership; call real `dispatch_once(dry_run=True)` on a scratch DB: `skipped_locked` is false, while a second global contender is blocked.
2–4. Raise `KeyboardInterrupt`, `GeneratorExit`, and `asyncio.CancelledError` inside a tick: another tick can acquire afterward.
5. Abandon an entered context manager, drop its final reference, and collect: another tick can acquire afterward. This does not prove cleanup while a reference is retained.
6. Cancel an actual asyncio task holding the tick context: another tick can acquire afterward. This does not claim that cancellation can kill a worker thread.
7. Verify the real open descriptor has `FD_CLOEXEC`; launch an exec subprocess with `close_fds=False` and compare descriptor device/inode in the child: it is not inherited. This does not cover fork-without-exec.

Existing gateway standby tests additionally cover leadership takeover and cancellation. These passing results reject the supplied simple reproduction hypotheses; they do not exclude every possible incident cause.

## Raw log cross-check

Read-only search of the existing Aegis gateway log found dispatch progress inside the supplied 23:32–23:45 interval on 2026-09-20:

- `23:32:20,233`: default board `spawned=2` (line 3357).
- `23:40:27,050`: default board `spawned=13` (line 3368).
- `23:41:31,471`: default board `spawned=29` (line 3372).
- `23:42:36,700`: default board `spawned=1`, `respawn_guarded=19` (line 3381).
- `23:44:52,044`: default board `spawned=1`, `respawn_guarded=38` (line 3387).
- `23:45:53,623`: default board `spawned=5`, `respawn_guarded=38` (line 3389).

These log records lack PID attribution, so they do not establish which process emitted each tick. They do contradict treating 23:32:20 as the log's last dispatch progress through that entire interval. They do not explain the earlier gap.

## Verification

No exact verification command was given in the task. Ran the repository's canonical runner:

```
HERMES_PYTHON=/Users/alexgierczyk/.hermes/hermes-agent/.venv/bin/python bash scripts/run_tests.sh -j 2 tests/hermes_cli/test_kanban_lock_incident_characterization.py tests/hermes_cli/test_kanban_dispatch_lock.py tests/gateway/test_kanban_dispatcher_standby.py
```

Observed output:

```
=== Summary: 3 files, 30 tests passed, 0 failed (100% complete) in 74.1s (2 workers) ===
```

The new probes assert the imported `kanban_db` comes from this worktree. All DBs and lock files are sandboxed. Full suite not run; production is unchanged.

## Decision/evidence needed

To repair the reported leak rather than an unrelated mechanism, supply the exact contended **board** lock path, CLI invocation/output (including selected board), timestamp and raw thread dump associated with that contention. Alternatively approve reframing this card as observability-only hardening of board tick locks (holder/age stamp, CLI warning, stale board-lock diagnostic), explicitly without claiming a global leadership leak was fixed. Global leadership health needs tick progress/liveness, not merely acquisition age.

No fork or upstream PR for a production fix has been opened because no such fix is justified by the verified evidence yet. The investigation and probes are preserved on the task branch.
