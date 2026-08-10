"""Regression tests for the high-fd output blackout (silent empty capture).

Root cause: ``BaseEnvironment._wait_for_process``'s drain thread polled the
subprocess stdout pipe with ``select.select()``. On POSIX, select(2) cannot
represent a file descriptor at or above ``FD_SETSIZE`` (1024) and raises
``ValueError: filedescriptor out of range in select()``. The drain loop
swallowed that with a bare ``break``, so every command in a long-lived
gateway that had accumulated >1024 fds returned ``{"output": "",
"returncode": 0}`` — indistinguishable from a command that legitimately
printed nothing. ``file_operations._exec`` shares this path, so ``write_file``
post-write verification read back "" in the same window and reported a bogus
"the write did not persist".

Two guarantees are locked here:

1. Draining a subprocess whose stdout fd is >= FD_SETSIZE returns the real
   output (poll() has no such ceiling).
2. If the drain DOES abort abnormally, the result fails LOUD — a marker in
   the output and a ``drain_error`` key — rather than returning a silent
   empty capture.
"""

import os
import resource
import subprocess
import sys

import pytest

from tools.environments.base import BaseEnvironment, _BoundedOutputCollector

FD_SETSIZE = 1024

# Burn comfortably past the cliff so the spawned pipe lands above it with
# room to spare rather than straddling it.
_BURN_CEILING = FD_SETSIZE + 60

# Safety valves: no platform should ever spin in the burn loop.
_MAX_HELD = 4000
_MAX_SPAWN_ATTEMPTS = 5


class _TestableEnv(BaseEnvironment):
    def __init__(self, cwd="/tmp", timeout=10):
        super().__init__(cwd=cwd, timeout=timeout)

    def _run_bash(self, cmd_string, *, login=False, timeout=120, stdin_data=None):
        raise NotImplementedError("not used")

    def cleanup(self):
        pass


def _lowest_free_fd(limit=FD_SETSIZE):
    """Return the lowest currently-unused fd below *limit*, or None if full.

    POSIX guarantees a new fd is the lowest-numbered one available, so this
    is exactly what the next ``Popen`` pipe would be handed.
    """
    for fd in range(3, limit):
        try:
            os.fstat(fd)
        except OSError:
            return fd
    return None


