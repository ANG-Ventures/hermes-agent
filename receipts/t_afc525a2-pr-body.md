Card: t_afc525a2. Repairs the initial no-fork terminal spawn implementation; not ready to land until exact-head CI passes.

Approach: Popen pipe/communication handling with a lazily created subclass overriding the POSIX execution boundary. It invokes os.posix_spawn with setsid and original close_fds semantics on fallback. Explicit standard descriptors and closed standard streams fall back to ordinary Popen; unsupported setsid falls back without weakening session isolation. Windows is unchanged.

On platforms without POSIX_SPAWN_CLOSEFROM, an isolated -I -S interpreter closes its own single-threaded fd table before exec. Parent-side inheritable-fd enumeration was racy and is removed. This adds interpreter startup latency, not parent-GIL fork latency. No untrusted startup/site code is loaded. This revised fallback has NOT been benchmarked on macOS; previous Apollo measurements apply to the previous helper, not this head.

Also fixes import with Popen replaced by a function, cold bash version probes, run_nofork input/capture_output/check behavior, physical cwd and missing-cwd diagnostics, Android shell path, and recovered-cwd assertions.

Verification: local test-gate never executed tests (guest-not-enrolled); remote gate could not reach ci/ace-media/ace-ai. Direct invocation of the 16 fixture regression functions with temporary HOME/HERMES_HOME passed (not a full pytest collection). Original helper reproduces import TypeError, late inheritable descriptor leak, and discarded input. CI is required for collection and broader regressions.

Fragmented 2 GB heap, 231 threads, 1500 open descriptors, 20 spawns, Linux Python 3.12.3, pid 1016981: old forced fork ticker-gap p50/p95 25.052/32.095 ms; repaired posix_spawn 0.040/0.266 ms. Fork_exec/posix_spawn counts old 20/0 vs repaired 0/20. Linux native vfork baseline p50/p95 0.024/0.070 ms. Ticker gaps include scheduling effects, not pure GIL CPU time. No new macOS measurements.

new-suite: tests/tools/test_spawn_nofork.py -- real-process no-fork, inheritance and session semantics
mutation-canary: tests/tools/test_spawn_nofork.py::test_late_inheritable_fd_is_closed

live-evidence: yes: post-deploy gateway py-spy --gil _execute_child under 5% (was 29.2%); owner performs deployment/restart. No live changes by worker.
