# Portable verification and cleanup attribution

The probe requirements pin the direct dependency versions from the previously
used test interpreter. PM resolves their transitive dependencies; this is a
focused test environment, not the full product lock or a production installation.
No optional TTS dependencies are needed for these judge tests.

Preparation (Linux, Python 3.14):

1. Extract `git archive HEAD` into a private data-disk directory.
2. Copy the installed hermes-pytest-hermeticity-guard source (pyproject.toml + src)
   into a sibling `guard/` directory. It contains no credentials. Keep its
   pytest11 entry point enabled; install it noneditable through PM.
3. From the source tree, build a fresh sibling environment:

       python3 -m pm.build_env --out ../probe-env \
         --requirements plans/goal-judge-fallback/probe-requirements.txt \
         --requirement 'hermes-pytest-hermeticity-guard @ file:///absolute/path/guard' \
         --python /usr/bin/python3

4. Set TMPDIR to a private directory on the data disk and PYTHONPATH to this
   source tree. Run the three explicit test files through the installed test-gate
   with that environment's interpreter. Canonical scripts/run_tests.sh was refused
   on the worker by local-suite-guard in earlier attempts; the sanctioned narrow
   test-gate command is the recorded runner deviation.
5. For attribution only, put plans/goal-judge-fallback on PYTHONPATH and set
   PYTEST_PLUGINS=stage_probe and JUDGE_STAGE_TRACE to a private JSONL path.
   Do not use `-p stage_probe`: importing hermes_cli.main interprets that option
   as its own profile argument. The diagnostic plugin preserves all guards.

Observed on source a3e1fbb:

- All six malformed-output cases passed in isolation on ci (77.33 s) and Studio
  (49.29 s), each with 12 real child/HTTP/returncode=0 reaps. The prior malformed
  failure did not reproduce; it is not attributed to load as a proven cause.
- The broader ci run reproduced a different actual failure: 17 passed, then the
  deadline/cancel request-owner test failed its two-second cleanup assertion.
- Diagnostic parent PID 291638, child 299197: kill entered at monotonic
  28405.737822895; foreground cleanup returned pending at 28405.787509233.
  The reaper stack was kill_process_tree -> _process_tree_snapshot ->
  psutil.children -> Linux ppid_map -> home_io_guard realpath on /proc files.
  No kill return was observed before the test failed. The gate killed the
  leftover group. This proves the ownership census delayed signalling; it does
  not prove that the original Studio malformed case had the same stack.

The repair launches owned POSIX requests in fresh sessions and signals their
own process group directly, avoiding a whole-machine ancestry census. Windows
keeps canonical tree termination. The retained cleanup fence and finite caller
wait remain unchanged; failed/stuck signalling still inhibits retries. The
three focused deadline/cancel and stuck-cleanup tests passed after the repair.
No timeout or test deadline was raised, and no live configuration was changed.