@pytest.fixture
def burn_fds():
    """Occupy EVERY fd below FD_SETSIZE so fresh fds must land above it.

    Why "every" and not merely "the highest one we grabbed": POSIX hands out
    the *lowest* free descriptor.  A single hole anywhere under 1024 — left by
    an unrelated fd closed while this fixture runs (a GC'd file object, a
    logging handler rotating, a pytest capture fd being recycled) — is claimed
    by the next ``subprocess.Popen`` pipe, which then lands *below* the cliff.

    The original fixture checked only its own last-allocated pair, so it
    reported success while a hole remained.  That made this test FLAKY rather
    than weak: it passed most runs and failed the ones where a hole happened to
    open, with the fixture's own anti-vacuous guard as the failing assertion
    (measured on macOS: an fd freed at 911 mid-burn, Popen took 911, the guard
    fired ``assert 911 >= 1024``).  Closing every hole before yielding removes
    the race at its source; ``assert_high_fd_pipe`` below re-plugs any hole
    that opens between the burn and the spawn.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    raised = False
    if soft < FD_SETSIZE + 200:
        try:
            resource.setrlimit(
                resource.RLIMIT_NOFILE, (min(hard, FD_SETSIZE + 400), hard)
            )
            raised = True
        except (ValueError, OSError):
            pytest.skip(
                "cannot raise RLIMIT_NOFILE above FD_SETSIZE "
                f"(soft={soft}, hard={hard}); this platform cannot reach the "
                "fd >= 1024 path the regression guards"
            )

    soft_now, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft_now <= _BURN_CEILING:
        pytest.skip(
            f"RLIMIT_NOFILE soft ceiling is {soft_now}, at or below the "
            f"{_BURN_CEILING} fds needed to push a pipe past FD_SETSIZE "
            f"({FD_SETSIZE}); cannot exercise the high-fd path here"
        )

    held: list[int] = []
    try:
        while True:
            try:
                fd = os.open(os.devnull, os.O_RDONLY)
            except OSError as exc:
                pytest.skip(
                    f"ran out of file descriptors at fd count {len(held)} "
                    f"before reaching FD_SETSIZE ({FD_SETSIZE}): {exc}"
                )
            held.append(fd)
            if fd > _BURN_CEILING:
                break
            if len(held) > _MAX_HELD:
                break

        # The loop above stops at the first fd past the ceiling, but holes
        # below FD_SETSIZE may have opened *while* it ran.  Plug them all:
        # only when nothing is free below the cliff is a fresh pipe forced
        # above it.
        while True:
            hole = _lowest_free_fd()
            if hole is None:
                break
            try:
                held.append(os.open(os.devnull, os.O_RDONLY))
            except OSError as exc:
                pytest.skip(f"could not plug free fd {hole} below FD_SETSIZE: {exc}")

        ceiling = max(held) if held else -1
        if _lowest_free_fd() is not None:
            pytest.skip(
                "could not exhaust file descriptors below FD_SETSIZE "
                f"({FD_SETSIZE}); highest fd reached was {ceiling}"
            )
        yield
    finally:
        for fd in held:
            try:
                os.close(fd)
            except OSError:
                pass
        if raised:
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
            except (ValueError, OSError):
                pass


def _spawn_high_fd_echo(marker):
    """Spawn ``echo <marker>`` whose stdout pipe is above FD_SETSIZE.

    Returns ``(proc, fd, attempts)``.  The caller keeps the anti-vacuous
    assertion; this only removes the *race*, never the check — if every
    attempt lands low, the last process is returned as-is so the caller's
    ``assert fd >= FD_SETSIZE`` fires with the real measured fd.

    Even with every fd below the cliff occupied by the fixture, a hole can
    open in the window between the burn and this spawn (an unrelated fd being
    closed by GC, logging, or pytest's own capture machinery), and POSIX hands
    the lowest free fd to the new pipe.  When that happens the spawned pipe is
    itself now plugging the hole — so keep it alive and try again.  A few
    attempts converge because each retry permanently fills one more hole.
    """
    parked = []
    for attempts in range(1, _MAX_SPAWN_ATTEMPTS + 1):
        proc = subprocess.Popen(
            ["/bin/sh", "-c", f"echo {marker}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        if fd >= FD_SETSIZE or attempts == _MAX_SPAWN_ATTEMPTS:
            # Release the low-fd stand-ins now that our pipe's fd is fixed.
            for stale in parked:
                try:
                    stale.kill()
                    stale.wait(timeout=5)
                except Exception:
                    pass
            return proc, fd, attempts
        # Landed in a hole. Hold this process open (its pipe now fills the
        # hole) and retry; closing it here would simply reopen the hole.
        parked.append(proc)

    raise AssertionError("unreachable")  # pragma: no cover


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX FD_SETSIZE behaviour")
def test_high_fd_stdout_is_still_captured(burn_fds):
    """A subprocess whose stdout fd is >= FD_SETSIZE must still be drained.

    Without the poll() fix this returns output="" with returncode=0 — the
    silent blackout.
    """
    env = _TestableEnv()
    proc, fd, attempts = _spawn_high_fd_echo("HIGH_FD_MARKER_OK")
    try:
        # Anti-vacuous guard: if the pipe is not actually above the cliff the
        # poll()-vs-select() path under test was never exercised, and a pass
        # would be meaningless.  Kept deliberately — see the card that made
        # this fixture reliable rather than deleting this line.
        assert fd >= FD_SETSIZE, (
            f"fixture failed to push the pipe above FD_SETSIZE (fd={fd}) after "
            f"{attempts} spawn attempt(s); the test would pass vacuously"
        )
        result = env._wait_for_process(proc, timeout=20)
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    assert result["returncode"] == 0
    assert "HIGH_FD_MARKER_OK" in result["output"], (
        "high-fd stdout was silently dropped — this is the blackout bug"
    )
    assert "drain_error" not in result


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX FD_SETSIZE behaviour")
def test_low_fd_stdout_still_works():
    """Control: the ordinary (low fd) path is unchanged."""
    env = _TestableEnv()
    proc = subprocess.Popen(
        ["/bin/sh", "-c", "echo LOW_FD_MARKER_OK; echo ERRSIDE >&2"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        result = env._wait_for_process(proc, timeout=20)
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    assert result["returncode"] == 0
    assert "LOW_FD_MARKER_OK" in result["output"]
    assert "ERRSIDE" in result["output"]


class TestDrainFailsLoud:
    """An abnormal drain abort must never look like an empty result."""

    def test_drain_error_prepends_marker_and_sets_key(self):
        collector = _BoundedOutputCollector(1000)
        result = BaseEnvironment._finalize_wait_result(
            collector,
            "",
            0,
            ["ValueError: filedescriptor out of range in select()"],
        )
        assert result["output"] != "", "a failed capture must not render as empty"
        assert "OUTPUT CAPTURE FAILED" in result["output"]
        assert "filedescriptor out of range" in result["output"]
        assert result["drain_error"] == (
            "ValueError: filedescriptor out of range in select()"
        )

    def test_partial_output_is_kept_below_the_marker(self):
        collector = _BoundedOutputCollector(1000)
        result = BaseEnvironment._finalize_wait_result(
            collector, "partial stdout\n", 0, ["OSError: [Errno 9] Bad file descriptor"]
        )
        assert result["output"].startswith("[hermes] OUTPUT CAPTURE FAILED")
        assert "partial stdout" in result["output"]

    def test_clean_drain_is_untouched(self):
        collector = _BoundedOutputCollector(1000)
        result = BaseEnvironment._finalize_wait_result(collector, "hello\n", 0, [])
        assert result == {"output": "hello\n", "returncode": 0}

    def test_clean_drain_default_arg_is_untouched(self):
        collector = _BoundedOutputCollector(1000)
        result = BaseEnvironment._finalize_wait_result(collector, "hello\n", 0)
        assert result == {"output": "hello\n", "returncode": 0}
