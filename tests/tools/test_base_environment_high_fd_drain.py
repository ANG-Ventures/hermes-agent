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


class _TestableEnv(BaseEnvironment):
    def __init__(self, cwd="/tmp", timeout=10):
        super().__init__(cwd=cwd, timeout=timeout)

    def _run_bash(self, cmd_string, *, login=False, timeout=120, stdin_data=None):
        raise NotImplementedError("not used")

    def cleanup(self):
        pass


@pytest.fixture
def burn_fds():
    """Hold open pipes until fresh fds land above FD_SETSIZE."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    raised = False
    if soft < FD_SETSIZE + 200:
        try:
            resource.setrlimit(
                resource.RLIMIT_NOFILE, (min(hard, FD_SETSIZE + 400), hard)
            )
            raised = True
        except (ValueError, OSError):
            pytest.skip("cannot raise RLIMIT_NOFILE above FD_SETSIZE")

    held = []
    try:
        while True:
            try:
                r, w = os.pipe()
            except OSError:
                break
            held.append((r, w))
            if r > FD_SETSIZE + 60:
                break
            if len(held) > 4000:
                break
        if not held or held[-1][1] <= FD_SETSIZE:
            pytest.skip("could not push file descriptors above FD_SETSIZE")
        yield
    finally:
        for r, w in held:
            for fd in (r, w):
                try:
                    os.close(fd)
                except OSError:
                    pass
        if raised:
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
            except (ValueError, OSError):
                pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX FD_SETSIZE behaviour")
def test_high_fd_stdout_is_still_captured(burn_fds):
    """A subprocess whose stdout fd is >= FD_SETSIZE must still be drained.

    Without the poll() fix this returns output="" with returncode=0 — the
    silent blackout.
    """
    env = _TestableEnv()
    proc = subprocess.Popen(
        ["/bin/sh", "-c", "echo HIGH_FD_MARKER_OK"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        fd = proc.stdout.fileno()
        assert fd >= FD_SETSIZE, (
            f"fixture failed to push the pipe above FD_SETSIZE (fd={fd}); "
            "the test would pass vacuously"
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
