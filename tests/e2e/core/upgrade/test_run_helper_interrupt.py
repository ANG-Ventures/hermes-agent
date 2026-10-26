"""An interrupted ``H.run`` takes its sandbox with it (t_395344c0).

The harness wall-clock stop arrives as an async exception inside ``communicate()``. A child left
running there keeps its PM generation leased (a held flock) for the rest of the module, so the NEXT
test sharing the home sees that generation survive GC (test_generation_gc: "pm repair #1 kept
superseded generations built two days ago").
"""

from __future__ import annotations

import fcntl
import os
import signal
import sys
import time

import pytest

from tests.e2e.core.upgrade import _helpers as H

# The child holds an exclusive flock, like a generation lease, then outlives any reasonable test.
_HOLDER = ("import fcntl, sys, time\n"
           "f = open(sys.argv[1], 'a+')\n"
           "fcntl.flock(f, fcntl.LOCK_EX)\n"
           "open(sys.argv[2], 'w').close()\n"
           "time.sleep(120)\n")


class _Stop(BaseException):
    pass


def _lock_free(path) -> bool:
    with open(path, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(f, fcntl.LOCK_UN)
        return True


@pytest.mark.platforms("posix")
def test_interrupt_inside_run_releases_the_childs_lease(tmp_path):
    lease, ready = tmp_path / "lease", tmp_path / "ready"

    def fire(*_):
        raise _Stop()

    previous = signal.signal(signal.SIGALRM, fire)
    signal.alarm(4)
    try:
        with pytest.raises(_Stop):
            H.run([sys.executable, "-c", _HOLDER, str(lease), str(ready)], env=dict(os.environ),
                  cwd=tmp_path, writable=[tmp_path], timeout=120)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    assert ready.exists(), "harness: the child never took its lease before the interrupt"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _lock_free(lease):
            return
        time.sleep(0.05)
    pytest.fail("the interrupted H.run left its child running: the lease is still held")
